"""Component roster and chat-skill endpoints for the chat agent.

Exposes:
- ``GET /chat-skill`` — the deploy server's own chat-agent skill description
- ``GET /chat/components`` — list components reachable by the chat agent
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request

from ..auth import verify_auth
from ..deps import _get_component_config_store
from .._config_utils import _sanitize_log
from ...registry.config_store import ComponentConfigStore

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
        "this key from the `DEPLOY_API_KEY` environment variable (injected "
        "by the deploy server itself at container start).\n\n"
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
        "- `GET /chat/langfuse/{project}/observations/{observationId}` — single observation detail\n\n"
        "## Scoped write endpoints (chat-agent allowlisted)\n"
        "- `PUT /chat/config/{name}` — update non-secret config keys\n"
        "- `POST /chat/config/{name}/rollback` — restore previous config version\n"
        "- `POST /chat/services/{name}/restart` — restart a service\n"
        "- `POST /chat/services/{name}/update` — pull + recreate (deploy) a service\n\n"
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
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
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
            async with httpx.AsyncClient(timeout=5.0) as client:
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
        except Exception as exc:
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
