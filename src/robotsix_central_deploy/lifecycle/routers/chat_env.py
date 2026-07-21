"""Chat agent env/secret provisioning endpoint.

Provides a scoped, audited write surface for the chat agent to set
or rotate secret environment variables on allowlisted services.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import verify_auth
from ..deps import (
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_env_store,
)
from .._config_utils import _sanitize_log
from ._chat_common import (
    _check_rate_limit,
    _require_allowed_service,
    logger,
)
from ..schemas import ChatAgentEnvResponse, ChatAgentEnvUpdate
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore
from ...registry.env_store import EnvStore

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# PUT /chat/env/{name}
# ---------------------------------------------------------------------------


@router.put(
    "/chat/env/{name}",
    response_model=ChatAgentEnvResponse,
    summary="Upsert env vars and secrets for an allowlisted service",
    responses={
        403: {"description": "Service not allowlisted"},
        429: {"description": "Rate limited"},
    },
)
async def chat_upsert_env(
    name: str,
    body: ChatAgentEnvUpdate,
    request: Request,
    env_store: EnvStore = Depends(_get_env_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> ChatAgentEnvResponse:
    """Upsert environment variables and secrets for an allowlisted service.

    Secret values are encrypted at rest via Fernet and are **never**
    logged, echoed in responses, or written to non-secret config.  Every
    write is audited (key name + actor; value redacted).  Secrets become
    available to the target service's container on next restart or
    redeploy.

    Rate-limited to one write per 5 seconds per service.
    """
    await _require_allowed_service(name, component_config_store)
    _check_rate_limit(request.app.state, name, "env_update")

    env = body.env or {}
    secrets = body.secrets or {}

    if not env and not secrets:
        return ChatAgentEnvResponse(
            component=name,
            detail="No env or secret keys submitted; nothing to do.",
        )

    await env_store.upsert(
        name,
        env,
        secrets,
        env_scopes=body.env_scopes,
        secret_scopes=body.secret_scopes,
    )

    # Audit-log each key (secret values are NEVER written to the audit log).
    for key in sorted(env):
        logger.info(
            "Chat agent set env key '%s' on component '%s'",
            _sanitize_log(key),
            _sanitize_log(name),
        )
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action="env_update",
                key=key,
                old_value=None,
                new_value="***",  # never log plaintext env values in audit
                detail="env key upserted (value redacted)",
            )
        )

    for key in sorted(secrets):
        logger.info(
            "Chat agent set secret key '%s' on component '%s'",
            _sanitize_log(key),
            _sanitize_log(name),
        )
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action="env_update",
                key=key,
                old_value=None,
                new_value="***",
                detail="secret key upserted (value redacted)",
            )
        )

    env_keys = sorted(env)
    secret_keys = sorted(secrets)
    return ChatAgentEnvResponse(
        component=name,
        env_keys=env_keys,
        secret_keys=secret_keys,
        detail=(
            f"Upserted {len(env_keys)} env key(s) and "
            f"{len(secret_keys)} secret key(s) for '{name}'."
            if env_keys or secret_keys
            else "No keys changed."
        ),
    )
