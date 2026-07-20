"""Central-deploy self-management endpoints.

Exposes:
- ``POST /chat/services/central-deploy/restart`` — restart central-deploy itself
- ``POST /chat/services/central-deploy/update`` — pull + recreate central-deploy itself
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..config import LifecycleConfig
from ..deps import (
    _get_backend,
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_config,
)
from ..models import SelfInspect
from ..schemas import ChatAgentSelfRestartResponse, ChatAgentSelfUpdateResponse
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore

from ._chat_common import (
    _check_rate_limit,
    _require_allowed_service,
)

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# POST /chat/services/central-deploy/restart
# ---------------------------------------------------------------------------


@router.post(
    "/chat/services/central-deploy/restart",
    response_model=ChatAgentSelfRestartResponse,
    status_code=202,
    summary="Restart central-deploy itself (allowlisted)",
    responses={
        403: {"description": "Service not allowlisted"},
        429: {"description": "Rate limited"},
        503: {"description": "Self-restart unsupported (backend or environment)"},
    },
)
async def chat_self_restart(
    request: Request,
    backend: ExecutionBackend = Depends(_get_backend),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> ChatAgentSelfRestartResponse:
    """Restart the central-deploy container itself.

    Returns 202 immediately — the Docker daemon handles the restart
    asynchronously so the response can flush before SIGTERM.
    """
    await _require_allowed_service("central-deploy", component_config_store)
    _check_rate_limit(request.app.state, "central-deploy", "restart")

    self_info: SelfInspect | None = None
    try:
        self_info = await backend.inspect_self()
    except NotImplementedError:
        # non-docker_sdk backends cannot inspect themselves; None triggers 503 below
        self_info = None

    if self_info is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="self-restart requires the docker_sdk backend running inside a container",
        )

    try:
        container_id = await backend.trigger_self_restart(self_info)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await audit_store.append(
        ChatAgentAuditEntry(
            component="central-deploy",
            action="self-restart",
            detail=f"Self-restart triggered for container {container_id}.",
        )
    )

    return ChatAgentSelfRestartResponse(
        container_id=container_id,
    )


# ---------------------------------------------------------------------------
# POST /chat/services/central-deploy/update
# ---------------------------------------------------------------------------


@router.post(
    "/chat/services/central-deploy/update",
    response_model=ChatAgentSelfUpdateResponse,
    status_code=202,
    summary="Pull + recreate central-deploy itself (allowlisted)",
    responses={
        403: {"description": "Service not allowlisted"},
        429: {"description": "Rate limited"},
        503: {"description": "Self-update unsupported (backend or environment)"},
    },
)
async def chat_self_update(
    request: Request,
    backend: ExecutionBackend = Depends(_get_backend),
    config: LifecycleConfig = Depends(_get_config),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> ChatAgentSelfUpdateResponse:
    """Pull the latest image and recreate the central-deploy container itself.

    Launches a one-shot watchtower container that pulls the new image,
    stops the old container, and recreates it — from outside this process,
    which is the only safe way for the server to replace itself.  Returns
    202 immediately.
    """
    await _require_allowed_service("central-deploy", component_config_store)
    _check_rate_limit(request.app.state, "central-deploy", "update")

    self_info: SelfInspect | None = None
    try:
        self_info = await backend.inspect_self()
    except NotImplementedError:
        # non-docker_sdk backends cannot inspect themselves; None triggers 503 below
        self_info = None

    if self_info is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="self-update requires the docker_sdk backend running inside a container",
        )

    try:
        updater_id = await backend.trigger_self_update(
            self_info,
            config.self_update_watchtower_image,
            config.docker_socket_url,
            config.self_update_docker_api_version,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await audit_store.append(
        ChatAgentAuditEntry(
            component="central-deploy",
            action="self-update",
            detail=f"Self-update triggered; updater container {updater_id}.",
        )
    )

    return ChatAgentSelfUpdateResponse(
        updater_container_id=updater_id,
    )
