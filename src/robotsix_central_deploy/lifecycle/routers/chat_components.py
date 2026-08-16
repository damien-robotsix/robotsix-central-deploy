"""Component roster and chat-skill endpoints for the chat agent.

Exposes:
- ``GET /chat-skill`` — the deploy server's own chat-agent skill description
- ``GET /chat/components`` — list components reachable by the chat agent
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..._http import retry_client_context
from ...registry.config_store import ComponentConfigStore
from ...registry.traefik_labels import public_url
from .._config_utils import _sanitize_log
from ..auth import verify_auth
from ..deps import _get_component_config_store, _get_config
from ._chat_common import (
    _inject_auth,
    logger,
)

# Simple TTL cache for skill bodies: {component_id: (timestamp, body)}
_skill_cache: dict[str, tuple[float, str]] = {}
_SKILL_CACHE_TTL: float = 60.0

router = APIRouter(tags=["chat"])


def _excluded(component_id: str, reason: str) -> dict[str, Any]:
    """A roster entry for a component the agent cannot use, and why.

    Dropping such a component outright is what let file-hub sit unusable and
    unmentioned: it had no ports, so it never reached the roster, and the only
    trace was a log line nobody was reading. An entry with no ``skill`` is
    still not callable — consumers already filter on ``_error`` — but the
    reason is now in the payload rather than only in the logs.
    """
    return {"id": component_id, "base_url": "", "skill": "", "_error": reason}


# ---------------------------------------------------------------------------
# GET /chat-skill — the deploy server's own skill, so the chat agent can
# discover the deploy component itself.
# ---------------------------------------------------------------------------


@router.get(
    "/chat-skill",
    summary="Deploy server's own chat-agent skill description",
    responses={200: {"description": "Markdown skill body"}},
)
async def deploy_chat_skill() -> str:
    """Return the Markdown skill description for the deploy server itself.

    This lets the deploy server register as a virtual component with
    ``chat_base_url`` pointing at itself, and the roster endpoint's
    probe succeeds against this handler.
    """
    return (
        "# Deploy Lifecycle Server\n"
        "Manages the robotsix Docker fleet: start, stop, restart, deploy, "
        "rollback, and inspect every managed component.\n\n"
        "## Authentication\n"
        "All requests require an `X-API-Key` header.  The chat agent reads "
        "this key from its own config at `central_deploy.api_token`, which "
        "the deploy server provisions into the component's config volume.\n\n"
        "## Read-only endpoints\n"
        "- `GET /services` — list all managed services\n"
        "- `GET /services/{name}` — full status (state, image, health, digests)\n"
        "- `GET /services/{name}/health` — health status string\n"
        "- `GET /services/{name}/logs` — stream container logs\n"
        "- `GET /health` — liveness probe\n"
        "- `GET /disk` — host disk usage + Docker storage breakdown\n"
        "- `GET /chat/components` — list components reachable by the chat agent\n"
        "- `GET /chat/audit-log` — read recent audit entries\n"
        "- `GET /chat/langfuse/projects` — list configured Langfuse project aliases\n"
        "- `GET /chat/langfuse/{project}/traces` — list Langfuse traces for a project\n"
        "- `GET /chat/langfuse/{project}/traces/{traceId}` — single Langfuse trace detail\n"
        "- `GET /chat/langfuse/{project}/observations` — list Langfuse observations\n"
        "- `GET /chat/langfuse/{project}/observations/{observationId}` — single observation detail\n"
        "- `GET /chat/services/{name}/logs` — recent container logs (bounded tail, per-component gate)\n"
        "- `GET /chat/services/{name}/status` — lifecycle status as structured JSON (per-component gate)\n"
        "- `GET /chat/services/{name}/volumes` — list named volumes for a service (per-component gate)\n"
        "- `GET /chat/services/{name}/volumes/{vol}/files?path=…` — read-only file inspection within a volume (per-component gate)\n\n"
        "## Scoped write endpoints (chat-agent allowlisted)\n"
        "- `POST /chat/services` — register a new managed component (registration only; no auto-start)\n"
        "- `POST /chat/services/{name}/deploy` — first-boot deploy a newly-registered component (create + start container)\n"
        "- `POST /chat/services/{name}/restart` — restart a service\n"
        "- `POST /chat/services/{name}/update` — pull + recreate (deploy) a service\n"
        "- `POST /chat/disk/reclaim` — prune dangling Docker images and/or reclaimable build cache\n\n"
        "## Config writes\n"
        "A component owns its own config. Read it here via "
        "`GET /chat/config/{name}`, but send changes to the component's own "
        "`PUT /config` (rollback: its own `POST /config/rollback`). The deploy "
        "plane's former write endpoints now return 410 — they rebuilt the "
        "document from a stored schema template, dropping keys the template "
        "did not know and reinstating keys the component had removed.\n\n"
        "## Agent self-restart\n"
        "The chat agent can restart itself via its registered component id:\n"
        "`POST /chat/services/{name}/restart`\n"
        "This is needed after the component roster is updated so the agent "
        "picks up newly registered virtual components."
    )


# ---------------------------------------------------------------------------
# GET /chat/components
# ---------------------------------------------------------------------------


@router.get(
    "/chat/components",
    summary="List components reachable by the chat agent",
    responses={401: {"description": "Unauthorized"}},
)
async def list_chat_components(
    request: Request,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> list[dict[str, Any]]:
    """Return a roster of components the chat agent can interact with.

    Each usable entry has ``id``, ``base_url`` (the internal address),
    ``public_url`` (the edge URL, or ``None`` when the component is not
    routed) and ``skill`` (the Markdown body fetched live from the
    component's ``GET /chat-skill``).  A component whose skill probe fails is
    served from its last-known-good cached skill when one exists
    (stale-while-error), so a transient probe failure does not drop it.

    Components without ``allow_chat_access`` are omitted — that is the
    operator declining, not a fault.  A component the operator *did* grant
    access to but which cannot be used is reported instead of dropped: the
    entry carries ``_error`` and an empty ``skill``, so it is still not
    callable, but the reason travels with the roster rather than living only
    in this server's logs.

    ``base_url`` is internal and never passes through the edge, so a probe
    against it says nothing about whether the component's public URL works.
    That is what ``public_url`` is for.

    Skill bodies are cached for 60 seconds; a component whose cached
    entry has expired is re-probed on the next request.
    """
    results: list[dict[str, Any]] = []
    now = time.monotonic()

    # The public hostname is derived from the same data the edge routes on, so
    # a component reported at a URL is a component the edge actually carries.
    try:
        base_domain = (await _get_config(request)).gateway_base_domain
    except Exception:  # noqa: BLE001 — an unconfigured gateway is not an error here
        base_domain = ""

    for comp_cfg in component_config_store.all():
        if not comp_cfg.allow_chat_access:
            continue
        # Virtual components (with chat_base_url set) don't need ports;
        # Docker components do. A Docker component with none is not reachable
        # at all — no internal address to call and no edge route — so say so
        # instead of leaving it out of the roster entirely.
        if not comp_cfg.chat_base_url and not comp_cfg.ports:
            results.append(
                _excluded(
                    comp_cfg.id,
                    "no port in its deploy contract — the component has no "
                    "address to call and the edge emits no route for it; "
                    "refresh its contract (POST /services/{name}/refresh-contract)",
                )
            )
            continue

        base_url = comp_cfg.chat_base_url or (
            f"http://{comp_cfg.container_name}:{comp_cfg.ports[0].container}"
        )
        skill_endpoint = comp_cfg.chat_skill_endpoint
        component_public_url = public_url(comp_cfg, base_domain)

        # Static skill body — no probing needed.
        if comp_cfg.chat_skill:
            entry: dict[str, Any] = {
                "id": comp_cfg.id,
                "base_url": base_url,
                "public_url": component_public_url,
                "skill": comp_cfg.chat_skill,
            }
            _inject_auth(entry, comp_cfg)
            results.append(entry)
            continue

        # Check cache first
        cached = _skill_cache.get(comp_cfg.id)
        if cached is not None:
            cached_at, cached_body = cached
            if now - cached_at < _SKILL_CACHE_TTL:
                entry = {
                    "id": comp_cfg.id,
                    "base_url": base_url,
                    "public_url": component_public_url,
                    "skill": cached_body,
                }
                _inject_auth(entry, comp_cfg)
                results.append(entry)
                continue

        # Probe the component's chat-skill endpoint
        skill_body: str | None = None
        try:
            async with retry_client_context(timeout=5.0) as client:
                resp = await client.get(f"{base_url}{skill_endpoint}")
            if resp.status_code == 200 and resp.text.strip():
                skill_body = resp.text
                _skill_cache[comp_cfg.id] = (now, skill_body)
            else:
                logger.warning(
                    "chat components: skill probe for %s (%s) returned %s",
                    _sanitize_log(comp_cfg.id),
                    _sanitize_log(base_url),
                    resp.status_code,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "chat components: skill probe failed for %s (%s): %s",
                _sanitize_log(comp_cfg.id),
                _sanitize_log(base_url),
                exc,
            )

        if skill_body is None and cached is not None:
            # Stale-while-error: serve the expired cached skill rather than
            # dropping the component from the roster; the stale timestamp is
            # kept so the next request re-probes.
            skill_body = cached[1]

        if skill_body is not None:
            entry = {
                "id": comp_cfg.id,
                "base_url": base_url,
                "public_url": component_public_url,
                "skill": skill_body,
            }
            _inject_auth(entry, comp_cfg)
            results.append(entry)
        else:
            # Chat access is on, so the operator meant this component to be
            # usable, but it serves no skill and has never been probed
            # successfully. Reporting the reason distinguishes "the operator
            # has not granted access" from "the component does not implement
            # the endpoint", which the caller otherwise cannot tell apart
            # from an absent entry.
            results.append(
                _excluded(
                    comp_cfg.id,
                    f"chat access is enabled but {base_url}{skill_endpoint} "
                    "returned no skill; the component must serve that "
                    "endpoint or declare a static robotsix.deploy.chat-skill "
                    "label in its deploy contract",
                )
            )

    return results
