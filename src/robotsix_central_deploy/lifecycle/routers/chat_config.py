"""Config endpoints for the chat agent — extracted from chat.py."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import (
    _get_backend,
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_config_yaml_store,
    _get_store,
    _validate_config_or_422,
)
from ..store import ServiceStore
from .._config_utils import (
    _canonical_hash,
    _is_key_secret,
    _mask_secrets,
    _merge_config,
    _restore_secrets_from_current,
    _sanitize_log,
    _strip_secret_values,
)
from ._chat_common import (
    _check_rate_limit,
    _require_allowed_service,
    logger,
)
from ..schemas import (
    ChatAgentConfigRollbackResponse,
    ChatAgentConfigUpdate,
)
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.settings_store import VALID_LOG_LEVELS

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# GET /chat/config/{name}
# ---------------------------------------------------------------------------


@router.get(
    "/chat/config/{name}",
    response_model=ChatAgentConfigRollbackResponse,
    summary="Read the current config for an allowlisted service (secrets redacted)",
    responses={
        403: {"description": "Service not allowlisted"},
        404: {"description": "Service has no config schema"},
    },
)
async def chat_get_config(
    name: str,
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    _auth: None = Depends(verify_auth),
) -> ChatAgentConfigRollbackResponse:
    """Return the current config for an allowlisted service.

    Secret values are redacted — the chat agent sees ``"***"`` for set
    secrets and ``""`` for unset secrets.
    """
    await _require_allowed_service(name, component_config_store)
    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for component '{name}'",
        )
    current_raw = await config_yaml_store.get_current(name)
    if current_raw is None:
        current_raw = _merge_config(template, {}, {})
    masked = _mask_secrets(template, current_raw)
    return ChatAgentConfigRollbackResponse(
        component=name,
        restored=masked,
        detail="Current config (secrets redacted).",
    )


# ---------------------------------------------------------------------------
# PUT /chat/config/{name}
# ---------------------------------------------------------------------------


@router.put(
    "/chat/config/{name}",
    response_model=ChatAgentConfigRollbackResponse,
    summary="Update non-secret config keys for an allowlisted service",
    responses={
        403: {"description": "Service not allowlisted or secret key rejected"},
        404: {"description": "Service has no config schema"},
        429: {"description": "Rate limited"},
    },
)
async def chat_update_config(
    name: str,
    body: ChatAgentConfigUpdate,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ChatAgentConfigRollbackResponse:
    """Update non-secret config keys for an allowlisted service.

    Saves the current config as a rollback snapshot before applying the
    update.  Secret keys are rejected with 403.  Writes the merged config
    to the service's config volume and records an audit entry for every
    changed key.
    """
    await _require_allowed_service(name, component_config_store)
    _check_rate_limit(request.app.state, name, "config_update")

    # ------------------------------------------------------------------
    # log_level is a system-wide setting that the chat agent can raise
    # or lower during troubleshooting.  Apply it immediately to the root
    # logger and strip it from the submitted values so it does not
    # collide with component-level config schema validation.
    # ------------------------------------------------------------------
    if "log_level" in body.values:
        raw_level = str(body.values.pop("log_level")).upper()
        if raw_level not in VALID_LOG_LEVELS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": (
                        f"Unknown log level '{raw_level}'. "
                        f"Valid: {', '.join(sorted(VALID_LOG_LEVELS))}"
                    )
                },
            )
        logging.getLogger().setLevel(raw_level)
        logger.info(
            "Chat agent set log_level to %s via /chat/config/%s",
            _sanitize_log(raw_level),
            _sanitize_log(name),
        )
        if not body.values:
            # Only log_level was submitted — nothing to write to the
            # component config volume.
            return ChatAgentConfigRollbackResponse(
                component=name,
                restored={},
                detail=f"log_level set to {raw_level}; no component config keys changed.",
            )

    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for component '{name}'",
        )

    # Snapshot current config for rollback before mutating.
    existing = await config_yaml_store.get_current(name)
    if existing is not None:
        # Strip secret values from the rollback snapshot so history
        # never persists secrets.  Rollback will restore secrets from
        # the live config when it runs.
        safe_snapshot = _strip_secret_values(template, existing)
        await config_yaml_store.save_previous(name, safe_snapshot)
    else:
        # No current config yet — snapshot the template defaults so the
        # operator can still roll back the first change.
        default_config = _merge_config(template, {}, {})
        safe_default = _strip_secret_values(template, default_config)
        await config_yaml_store.save_previous(name, safe_default)

    # Merge submitted values over existing (or template defaults when no
    # current config exists yet). The chat agent submits PARTIAL updates, so
    # keys absent from the payload must keep their existing values — without
    # prefer_existing_for_unset every unsubmitted key (ports, API keys,
    # integration URLs) is silently reset to its template default.
    try:
        merged = _merge_config(
            template, existing or {}, body.values, prefer_existing_for_unset=True
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": str(exc)},
        )

    _validate_config_or_422(template, merged)

    # Write to volume if available.
    comp_cfg = component_config_store.get(name)
    if comp_cfg and comp_cfg.config_volume:
        await backend.write_config_to_volume(comp_cfg.config_volume, merged)
        new_hash = _canonical_hash(merged)
        await config_yaml_store.update_current_and_hash(name, merged, new_hash)
    else:
        await config_yaml_store.update_current(name, merged)

    # Audit-log each changed key (secret values redacted recursively).
    for key, new_val in body.values.items():
        old_val = existing.get(key) if isinstance(existing, dict) else None
        is_secret = _is_key_secret(template, key)
        # If the value is a nested dict/object, mask any secrets within it
        # so the audit log never records secret plaintext.
        safe_old: Any = old_val
        safe_new: Any = new_val
        if isinstance(old_val, dict):
            safe_old = _mask_secrets(template, {key: old_val}).get(key, old_val)
        if isinstance(new_val, dict):
            safe_new = _mask_secrets(template, {key: new_val}).get(key, new_val)
        if isinstance(old_val, list):
            safe_old = _mask_secrets(template, {key: old_val}).get(key, old_val)
        if isinstance(new_val, list):
            safe_new = _mask_secrets(template, {key: new_val}).get(key, new_val)
        if is_secret:
            safe_old = "***"
            safe_new = "***"
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action="config_update",
                key=key,
                old_value=safe_old,
                new_value=safe_new,
            )
        )

    masked = _mask_secrets(template, merged)
    return ChatAgentConfigRollbackResponse(
        component=name,
        restored=masked,
        detail="Config updated; previous version saved for rollback.",
    )


# ---------------------------------------------------------------------------
# POST /chat/config/{name}/rollback
# ---------------------------------------------------------------------------


@router.post(
    "/chat/config/{name}/rollback",
    response_model=ChatAgentConfigRollbackResponse,
    summary="Restore the previous config version for an allowlisted service",
    responses={
        403: {"description": "Service not allowlisted"},
        404: {"description": "No previous config snapshot available"},
        429: {"description": "Rate limited"},
    },
)
async def chat_rollback_config(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ChatAgentConfigRollbackResponse:
    """Restore the previous config snapshot for an allowlisted service.

    Returns 404 when no previous snapshot exists (e.g. no config update
    has been performed yet through the chat surface).
    """
    await _require_allowed_service(name, component_config_store)
    _check_rate_limit(request.app.state, name, "config_rollback")

    previous = await config_yaml_store.get_previous(name)
    if previous is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No previous config snapshot for component '{name}'.",
        )

    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for component '{name}'",
        )

    _validate_config_or_422(template, previous)

    # Restore current secret values into the snapshot — rollback must not
    # clobber secrets the snapshot doesn't know about.
    current = await config_yaml_store.get_current(name)
    if current:
        previous = _restore_secrets_from_current(template, previous, current)

    # Write the previous config back.
    comp_cfg = component_config_store.get(name)
    if comp_cfg and comp_cfg.config_volume:
        await backend.write_config_to_volume(comp_cfg.config_volume, previous)
        new_hash = _canonical_hash(previous)
        await config_yaml_store.update_current_and_hash(name, previous, new_hash)
    else:
        await config_yaml_store.update_current(name, previous)

    await audit_store.append(
        ChatAgentAuditEntry(
            component=name,
            action="config_rollback",
            detail="Restored previous config snapshot.",
        )
    )

    masked = _mask_secrets(template, previous)
    return ChatAgentConfigRollbackResponse(
        component=name,
        restored=masked,
        detail="Config rolled back to previous snapshot.",
    )
