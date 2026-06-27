"""FastAPI application — lifecycle REST server.

Endpoints:

* ``GET  /health``                          — liveness probe (no auth).
* ``GET  /services``                        — list all managed services.
* ``GET  /services/{name}``                 — full status for one service.
* ``GET  /services/{name}/health``           — health status for one service (auth-gated).
* ``GET  /services/{name}/logs``            — stream container logs (auth-gated).
* ``POST /services/{name}/start``           — start a service (idempotent).
* ``POST /services/{name}/stop``            — stop a service (idempotent).
* ``POST /services/{name}/restart``         — restart a service (idempotent).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.params import Body
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .auth import verify_auth
from .backend import DockerBackend, DockerSdkBackend, ExecutionBackend, NoopBackend
from .config import LifecycleConfig
from .models import (
    ActionResponse,
    DeployRequest,
    DeployResponse,
    DiskUsageResponse,
    ErrorDetail,
    RollbackResponse,
    ServiceHealthResponse,
    ServiceListResponse,
    ServiceRecord,
    ServiceState,
    ServiceStatus,
    can_transition,
)
from ..registry.config_store import ComponentConfigStore
from ..registry.env_store import EnvStore
from ..registry.loader import ComponentRegistry
from ..registry.models import ComponentConfig
from ..registry.secret_key import SecretKeyManager
from ..registry_check import RegistryChecker
from ..ui.router import router as ui_router
from .store import FileStore, InMemoryStore, ServiceStore

logger = logging.getLogger(__name__)

#: Module-level registry checker (set by lifespan, used by endpoints).
_registry_checker: RegistryChecker | None = None
_http_client: httpx.AsyncClient | None = None


# ---------------------------------------------------------------------------
# Onboard request / response models
# ---------------------------------------------------------------------------


from robotsix_central_deploy.onboard.models import DerivedSpec  # noqa: E402


class OnboardPreflightRequest(BaseModel):
    git_url: str
    name: str  # validated: ^[a-z0-9][a-z0-9-]*$


class OnboardPreflightResponse(BaseModel):
    spec: DerivedSpec


class OnboardConfirmRequest(BaseModel):
    spec: DerivedSpec  # env values now user-filled


class OnboardConfirmResponse(BaseModel):
    name: str
    image: str
    state: str


# ---------------------------------------------------------------------------
# Env endpoint models
# ---------------------------------------------------------------------------


class EnvResponse(BaseModel):
    env: dict[str, str]
    secrets: dict[str, str]  # values are always "***"


class EnvUpdate(BaseModel):
    env: dict[str, str] = {}
    secrets: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Background registry-check loop
# ---------------------------------------------------------------------------


async def _registry_check_loop(
    store: ServiceStore,
    checker: RegistryChecker,
    backend: ExecutionBackend,
    interval_sec: int,
) -> None:
    """Periodically poll the registry for every managed service and
    update ``update_available`` / ``latest_registry_digest``."""
    try:
        while True:
            await asyncio.sleep(interval_sec)
            records = await store.list_all()
            for record in records:
                # Refresh running_digest from Docker if unknown
                if record.image and not record.deployed_image_digest:
                    try:
                        ins = await backend.status(record)
                        if ins.running_digest:
                            record.deployed_image_digest = ins.running_digest
                            await store.put(record)
                    except Exception:
                        pass

                if not record.image or not record.deployed_image_digest:
                    continue
                try:
                    latest = await checker.get_latest_digest(record.image)
                    if latest is not None:
                        new_ua = latest != record.deployed_image_digest
                        if (
                            record.update_available != new_ua
                            or record.latest_registry_digest != latest
                        ):
                            record.update_available = new_ua
                            record.latest_registry_digest = latest
                            await store.put(record)
                except Exception:  # noqa: BLE001
                    pass
    except asyncio.CancelledError:
        pass


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
    if cfg.execution_backend == "docker_sdk":
        return DockerSdkBackend(socket_url=cfg.docker_socket_url)
    if cfg.execution_backend == "docker":
        return DockerBackend()
    return NoopBackend()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _store, _backend, _registry_checker, _http_client
    _config = LifecycleConfig()  # type: ignore[call-arg]
    _store = _build_store(_config)
    _backend = _build_backend(_config)
    _key_manager = SecretKeyManager(Path(_config.secret_key_path))
    _env_store = EnvStore(Path(_config.env_store_path), _key_manager)
    app.state.config = _config
    app.state.store = _store
    app.state.backend = _backend
    app.state.key_manager = _key_manager
    app.state.env_store = _env_store

    # -- System settings store (MUST come before RegistryChecker so that
    #    the checker sees the overlaid ghcr_token) ------------------------
    from ..registry.settings_store import SystemSettingsStore

    settings_store = SystemSettingsStore(_config.effective_system_settings_path)
    app.state.settings_store = settings_store
    _config = settings_store.overlay(_config)          # returns new LifecycleConfig (or same if no file)
    app.state.config = _config                         # replace with overlaid version

    # Apply log_level from (possibly overlaid) config
    logging.getLogger().setLevel(_config.log_level)

    # -- Registry checker ------------------------------------------------
    http_client = httpx.AsyncClient(timeout=10.0)
    registry_checker = RegistryChecker(
        http_client,
        ghcr_token=_config.ghcr_token,
        ttl_seconds=_config.registry_check_ttl,
    )
    app.state.registry_checker = registry_checker
    _registry_checker = registry_checker
    _http_client = http_client

    bg_task = None
    if _config.registry_check_interval > 0:
        bg_task = asyncio.create_task(
            _registry_check_loop(
                _store, registry_checker, _backend, _config.registry_check_interval,
            )
        )

    logger.info(
        "lifecycle server starting — store=%s backend=%s auth=%s",
        type(_store).__name__,
        type(_backend).__name__,
        "on" if _config.auth_required else "off",
    )

    # -- Component registry (in-memory, populated from persisted store) ------
    registry = ComponentRegistry([])
    app.state.registry = registry

    # -- Dynamic component config store ------------------------------------
    store_path: Path = _config.effective_component_config_store_path
    component_config_store = ComponentConfigStore(store_path)
    app.state.component_config_store = component_config_store

    # Merge dynamic store into in-memory registry (unchanged logic)
    for dyn_config in component_config_store.all():
        registry.register(dyn_config)
        existing = await _store.get(dyn_config.id)
        if existing is None:
            await _store.put(ServiceRecord(
                name=dyn_config.id,
                container_name=dyn_config.container_name,
                image=dyn_config.image,
            ))
        logger.info("Loaded dynamic component config for '%s'", dyn_config.id)

    yield

    if bg_task:
        bg_task.cancel()
        await asyncio.gather(bg_task, return_exceptions=True)
    await http_client.aclose()
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
        401: {"model": ErrorDetail, "description": "Unauthorized — invalid or missing credentials"},
    },
)

app.include_router(ui_router)

from .settings_router import settings_router
app.include_router(settings_router)

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


async def _get_registry(request: Request) -> ComponentRegistry:
    """Return the ComponentRegistry from app state."""
    return request.app.state.registry


def _get_registry_checker(request: Request) -> RegistryChecker:
    return request.app.state.registry_checker


async def _get_component_config_store(request: Request) -> ComponentConfigStore:
    return request.app.state.component_config_store


async def _get_env_store(request: Request) -> EnvStore:
    return request.app.state.env_store


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
# GET /disk
# ---------------------------------------------------------------------------


@app.get("/disk", response_model=DiskUsageResponse)
async def get_disk_usage(
    _auth: None = Depends(verify_auth),
    backend: ExecutionBackend = Depends(_get_backend),
    config: LifecycleConfig = Depends(_get_config),
) -> DiskUsageResponse:
    """Host disk usage and Docker storage breakdown."""
    usage = shutil.disk_usage(config.disk_path)
    docker_df = await backend.disk_df()
    return DiskUsageResponse(
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        warn_threshold_bytes=config.disk_warn_bytes,
        docker=docker_df,
    )


# ---------------------------------------------------------------------------
# GET /services
# ---------------------------------------------------------------------------


@app.get(
    "/services",
    response_model=ServiceListResponse,
    summary="List managed services",
)
async def list_services(
    store: ServiceStore = Depends(_get_store),
    _auth: None = Depends(verify_auth),
) -> ServiceListResponse:
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
    request: Request,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ServiceStatus:
    record = await _get_or_create_record(name, store)
    # Refresh live state from backend (best-effort).
    inspect = await backend.status(record)
    changed = (
        inspect.state != record.state
        or inspect.image_revision != record.image_revision
        or inspect.health != record.health
    )
    if changed:
        record.state = inspect.state
        record.image_revision = inspect.image_revision
        record.health = inspect.health
        await store.put(record)

    # Persist running_digest from image inspect if available
    if inspect.running_digest and inspect.running_digest != record.deployed_image_digest:
        record.deployed_image_digest = inspect.running_digest
        await store.put(record)

    # Registry check — update if we have image+digest and checker is available
    checker: RegistryChecker = _get_registry_checker(request)
    if record.image and record.deployed_image_digest:
        try:
            latest = await checker.get_latest_digest(record.image)
            if latest is not None:
                new_ua = latest != record.deployed_image_digest
                if (
                    record.update_available != new_ua
                    or record.latest_registry_digest != latest
                ):
                    record.update_available = new_ua
                    record.latest_registry_digest = latest
                    await store.put(record)
        except Exception:  # noqa: BLE001
            pass  # degrade gracefully; return last known update_available

    return record.to_status()


# ---------------------------------------------------------------------------
# GET /services/{name}/health
# ---------------------------------------------------------------------------


@app.get(
    "/services/{name}/health",
    response_model=ServiceHealthResponse,
    summary="Get service health",
    responses={404: {"model": ErrorDetail, "description": "Service not found"}},
)
async def get_service_health(
    name: str,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ServiceHealthResponse:
    record = await _get_or_create_record(name, store)
    inspect = await backend.status(record)
    if inspect.health != record.health:
        record.health = inspect.health
        await store.put(record)
    health = inspect.health if inspect.health else "unknown"
    return ServiceHealthResponse(name=name, health=health)


# ---------------------------------------------------------------------------
# GET /services/{name}/logs
# ---------------------------------------------------------------------------


@app.get(
    "/services/{name}/logs",
    summary="Stream container logs (auth-gated)",
    responses={
        404: {"model": ErrorDetail, "description": "Service not found"},
        422: {"description": "Validation error (tail out of range 1-10000)"},
    },
)
async def get_service_logs(
    name: str,
    tail: int = Query(100, ge=1, le=10000),
    since: str | None = Query(None, description="ISO 8601 or Unix timestamp"),
    follow: bool = Query(False, description="If true, stream new log lines as they arrive"),
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> StreamingResponse:
    record = await _get_or_create_record(name, store)

    async def log_gen():
        async for chunk in backend.stream_logs(record, tail=tail, since=since, follow=follow):
            yield chunk

    return StreamingResponse(log_gen(), media_type="text/plain; charset=utf-8")


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
    _auth: None = Depends(verify_auth),
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
    _auth: None = Depends(verify_auth),
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
    _auth: None = Depends(verify_auth),
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
# POST /services/{name}/deploy
# ---------------------------------------------------------------------------


@app.post(
    "/services/{name}/deploy",
    response_model=DeployResponse,
    summary="Deploy a new image version for a service",
    responses={
        404: {"model": ErrorDetail, "description": "Service or component config not found"},
        503: {"model": ErrorDetail, "description": "Registry not loaded"},
    },
)
async def deploy_service(
    name: str,
    request: Request,
    body: DeployRequest = Body(default=None),
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    _auth: None = Depends(verify_auth),
) -> DeployResponse:
    if body is None:
        body = DeployRequest()

    record = await _get_or_create_record(name, store)

    config = registry.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config for '{name}' — cannot deploy",
        )

    env_store: EnvStore = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(name, config.env)
    config = config.model_copy(update={"env": merged_env})

    image_ref = body.image or config.image

    try:
        outcome = await backend.deploy(record, config, image_ref)
    except Exception as exc:
        logger.exception("deploy %s failed", name)
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Deploy failed: {exc}",
        )

    record.state = outcome.state
    record.image = image_ref
    record.image_revision = outcome.deployed_digest
    record.deployed_image_digest = outcome.deployed_digest
    record.previous_image_digest = outcome.previous_digest
    record.last_error = ""
    await store.put(record)

    return DeployResponse(
        name=name,
        deployed_digest=outcome.deployed_digest,
        previous_digest=outcome.previous_digest,
        current_state=record.state,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/rollback
# ---------------------------------------------------------------------------


@app.post(
    "/services/{name}/rollback",
    response_model=RollbackResponse,
    summary="Rollback a service to its prior image digest",
    responses={
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail, "description": "No prior digest recorded"},
        503: {"model": ErrorDetail},
    },
)
async def rollback_service(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    _auth: None = Depends(verify_auth),
) -> RollbackResponse:
    record = await _get_or_create_record(name, store)

    if not record.previous_image_digest:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No prior image digest recorded for '{name}' — run a deploy first",
        )

    config = registry.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config for '{name}' — cannot rollback",
        )

    env_store: EnvStore = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(name, config.env)
    config = config.model_copy(update={"env": merged_env})

    # Snapshot current digests before mutating
    old_deployed = record.deployed_image_digest
    old_previous = record.previous_image_digest

    try:
        outcome = await backend.rollback(record, config)
    except Exception as exc:
        logger.exception("rollback %s failed", name)
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback failed: {exc}",
        )

    # Swap digests: rolled-back-to becomes deployed; what-we-had becomes previous
    record.state = outcome.state
    record.deployed_image_digest = old_previous
    record.previous_image_digest = old_deployed
    record.image_revision = old_previous
    record.last_error = ""
    await store.put(record)

    return RollbackResponse(
        name=name,
        rolled_back_to_digest=old_previous,
        current_state=record.state,
    )


# ---------------------------------------------------------------------------
# GET /services/{name}/env
# ---------------------------------------------------------------------------


@app.get(
    "/services/{name}/env",
    response_model=EnvResponse,
    summary="Get stored environment variables and secret keys for a service",
    responses={404: {"model": ErrorDetail, "description": "Service not found"}},
)
async def get_service_env(
    name: str,
    store: ServiceStore = Depends(_get_store),
    env_store: EnvStore = Depends(_get_env_store),
    _auth: None = Depends(verify_auth),
) -> EnvResponse:
    await _get_or_create_record(name, store)
    config = await env_store.get(name)
    secrets_masked = {key: "***" for key in config.secret_tokens}
    return EnvResponse(env=config.env, secrets=secrets_masked)


# ---------------------------------------------------------------------------
# PUT /services/{name}/env
# ---------------------------------------------------------------------------


@app.put(
    "/services/{name}/env",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Upsert environment variables and secrets for a service",
    responses={404: {"model": ErrorDetail, "description": "Service not found"}},
)
async def put_service_env(
    name: str,
    body: EnvUpdate,
    store: ServiceStore = Depends(_get_store),
    env_store: EnvStore = Depends(_get_env_store),
    _auth: None = Depends(verify_auth),
) -> None:
    await _get_or_create_record(name, store)
    await env_store.upsert(name, body.env, body.secrets)


# ---------------------------------------------------------------------------
# DELETE /services/{name}/env/{key}
# ---------------------------------------------------------------------------


@app.delete(
    "/services/{name}/env/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an environment variable or secret key for a service",
    responses={
        404: {"model": ErrorDetail, "description": "Service or key not found"},
    },
)
async def delete_service_env_key(
    name: str,
    key: str,
    store: ServiceStore = Depends(_get_store),
    env_store: EnvStore = Depends(_get_env_store),
    _auth: None = Depends(verify_auth),
) -> None:
    await _get_or_create_record(name, store)
    found = await env_store.delete_key(name, key)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Key '{key}' not found in env or secrets for '{name}'",
        )


# ---------------------------------------------------------------------------
# POST /onboard/preflight
# ---------------------------------------------------------------------------


@app.post("/onboard/preflight", response_model=OnboardPreflightResponse)
async def onboard_preflight(
    req: OnboardPreflightRequest,
    _: None = Depends(verify_auth),
    store: ServiceStore = Depends(_get_store),
) -> OnboardPreflightResponse:
    """Fetch and parse a service repo's docker-compose.yml, returning a DerivedSpec.

    The caller reviews the spec before confirming onboarding via `/onboard/confirm`.
    """
    import re

    from robotsix_central_deploy.onboard.fetcher import FetchError, fetch_compose_bytes
    from robotsix_central_deploy.onboard.parser import ParseError, parse_compose

    # Validate name slug
    if not re.fullmatch(r"^[a-z0-9][a-z0-9-]*$", req.name):
        raise HTTPException(
            status_code=422,
            detail={"error": f"Invalid name '{req.name}': must match ^[a-z0-9][a-z0-9-]*$"},
        )

    # Reserved-name guard
    from ..gateway.router import RESERVED_NAMES  # noqa: PLC0415
    if req.name in RESERVED_NAMES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": f"Component name '{req.name}' is reserved"},
        )

    # Check for duplicate
    existing = await store.get(req.name)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": f"component '{req.name}' already exists"},
        )

    # Fetch compose bytes (git clone is blocking → run in executor)
    loop = asyncio.get_running_loop()
    try:
        compose_bytes = await loop.run_in_executor(
            None, fetch_compose_bytes, req.git_url,
        )
    except FetchError as e:
        raise HTTPException(status_code=422, detail={"error": str(e)})

    # Parse compose
    try:
        derived_spec = parse_compose(compose_bytes, req.name, req.git_url)
    except ParseError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": "compose validation failed", "violations": e.violations},
        )

    return OnboardPreflightResponse(spec=derived_spec)


# ---------------------------------------------------------------------------
# POST /onboard/confirm
# ---------------------------------------------------------------------------


@app.post("/onboard/confirm", response_model=OnboardConfirmResponse)
async def onboard_confirm(
    req: OnboardConfirmRequest,
    _: None = Depends(verify_auth),
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
) -> OnboardConfirmResponse:
    """Persist a reviewed DerivedSpec, deploy the container, and register the component."""
    spec = req.spec

    # Race-condition guard: re-check name not already in store
    existing = await store.get(spec.name)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": f"component '{spec.name}' already exists"},
        )

    # Reserved-name guard: don't allow names that shadow API routes
    from ..gateway.router import RESERVED_NAMES  # noqa: PLC0415
    if spec.name in RESERVED_NAMES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": f"Component name '{spec.name}' is reserved"},
        )

    # Build ComponentConfig from the DerivedSpec
    config = ComponentConfig(
        id=spec.name,
        image=spec.image,
        container_name=spec.container_name or spec.name,
        ports=spec.ports,
        mounts=spec.volume_mounts,
        env=spec.env,
        health_check=spec.health_check,
        claude_mount=spec.claude_mount,
        named_volumes=[m.host for m in spec.volume_mounts],
        stateful_volumes=spec.stateful_volumes,
    )

    # Persist config
    await component_config_store.put(config)

    # Register in-memory
    registry.register(config)

    # Create and persist ServiceRecord
    record = ServiceRecord(
        name=spec.name,
        container_name=spec.container_name or spec.name,
        image=spec.image,
    )
    await store.put(record)

    # Deploy
    try:
        outcome = await backend.deploy(record, config, config.image)
    except Exception as exc:
        logger.exception("onboard deploy failed for '%s'", spec.name)
        # Best-effort rollback: remove config, record, and in-memory entry
        await component_config_store.delete(config.id)
        registry._index.pop(config.id, None)  # noqa: SLF001
        await store.delete(spec.name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": str(exc)},
        )

    # Update record state from outcome
    record.state = outcome.state
    record.image = config.image
    record.image_revision = outcome.deployed_digest
    record.deployed_image_digest = outcome.deployed_digest
    record.previous_image_digest = outcome.previous_digest
    await store.put(record)

    return OnboardConfirmResponse(
        name=spec.name,
        image=spec.image,
        state=record.state.value,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        content = exc.detail
    elif isinstance(exc.detail, str):
        content = {"error": exc.detail}
    else:
        content = {"error": str(exc.detail)}
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=exc.headers if exc.headers else None,
    )


# ---------------------------------------------------------------------------
# Gateway router — MUST be registered last so its catch-all routes only
# match after every specific API route has been tried.
# ---------------------------------------------------------------------------

from ..gateway.router import gateway_router  # noqa: E402
app.include_router(gateway_router)


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
