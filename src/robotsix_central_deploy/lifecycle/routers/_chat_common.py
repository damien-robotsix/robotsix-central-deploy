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

logger = logging.getLogger(__name__)

# Rate-limit cooldowns (seconds) per action type.
_RATE_LIMIT_COOLDOWNS: dict[str, float] = {
    "restart": 60.0,
    "update": 300.0,
    "config_update": 5.0,
    "config_rollback": 10.0,
}


# ---------------------------------------------------------------------------
# Auth metadata injection helper
# ---------------------------------------------------------------------------


def _inject_auth(entry: dict[str, Any], comp_cfg: ComponentConfig) -> None:
    """Attach an ``auth`` sub-dict to *entry* when the component carries
    auth metadata the chat agent can use to authenticate requests.

    The auth dict references **environment variable names** — the chat
    agent resolves them at runtime from its own container environment
    (which the deploy server populates via the EnvStore at deploy time).
    Actual credential values are never embedded in the roster response.
    """
    if not comp_cfg.auth_type:
        return
    auth: dict[str, str] = {"type": comp_cfg.auth_type}
    if comp_cfg.auth_type == "basic":
        if comp_cfg.auth_username_env:
            auth["username_env"] = comp_cfg.auth_username_env
        if comp_cfg.auth_password_env:
            auth["password_env"] = comp_cfg.auth_password_env
    elif comp_cfg.auth_type == "header":
        if comp_cfg.auth_header_name:
            auth["header_name"] = comp_cfg.auth_header_name
        if comp_cfg.auth_token_env:
            auth["token_env"] = comp_cfg.auth_token_env
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
    name: str, component_config_store: ComponentConfigStore
) -> None:
    """Raise HTTP 403 when *name* is not chat-agent mutatable.

    A component is mutatable when its ``chat_agent_mutatable`` flag is set
    in the component config — this is a declarative, per-service setting,
    not a hard-coded allowlist.  Virtual components are never mutatable
    (they have no Docker containers to restart/deploy).

    This single flag gates **restart**, **config-write**, and
    **config-rollback** together — restart access implies config-write
    access.  ``update`` (self-deploy) is a separate, more sensitive
    capability that is NOT implicitly granted by this flag.
    """
    comp_cfg = component_config_store.get(name)
    if comp_cfg is None or not comp_cfg.chat_agent_mutatable:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Chat agent is not permitted to mutate service '{name}'.",
        )
