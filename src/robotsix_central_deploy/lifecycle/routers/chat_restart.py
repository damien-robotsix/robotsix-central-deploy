"""Chat agent service restart endpoint.

Extracted from chat_services.py — the restart-service handler.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore
from ...registry.loader import ComponentRegistry
from .._config_utils import _sanitize_log
from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import (
    _get_backend,
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_or_create_record,
    _get_registry,
    _get_store,
)
from ..models import ActionType, ServiceState, can_transition
from ..schemas import ChatAgentRestartResponse
from ..store import ServiceStore
from ._chat_common import (
    _check_rate_limit,
    _require_allowed_service,
    logger,
)
from ._sibling_utils import _fanout_siblings_best_effort

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
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),  # noqa: B008
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
    except Exception as exc:  # noqa: BLE001
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
