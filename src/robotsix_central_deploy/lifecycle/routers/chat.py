"""Chat agent component roster endpoint.

Exposes ``GET /chat/components`` so the chat agent can discover
which managed components are reachable and what their chat skill is.
"""

from __future__ import annotations

import logging
import time

import httpx
from fastapi import APIRouter, Depends, Request

from ..auth import verify_auth
from ..deps import _get_component_config_store
from ...registry.config_store import ComponentConfigStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

# Simple TTL cache for skill bodies: {component_id: (timestamp, body)}
_skill_cache: dict[str, tuple[float, str]] = {}
_SKILL_CACHE_TTL: float = 60.0


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
) -> list[dict[str, str]]:
    """Return a roster of components the chat agent can interact with.

    Each entry has ``id``, ``base_url``, and ``skill`` (the Markdown body
    fetched live from the component's ``GET /chat-skill``).  Components
    that do not have ``allow_chat_access`` enabled, or whose skill probe
    returns a non-200 response, are silently omitted.

    Skill bodies are cached for 60 seconds; a component whose cached
    entry has expired is re-probed on the next request.
    """
    results: list[dict[str, str]] = []
    now = time.monotonic()

    for comp_cfg in component_config_store.all():
        if not comp_cfg.allow_chat_access:
            continue
        if not comp_cfg.ports:
            continue

        base_url = f"http://{comp_cfg.container_name}:{comp_cfg.ports[0].container}"

        # Check cache first
        cached = _skill_cache.get(comp_cfg.id)
        if cached is not None:
            cached_at, cached_body = cached
            if now - cached_at < _SKILL_CACHE_TTL:
                results.append(
                    {"id": comp_cfg.id, "base_url": base_url, "skill": cached_body}
                )
                continue

        # Probe the component's chat-skill endpoint
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{base_url}/chat-skill")
            if resp.status_code == 200 and resp.text.strip():
                skill_body = resp.text
                _skill_cache[comp_cfg.id] = (now, skill_body)
                results.append(
                    {"id": comp_cfg.id, "base_url": base_url, "skill": skill_body}
                )
        except Exception:
            logger.debug(
                "chat components: skill probe failed for %s (%s)", comp_cfg.id, base_url
            )

    return results
