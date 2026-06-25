"""FastAPI application — lifecycle REST server.

Endpoints:

* ``GET  /health``                          — liveness probe (no auth).
* ``GET  /services``                        — list all managed services.
* ``GET  /services/{name}``                 — full status for one service.
* ``POST /services/{name}/start``           — start a service (idempotent).
* ``POST /services/{name}/stop``            — stop a service (idempotent).
* ``POST /services/{name}/restart``         — restart a service (idempotent).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .auth import verify_api_key
from .backend import DockerBackend, ExecutionBackend, NoopBackend
from .config import LifecycleConfig
from .models import (
    ActionResponse,
    ErrorDetail,
    ServiceListResponse,
    ServiceRecord,
    ServiceState,
    ServiceStatus,
    can_transition,
)
from .store import FileStore, InMemoryStore, ServiceStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — wire up store & backend from config
# ---------------------------------------------------------------------------

_config: LifecycleConfig | None = None
_store: ServiceStore | None = None
_backend: ExecutionBackend | None = None


def _build_store(cfg: LifecycleConfig) -> ServiceStore:
    if cfg.store_backend == "file":
        return FileStore(cfg.effective_store_path)
    return InMemoryStore()


def _build_backend(cfg: LifecycleConfig) -> ExecutionBackend:
    if cfg.execution_backend == "docker":
        return DockerBackend()
    return NoopBackend()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _store, _backend
    _config = LifecycleConfig()  # type: ignore[call-arg]
    _store = _build_store(_config)
    _backend = _build_backend(_config)
    app.state.config = _config
    app.state.store = _store
    app.state.backend = _backend
    logger.info(
        "lifecycle server starting — store=%s backend=%s auth=%s",
        type(_store).__name__,
        type(_backend).__name__,
        "on" if _config.auth_required else "off",
    )
    yield
    logger.info("lifecycle server shutting down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="robotsix-central-deploy Lifecycle API",
    version="0.1.0",
    description="Start, stop, restart, and inspect suite services.",
    docs_url="/docs",
    openapi_url="/openapi.json",
    lifespan=lifespan,
    responses={
        403: {"model": ErrorDetail, "description": "Forbidden — invalid or missing API key"},
    },
)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def _get_store(request: Request) -> ServiceStore:
    store = request.app.state.store
    assert store is not None, "store not initialised"
    return store


async def _get_backend(request: Request) -> ExecutionBackend:
    backend = request.app.state.backend
    assert backend is not None, "backend not initialised"
    return backend


async def _get_config(request: Request) -> LifecycleConfig:
    config = request.app.state.config
    assert config is not None, "config not initialised"
    return config


async def _get_or_create_record(name: str, store: ServiceStore) -> ServiceRecord:
    """Fetch a service record by name, raising 404 when absent."""
    record = await store.get(name)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service '{name}' not found",
        )
    return record


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# GET /services
# ---------------------------------------------------------------------------


@app.get(
    "/services",
    response_model=ServiceListResponse,
    summary="List managed services",
)
async def list_services(store: ServiceStore = Depends(_get_store)) -> ServiceListResponse:
    records = await store.list_all()
    return ServiceListResponse(
        services=[r.to_list_item() for r in records],
    )


# ---------------------------------------------------------------------------
# GET /services/{name}
# ---------------------------------------------------------------------------


@app.get(
    "/services/{name}",
    response_model=ServiceStatus,
    summary="Get service status",
    responses={404: {"model": ErrorDetail, "description": "Service not found"}},
)
async def get_service_status(
    name: str,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
) -> ServiceStatus:
    record = await _get_or_create_record(name, store)
    # Refresh live state from backend (best-effort).
    live_state = await backend.status(record)
    if live_state != record.state:
        record.state = live_state
        await store.put(record)
    return record.to_status()


# ---------------------------------------------------------------------------
# POST /services/{name}/start
# ---------------------------------------------------------------------------


@app.post(
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
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_api_key),
) -> ActionResponse:
    record = await _get_or_create_record(name, store)
    previous = record.state

    # Idempotency: already running (or starting).
    if record.state == ServiceState.RUNNING:
        return ActionResponse(
            name=name, action="start",
            previous_state=previous, current_state=ServiceState.RUNNING,
            detail="Service is already running",
        )
    if record.state == ServiceState.STARTING:
        return ActionResponse(
            name=name, action="start",
            previous_state=previous, current_state=ServiceState.STARTING,
            detail="Start already in progress",
        )

    # Validate transition.
    if not can_transition(record.state, ServiceState.STARTING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot start from state '{record.state.value}'",
        )

    # Mark starting, then execute.
    record.state = ServiceState.STARTING
    await store.put(record)

    try:
        final_state = await backend.start(record)
    except Exception as exc:
        logger.exception("start %s failed", name)
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Start failed: {exc}",
        )

    record.state = final_state
    record.last_error = "" if final_state == ServiceState.RUNNING else "backend reported failure"
    await store.put(record)

    return ActionResponse(
        name=name, action="start",
        previous_state=previous, current_state=record.state,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/stop
# ---------------------------------------------------------------------------


@app.post(
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
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_api_key),
) -> ActionResponse:
    record = await _get_or_create_record(name, store)
    previous = record.state

    # Idempotency.
    if record.state == ServiceState.STOPPED:
        return ActionResponse(
            name=name, action="stop",
            previous_state=previous, current_state=ServiceState.STOPPED,
            detail="Service is already stopped",
        )
    if record.state == ServiceState.STOPPING:
        return ActionResponse(
            name=name, action="stop",
            previous_state=previous, current_state=ServiceState.STOPPING,
            detail="Stop already in progress",
        )

    if not can_transition(record.state, ServiceState.STOPPING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot stop from state '{record.state.value}'",
        )

    record.state = ServiceState.STOPPING
    await store.put(record)

    try:
        final_state = await backend.stop(record)
    except Exception as exc:
        logger.exception("stop %s failed", name)
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stop failed: {exc}",
        )

    record.state = final_state
    record.last_error = "" if final_state == ServiceState.STOPPED else "backend reported failure"
    await store.put(record)

    return ActionResponse(
        name=name, action="stop",
        previous_state=previous, current_state=record.state,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/restart
# ---------------------------------------------------------------------------


@app.post(
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
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_api_key),
) -> ActionResponse:
    record = await _get_or_create_record(name, store)
    previous = record.state

    # Idempotency — if already restarting, let it continue.
    if record.state == ServiceState.RESTARTING:
        return ActionResponse(
            name=name, action="restart",
            previous_state=previous, current_state=ServiceState.RESTARTING,
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
        logger.exception("restart %s failed", name)
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Restart failed: {exc}",
        )

    record.state = final_state
    record.last_error = "" if final_state == ServiceState.RUNNING else "backend reported failure"
    await store.put(record)

    return ActionResponse(
        name=name, action="restart",
        previous_state=previous, current_state=record.state,
    )


# ---------------------------------------------------------------------------
# Exception handler — structured error responses
# ---------------------------------------------------------------------------


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail if isinstance(exc.detail, str) else str(exc.detail)},
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import uvicorn

    cfg = LifecycleConfig()  # type: ignore[call-arg]
    uvicorn.run(
        "robotsix_central_deploy.lifecycle.server:app",
        host=cfg.host,
        port=cfg.port,
        reload=False,
    )
