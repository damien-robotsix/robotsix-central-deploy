"""Chat agent service restart/update endpoints.

Extracted from chat.py — these are the scoped write endpoints that
allow the chat agent to restart or update (pull + recreate) an
allowlisted service.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import (
    _get_backend,
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_env_store,
    _get_or_create_record,
    _get_registry,
    _get_sibling_pairs,
    _get_store,
)
from .._config_utils import _sanitize_log
from ._chat_common import (
    _check_rate_limit,
    _require_allowed_service,
    logger,
)
from ._sibling_utils import _fanout_siblings_best_effort
from ..deploy_lock import release_deploy_lock, try_acquire_deploy_lock
from ..models import ActionType, ServiceState, can_transition
from ..schemas import ChatAgentRestartResponse, ChatAgentUpdateResponse
from ..store import ServiceStore
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore
from ...registry.loader import ComponentRegistry

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# POST /chat/services/{name}/restart
# ---------------------------------------------------------------------------


@router.post(
    "/chat/services/{name}/restart",
    response_model=ChatAgentRestartResponse,
    summary="Restart an allowlisted service (idempotent)",
    responses={
        403: {"description": "Service not allowlisted"},
        404: {"description": "Service not found"},
        409: {"description": "Invalid state transition"},
        429: {"description": "Rate limited"},
    },
)
async def chat_restart_service(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> ChatAgentRestartResponse:
    """Restart an allowlisted service. Idempotent.

    Raises 403 if the service is not in the chat-agent allowlist.
    Rate-limited to one restart per 60 seconds per service.
    """
    await _require_allowed_service(name, component_config_store)
    _check_rate_limit(request.app.state, name, "restart")

    record = await _get_or_create_record(name, store)
    previous = record.state

    if record.state == ServiceState.RESTARTING:
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action=ActionType.RESTART,
                detail="Restart already in progress.",
            )
        )
        return ChatAgentRestartResponse(
            name=name,
            previous_state=previous.value,
            current_state=ServiceState.RESTARTING.value,
            detail="Restart already in progress",
        )

    if not can_transition(record.state, ServiceState.RESTARTING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot restart from state '{record.state.value}'",
        )

    record.state = ServiceState.RESTARTING
    await store.put(record)

    try:
        final_state = await backend.restart(record)
    except Exception as exc:
        logger.exception("chat restart %s failed", _sanitize_log(name))
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action=ActionType.RESTART,
                detail=f"Restart failed: {exc}",
            )
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Restart failed: {exc}",
        )

    record.state = final_state
    record.last_error = (
        "" if final_state == ServiceState.RUNNING else "backend reported failure"
    )
    await store.put(record)

    # Restart siblings (best-effort).
    config = registry.get(name)
    if config and config.siblings:
        await _fanout_siblings_best_effort(name, config, store, backend, "restart")

    await audit_store.append(
        ChatAgentAuditEntry(
            component=name,
            action=ActionType.RESTART,
            detail=f"Restarted: {previous.value} → {final_state.value}",
        )
    )

    return ChatAgentRestartResponse(
        name=name,
        previous_state=previous.value,
        current_state=record.state.value,
    )


# ---------------------------------------------------------------------------
# POST /chat/services/{name}/update
# ---------------------------------------------------------------------------


@router.post(
    "/chat/services/{name}/update",
    response_model=ChatAgentUpdateResponse,
    summary="Pull + recreate (deploy) an allowlisted service",
    responses={
        403: {"description": "Service not allowlisted"},
        404: {"description": "Service not found"},
        409: {"description": "Deploy already in progress"},
        429: {"description": "Rate limited"},
        503: {"description": "Registry not loaded"},
    },
)
async def chat_update_service(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> ChatAgentUpdateResponse:
    """Pull the latest image and recreate the container for an allowlisted service.

    Synchronous — waits for the deploy to complete before returning.
    Rate-limited to one update per 300 seconds per service.
    """
    await _require_allowed_service(name, component_config_store)
    _check_rate_limit(request.app.state, name, "update")

    record = await _get_or_create_record(name, store)

    config = registry.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config for '{name}'.",
        )

    # Merge env overrides from the env store (same as the main deploy endpoint).
    env_store = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(name, config.env)
    config = config.model_copy(update={"env": merged_env})

    # Serialise concurrent deploys.
    if not await try_acquire_deploy_lock(name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Deploy already in progress for '{name}'.",
        )

    try:
        outcome = await backend.deploy(record, config, config.image)
    except Exception as exc:
        logger.exception("chat update %s failed", _sanitize_log(name))
        await audit_store.append(
            ChatAgentAuditEntry(
                component=name,
                action="update",
                detail=f"Update failed: {exc}",
            )
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Update failed: {exc}",
        )
    finally:
        release_deploy_lock(name)

    record.state = outcome.state
    record.image = config.image
    record.deployed_image_digest = outcome.deployed_digest
    record.previous_image_digest = outcome.previous_digest
    await store.put(record)

    # Deploy siblings (best-effort) so the whole component group converges.
    updated_siblings: list[str] = []
    if config.siblings:
        for sib_cfg, sib_record in await _get_sibling_pairs(name, config, store):
            sib_name = f"{name}-{sib_cfg.service_key}"
            try:
                sib_merged_env = await env_store.get_merged_env(sib_name, sib_cfg.env)
                sib_effective = config.model_copy(
                    update={
                        "id": sib_name,
                        "image": sib_cfg.image,
                        "container_name": sib_cfg.container_name,
                        "ports": sib_cfg.ports,
                        "mounts": sib_cfg.mounts,
                        "env": sib_merged_env,
                        "health_check": sib_cfg.health_check,
                        "claude_mount": sib_cfg.claude_mount,
                        "claude_mount_path": sib_cfg.claude_mount_path,
                        "host_docker_sock": sib_cfg.host_docker_sock,
                        "named_volumes": [m.host for m in sib_cfg.mounts],
                        "command": sib_cfg.command,
                        "entrypoint": sib_cfg.entrypoint,
                        "tmpfs": sib_cfg.tmpfs,
                        "mem_limit": sib_cfg.mem_limit,
                        "user": sib_cfg.user,
                    }
                )
                sib_outcome = await backend.deploy(
                    sib_record, sib_effective, sib_cfg.image
                )
                sib_record.state = sib_outcome.state
                sib_record.image = sib_cfg.image
                sib_record.deployed_image_digest = sib_outcome.deployed_digest
                sib_record.previous_image_digest = sib_outcome.previous_digest
                await store.put(sib_record)
                updated_siblings.append(sib_name)
            except Exception:
                logger.warning(
                    "chat update: deploy sibling '%s' failed",
                    _sanitize_log(sib_name),
                )

    await audit_store.append(
        ChatAgentAuditEntry(
            component=name,
            action="update",
            detail=(
                f"Deployed {outcome.deployed_digest[:19]}… "
                f"(previous: {outcome.previous_digest[:19]}…) "
                f"→ {outcome.state.value}"
                + (
                    f"; siblings: {', '.join(updated_siblings)}"
                    if updated_siblings
                    else ""
                )
            ),
        )
    )

    return ChatAgentUpdateResponse(
        name=name,
        deployed_digest=outcome.deployed_digest,
        previous_digest=outcome.previous_digest,
        current_state=outcome.state.value,
        detail="Update completed."
        + (f" Also updated: {', '.join(updated_siblings)}" if updated_siblings else ""),
        updated_siblings=updated_siblings,
    )
