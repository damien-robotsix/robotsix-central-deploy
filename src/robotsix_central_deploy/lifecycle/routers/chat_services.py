"""Chat agent service restart/update/deploy endpoints.

Extracted from chat.py — these are the scoped write endpoints that
allow the chat agent to restart, update (pull + recreate), or
generically deploy an allowlisted service.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import (
    _get_backend,
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_config,
    _get_env_store,
    _get_or_create_record,
    _get_registry,
    _get_sibling_pairs,
    _get_store,
)
from ...registry.models import HealthCheck
from .._config_utils import _sanitize_log
from ._chat_common import (
    _check_rate_limit,
    _require_allowed_service,
    logger,
)
from ._sibling_utils import _fanout_siblings_best_effort
from ..deploy_lock import release_deploy_lock, try_acquire_deploy_lock
from ..models import ActionType, ServiceRecord, ServiceState, can_transition
from ..schemas import (
    ChatAgentDeployRequest,
    ChatAgentDeployResponse,
    ChatAgentRestartResponse,
    ChatAgentUpdateResponse,
)
from ..store import ServiceStore
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig, PortMapping

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


# ---------------------------------------------------------------------------
# POST /chat/deploy
# ---------------------------------------------------------------------------


def _build_minimal_config(body: ChatAgentDeployRequest) -> ComponentConfig:
    """Build a minimal ``ComponentConfig`` from a generic deploy request.

    Used when no persisted config exists for the component yet.
    """
    ports: list[PortMapping] = []
    health_check: HealthCheck | None = None
    if body.container_port is not None:
        ports.append(
            PortMapping(host=body.container_port, container=body.container_port)
        )
        # Provide a sensible default HTTP health check so the deploy
        # flow can verify the container is ready before returning.
        health_check = HealthCheck(
            test=["CMD", "curl", "-f", f"http://localhost:{body.container_port}/"],
            interval_seconds=30,
            timeout_seconds=10,
            retries=3,
            start_period_seconds=10,
        )
    return ComponentConfig(
        id=body.name,
        image=body.image,
        container_name=f"robotsix-{body.name}",
        ports=ports,
        health_check=health_check,
        chat_agent_mutatable=True,
    )


@router.post(
    "/chat/deploy",
    response_model=ChatAgentDeployResponse,
    summary="Generic deploy: pull + recreate an allowlisted component",
    responses={
        403: {"description": "Component not in the deploy allowlist"},
        409: {"description": "Deploy already in progress"},
        429: {"description": "Rate limited"},
        503: {"description": "Registry not loaded"},
    },
)
async def chat_deploy(
    body: ChatAgentDeployRequest,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> ChatAgentDeployResponse:
    """Pull the latest image and recreate a container for an allowlisted component.

    The component does NOT need a pre-existing ``ComponentConfig`` —
    when absent a minimal config is derived from the request body.
    Access is gated by the ``chat_agent_deployable_components`` server-
    level allowlist (``LifecycleConfig``).

    Synchronous — waits for the deploy to complete before returning.
    Rate-limited to one deploy per 300 seconds per component.
    """
    config = await _get_config(request)
    if body.name not in config.chat_agent_deployable_components:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Component '{body.name}' is not in the deploy allowlist.",
        )

    _check_rate_limit(request.app.state, body.name, "deploy")

    # Resolve or build the ComponentConfig.
    comp_cfg = component_config_store.get(body.name)
    if comp_cfg is None:
        comp_cfg = _build_minimal_config(body)
        # Persist the minimal config so future deploys (and sibling
        # fan-out, dashboard, etc.) can reference it.
        await component_config_store.put(comp_cfg)
        # Register in the in-memory loader so the gateway can route to it.
        registry.register(comp_cfg)

    # Merge env overrides and secrets from the EnvStore (same as
    # chat_update_service and the main deploy flow) so operator-
    # configured secrets are injected into the container.
    env_store = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(body.name, comp_cfg.env)
    comp_cfg = comp_cfg.model_copy(update={"env": merged_env})

    # Get or create the service record.
    record = await store.get(body.name)
    if record is None:
        record = ServiceRecord(name=body.name)
        await store.put(record)

    # Serialise concurrent deploys.
    if not await try_acquire_deploy_lock(body.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Deploy already in progress for '{body.name}'.",
        )

    try:
        outcome = await backend.deploy(record, comp_cfg, body.image)
    except Exception as exc:
        logger.exception("chat deploy %s failed", _sanitize_log(body.name))
        await audit_store.append(
            ChatAgentAuditEntry(
                component=body.name,
                action="deploy",
                detail=f"Deploy failed: {exc}",
            )
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Deploy failed: {exc}",
        )
    finally:
        release_deploy_lock(body.name)

    record.state = outcome.state
    record.image = body.image
    record.deployed_image_digest = outcome.deployed_digest
    record.previous_image_digest = outcome.previous_digest
    await store.put(record)

    # Update the persisted ComponentConfig.image so future dashboard-
    # initiated deploys use the correct image reference.
    if comp_cfg.image != body.image:
        comp_cfg.image = body.image
        await component_config_store.put(comp_cfg)

    await audit_store.append(
        ChatAgentAuditEntry(
            component=body.name,
            action="deploy",
            detail=(
                f"Deployed {outcome.deployed_digest[:19]}… "
                f"(previous: {outcome.previous_digest[:19]}…) "
                f"→ {outcome.state.value}"
            ),
        )
    )

    return ChatAgentDeployResponse(
        name=body.name,
        deployed_digest=outcome.deployed_digest,
        previous_digest=outcome.previous_digest,
        current_state=outcome.state.value,
        detail="Deploy completed.",
    )
