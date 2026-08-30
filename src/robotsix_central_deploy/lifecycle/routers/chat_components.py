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
from .._config_utils import _sanitize_log
from ..auth import verify_auth
from ..deps import _get_component_config_store
from ._chat_common import (
    _inject_auth,
    logger,
)

# Simple TTL cache for skill bodies: {component_id: (timestamp, body)}
_skill_cache: dict[str, tuple[float, str]] = {}
_SKILL_CACHE_TTL: float = 60.0

router = APIRouter(tags=["chat"])


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
        "- `GET /chat/services/{name}/diagnose` — full diagnostic report: stored spec vs repo contract, routing labels, edge probe, runtime state, and verdict (per-component gate)\n"
        "- `GET /chat/services/{name}/volumes` — list named volumes for a service (per-component gate)\n"
        "- `GET /chat/services/{name}/volumes/{vol}/files?path=…` — read-only file inspection within a volume (per-component gate)\n\n"
        "## Scoped write endpoints (chat-agent allowlisted)\n"
        "- `PUT /chat/services/{name}/volumes/{vol}/files` — create or overwrite a file "
        "within a volume (JSON body `{path, content, overwrite}`; create-only unless "
        "`overwrite: true`, 1 MiB default cap, per-component gate)\n"
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

    Each entry has ``id``, ``base_url``, and ``skill`` (the Markdown body
    fetched live from the component's ``GET /chat-skill``).  Components
    that do not have ``allow_chat_access`` enabled are omitted.  A
    component whose skill probe fails is served from its last-known-good
    cached skill when one exists (stale-while-error), so a transient
    probe failure does not drop it from the roster; it is omitted only
    when it has never been probed successfully.

    Skill bodies are cached for 60 seconds; a component whose cached
    entry has expired is re-probed on the next request.
    """
    results: list[dict[str, Any]] = []
    now = time.monotonic()

    for comp_cfg in component_config_store.all():
        if not comp_cfg.allow_chat_access:
            continue
        # Virtual components (with chat_base_url set) don't need ports;
        # Docker components do.
        if not comp_cfg.chat_base_url and not comp_cfg.ports:
            continue

        base_url = comp_cfg.chat_base_url or (
            f"http://{comp_cfg.container_name}:{comp_cfg.ports[0].container}"
        )
        skill_endpoint = comp_cfg.chat_skill_endpoint

        # Static skill body — no probing needed.
        if comp_cfg.chat_skill:
            entry: dict[str, Any] = {
                "id": comp_cfg.id,
                "base_url": base_url,
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
                "skill": skill_body,
            }
            _inject_auth(entry, comp_cfg)
            results.append(entry)

    return results
