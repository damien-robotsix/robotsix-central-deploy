"""Shared plumbing for the chat-agent routers.

Contains no route handlers, no domain-specific models or serializers.
Provides:
- ``_inject_auth`` — attach auth metadata to component roster entries
- ``_check_rate_limit`` — per-action rate-limit cooldown guard
- ``_require_allowed_service`` — enforce chat-agent mutatable flag
- ``_RATE_LIMIT_COOLDOWNS`` — cooldown durations per action type
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import HTTPException, status

from ...registry.config_store import ComponentConfigStore
from ...registry.models import ComponentConfig
from .._name_suggest import with_suggestions

logger = logging.getLogger(__name__)

# Rate-limit cooldowns (seconds) per action type.
_RATE_LIMIT_COOLDOWNS: dict[str, float] = {
    "restart": 60.0,
    "update": 300.0,
    "deploy": 300.0,
    "test_deploy": 300.0,
    "disk_reclaim": 300.0,
    "config_update": 5.0,
    "config_rollback": 10.0,
    "env_update": 5.0,
}


# ---------------------------------------------------------------------------
# Auth metadata injection helper
# ---------------------------------------------------------------------------


def _inject_auth(entry: dict[str, Any], comp_cfg: ComponentConfig) -> None:
    """Attach an ``auth`` sub-dict to *entry* when the component carries
    auth metadata the chat agent can use to authenticate requests.

    Metadata only — the scheme and, for header auth, the header name.  The
    chat agent resolves the credential *value* from its own config
    (``central_deploy.api_token``, or a per-component
    ``component_credentials.<id>`` override).  The roster never names an
    environment variable: first-party credentials do not travel that way
    (config-standard §5).
    """
    if not comp_cfg.auth_type:
        return
    auth: dict[str, str] = {"type": comp_cfg.auth_type}
    if comp_cfg.auth_type == "header" and comp_cfg.auth_header_name:
        auth["header_name"] = comp_cfg.auth_header_name
    entry["auth"] = auth


# ---------------------------------------------------------------------------
# Rate limiter helper
# ---------------------------------------------------------------------------


def _check_rate_limit(app_state: Any, service: str, action: str) -> None:
    """Raise HTTP 429 if *action* on *service* is within the cooldown window."""
    cooldown = _RATE_LIMIT_COOLDOWNS.get(action, 30.0)
    key = f"{service}:{action}"
    rate_limits: dict[str, float] = getattr(app_state, "chat_agent_rate_limits", {})
    now = time.monotonic()
    if key in rate_limits and now - rate_limits[key] < cooldown:
        remaining = cooldown - (now - rate_limits[key])
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit: {action} on '{service}' is allowed once every "
                f"{cooldown:.0f}s. Retry in {remaining:.1f}s."
            ),
        )
    rate_limits[key] = now


# ---------------------------------------------------------------------------
# Service allowlist guard
# ---------------------------------------------------------------------------


async def _require_allowed_service(
    name: str,
    component_config_store: ComponentConfigStore,
    action: str = "mutate",
) -> None:
    """Raise HTTP 403 when *name* is not chat-agent accessible.

    A component is accessible when either its ``chat_agent_mutatable`` flag
    (set declaratively via docker-compose label or programmatically at
    seed time) or its ``allow_chat_access`` flag (the operator-facing
    "Allow chat agent access" toggle) is enabled.  Either flag alone is
    sufficient to grant access.

    Virtual components are never mutatable (they have no Docker containers
    to restart/deploy).

    The *action* parameter customises the detail message — use
    ``"mutate"`` (default) for write endpoints and ``"access"`` for
    read-only endpoints so the chat agent receives a clear signal about
    which gate denied it.

    Raises:
        HTTP 404 when the component is not known at all — with a
            ``did you mean …?`` clause when a registered id is close.
        HTTP 403 when the component exists but is not chat-agent accessible.
    """
    comp_cfg = component_config_store.get(name)
    if comp_cfg is None:
        # Same phrasing as the operator plane's 404 (``_get_or_create_record``):
        # the caller is usually addressing a component by its repository name
        # while it is registered under a short one.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=with_suggestions(
                f"Service '{name}' not found",
                name,
                (c.id for c in component_config_store.all()),
            ),
        )
    if not (comp_cfg.chat_agent_mutatable or comp_cfg.allow_chat_access):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Chat agent is not permitted to {action} service '{name}'.",
        )
