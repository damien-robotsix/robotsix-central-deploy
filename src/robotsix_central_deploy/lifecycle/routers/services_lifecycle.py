"""Service lifecycle-action endpoints (start / stop / restart)."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status

from ...registry.loader import ComponentRegistry
from .._config_utils import _sanitize_log
from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import (
    _get_backend,
    _get_or_create_record,
    _get_registry,
    _get_store,
)
from ..models import (
    ActionResponse,
    ActionType,
    ErrorDetail,
    ServiceState,
    can_transition,
)
from ..store import ServiceStore
from ._sibling_utils import _fanout_siblings_best_effort

logger = logging.getLogger(__name__)

router = APIRouter(tags=["services"])


# ---------------------------------------------------------------------------
# Shared lifecycle-action helper (start / stop / restart)
# ---------------------------------------------------------------------------


async def _lifecycle_action(
    name: str,
    store: ServiceStore,
    backend: ExecutionBackend,
    registry: ComponentRegistry,
    action_type: Literal["start", "stop", "restart"],
    state_starting: ServiceState,
    state_running: ServiceState,
    state_stopping: ServiceState | None = None,
) -> ActionResponse:
    """Shared implementation for start, stop, and restart endpoints.

    Args:
        name: Service name.
        store: Service store backend.
        backend: Execution backend.
        registry: Component registry.
        action_type: The action being performed ("start", "stop", "restart").
        state_starting: The intermediate/in-progress state
            (STARTING, STOPPING, RESTARTING).
        state_running: The success target state (RUNNING, STOPPED, RUNNING).
        state_stopping: Optional "already at target" state for idempotency
            (RUNNING for start, STOPPED for stop, None for restart).
    """
    record = await _get_or_create_record(name, store)
    previous = record.state

    # Idempotency: already at target state (if applicable).
    if state_stopping is not None and record.state == state_stopping:
        return ActionResponse(
            name=name,
            action=ActionType(action_type),
            previous_state=previous,
            current_state=state_stopping,
            detail=f"Service is already {state_stopping.value}",
        )

    # Idempotency: already in progress.
    if record.state == state_starting:
        return ActionResponse(
            name=name,
            action=ActionType(action_type),
            previous_state=previous,
            current_state=state_starting,
            detail=f"{action_type.capitalize()} already in progress",
        )

    # Validate transition.
    if not can_transition(record.state, state_starting):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot {action_type} from state '{record.state.value}'",
        )

    # Mark intermediate state, then execute.
    record.state = state_starting
    await store.put(record)

    try:
        final_state = await getattr(backend, action_type)(record)
    except Exception as exc:
        logger.exception("%s %s failed", action_type, _sanitize_log(name))
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{action_type.capitalize()} failed: {exc}",
        )

    record.state = final_state
    record.last_error = (
        "" if final_state == state_running else "backend reported failure"
    )
    await store.put(record)

    # Fan out to siblings (best-effort per sibling)
    config = registry.get(name)
    if config and config.siblings:
        await _fanout_siblings_best_effort(name, config, store, backend, action_type)

    return ActionResponse(
        name=name,
        action=ActionType(action_type),
        previous_state=previous,
        current_state=record.state,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/start
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/start",
    response_model=ActionResponse,
    summary="Start a service (idempotent)",
    responses={
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail, "description": "Already in requested state"},
    },
)
async def start_service(
    name: str,
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ActionResponse:
    """Start a service. Idempotent — returns success if already running or starting.

    Transitions the service through STARTING to RUNNING (or FAILED on error).
    Raises 404 on missing service, 409 if the current state does not allow a
    start, and 500 on backend failure. Sibling services are started on a
    best-effort basis.
    """
    return await _lifecycle_action(
        name=name,
        store=store,
        backend=backend,
        registry=registry,
        action_type="start",
        state_starting=ServiceState.STARTING,
        state_running=ServiceState.RUNNING,
        state_stopping=ServiceState.RUNNING,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/stop
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/stop",
    response_model=ActionResponse,
    summary="Stop a service (idempotent)",
    responses={
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail},
    },
)
async def stop_service(
    name: str,
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ActionResponse:
    """Stop a service. Idempotent — returns success if already stopped or stopping.

    Transitions the service through STOPPING to STOPPED (or FAILED on error).
    Raises 404 on missing service, 409 if the current state does not allow a
    stop, and 500 on backend failure. Sibling services are stopped on a
    best-effort basis.
    """
    return await _lifecycle_action(
        name=name,
        store=store,
        backend=backend,
        registry=registry,
        action_type="stop",
        state_starting=ServiceState.STOPPING,
        state_running=ServiceState.STOPPED,
        state_stopping=ServiceState.STOPPED,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/restart
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/restart",
    response_model=ActionResponse,
    summary="Restart a service (idempotent)",
    responses={
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail},
    },
)
async def restart_service(
    name: str,
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ActionResponse:
    """Restart a service. Idempotent — returns success if a restart is already in progress.

    Transitions the service through RESTARTING to RUNNING (or FAILED on error).
    Raises 404 on missing service, 409 if the current state does not allow a
    restart, and 500 on backend failure. Sibling services are restarted on a
    best-effort basis.
    """
    return await _lifecycle_action(
        name=name,
        store=store,
        backend=backend,
        registry=registry,
        action_type="restart",
        state_starting=ServiceState.RESTARTING,
        state_running=ServiceState.RUNNING,
    )
