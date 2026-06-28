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
from pydantic import BaseModel, Field

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
from ..registry.config_yaml_store import ConfigYamlStore
from ..registry.env_store import EnvStore
from ..registry.loader import ComponentRegistry
from ..registry.models import ComponentConfig, ServiceConfig
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
    _config_yaml_store = ConfigYamlStore(Path(_config.config_yaml_store_path))
    app.state.config = _config
    app.state.store = _store
    app.state.backend = _backend
    app.state.key_manager = _key_manager
    app.state.env_store = _env_store
    app.state.config_yaml_store = _config_yaml_store

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
        # Seed sibling records
        for sib in dyn_config.siblings:
            sib_name = f"{dyn_config.id}-{sib.service_key}"
            existing_sib = await _store.get(sib_name)
            if existing_sib is None:
                await _store.put(ServiceRecord(
                    name=sib_name,
                    container_name=sib.container_name,
                    image=sib.image,
                    component_id=dyn_config.id,
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


async def _get_config_yaml_store(request: Request) -> ConfigYamlStore:
    return request.app.state.config_yaml_store


async def _get_or_create_record(name: str, store: ServiceStore) -> ServiceRecord:
    """Fetch a service record by name, raising 404 when absent."""
    record = await store.get(name)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service '{name}' not found",
        )
    return record


async def _get_sibling_pairs(
    name: str,
    config: ComponentConfig,
    store: ServiceStore,
) -> list[tuple[ServiceConfig, ServiceRecord]]:
    """Return (ServiceConfig, ServiceRecord) pairs for siblings of `name`.
    Missing sibling records are logged and skipped (best-effort).
    """
    pairs = []
    for sib in config.siblings:
        sib_name = f"{name}-{sib.service_key}"
        sib_record = await store.get(sib_name)
        if sib_record is None:
            logger.warning("sibling record '%s' not found; skipping", sib_name)
            continue
        pairs.append((sib, sib_record))
    return pairs


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
    registry: ComponentRegistry = Depends(_get_registry),
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

    # Fan out to siblings (best-effort per sibling)
    config = registry.get(name)
    if config and config.siblings:
        for sib, sib_record in await _get_sibling_pairs(name, config, store):
            try:
                final = await backend.start(sib_record)
                sib_record.state = final
                await store.put(sib_record)
            except Exception:
                logger.warning("start sibling '%s-%s' failed", name, sib.service_key)

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
    registry: ComponentRegistry = Depends(_get_registry),
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

    # Stop siblings (best-effort per sibling)
    config = registry.get(name)
    if config and config.siblings:
        for sib, sib_record in await _get_sibling_pairs(name, config, store):
            try:
                final = await backend.stop(sib_record)
                sib_record.state = final
                await store.put(sib_record)
            except Exception:
                logger.warning("stop sibling '%s-%s' failed", name, sib.service_key)

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
    registry: ComponentRegistry = Depends(_get_registry),
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

    # Restart siblings (best-effort per sibling)
    config = registry.get(name)
    if config and config.siblings:
        for sib, sib_record in await _get_sibling_pairs(name, config, store):
            try:
                final = await backend.restart(sib_record)
                sib_record.state = final
                await store.put(sib_record)
            except Exception:
                logger.warning("restart sibling '%s-%s' failed", name, sib.service_key)

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

    # Deploy siblings
    config_fresh = registry.get(name)  # re-read for sibling env
    if config_fresh and config_fresh.siblings:
        for sib_config, sib_record in await _get_sibling_pairs(name, config_fresh, store):
            sib_name = f"{name}-{sib_config.service_key}"
            merged_env = await env_store.get_merged_env(sib_name, sib_config.env)
            effective_sib = ComponentConfig(
                id=sib_name,
                image=sib_config.image,
                container_name=sib_config.container_name,
                ports=sib_config.ports,
                mounts=sib_config.mounts,
                env=merged_env,
                health_check=sib_config.health_check,
                claude_mount=sib_config.claude_mount,
                named_volumes=[m.host for m in sib_config.mounts],
            )
            try:
                sib_outcome = await backend.deploy(sib_record, effective_sib, sib_config.image)
                sib_record.state = sib_outcome.state
                sib_record.image = sib_config.image
                sib_record.deployed_image_digest = sib_outcome.deployed_digest
                sib_record.previous_image_digest = sib_outcome.previous_digest
                await store.put(sib_record)
            except Exception:
                logger.warning("deploy sibling '%s' failed", sib_name)

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

    # Rollback siblings using each sibling's previous_image_digest
    config_fresh = registry.get(name)
    if config_fresh and config_fresh.siblings:
        for sib_config, sib_record in await _get_sibling_pairs(name, config_fresh, store):
            if not sib_record.previous_image_digest:
                logger.warning("rollback sibling '%s-%s': no prior digest — skipping",
                               name, sib_config.service_key)
                continue
            sib_name = f"{name}-{sib_config.service_key}"
            merged_env = await env_store.get_merged_env(sib_name, sib_config.env)
            effective_sib = ComponentConfig(
                id=sib_name,
                image=sib_config.image,
                container_name=sib_config.container_name,
                ports=sib_config.ports,
                mounts=sib_config.mounts,
                env=merged_env,
                health_check=sib_config.health_check,
                claude_mount=sib_config.claude_mount,
                named_volumes=[m.host for m in sib_config.mounts],
            )
            try:
                sib_outcome = await backend.rollback(sib_record, effective_sib)
                sib_old_deployed = sib_record.deployed_image_digest
                sib_old_previous = sib_record.previous_image_digest
                sib_record.state = sib_outcome.state
                sib_record.deployed_image_digest = sib_old_previous
                sib_record.previous_image_digest = sib_old_deployed
                sib_record.image_revision = sib_old_previous
                await store.put(sib_record)
            except Exception:
                logger.warning("rollback sibling '%s' failed", sib_name)

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
# Config helpers
# ---------------------------------------------------------------------------


def _mask_secrets(template: dict, current: dict) -> dict:
    """Return *current* with secret leaf values replaced by ``"***"``.

    A leaf in *template* is a secret if its value is ``""`` or ``None``.
    Corresponding non-empty string values in *current* are masked.
    Non-secret and nested branches are preserved as-is from *current*.
    """

    def _recursive(i_template: dict, i_current: dict) -> dict:
        result: dict = {}
        for key, tval in i_template.items():
            cval = i_current.get(key)
            if isinstance(tval, dict) and isinstance(cval, dict):
                result[key] = _recursive(tval, cval)
            elif tval in ("", None) and isinstance(cval, str) and cval:
                result[key] = "***"
            else:
                result[key] = cval if key in i_current else tval
        return result

    return _recursive(template, current)


def _merge_config(template: dict, existing: dict, submitted: dict) -> dict:
    """Deep-merge *submitted* over *existing*, respecting secret sentinel.

    For each key in *template*:
    - If the key is a nested dict in all three, recurse.
    - If the template leaf is a secret (``""`` or ``None``) AND
      ``submitted[key] == "***"``: keep ``existing[key]`` unchanged.
    - Else: use ``submitted.get(key, template[key])``.
    """

    def _recursive(i_template: dict, i_existing: dict, i_submitted: dict) -> dict:
        result: dict = {}
        for key, tval in i_template.items():
            if (
                isinstance(tval, dict)
                and isinstance(i_existing.get(key), dict)
                and isinstance(i_submitted.get(key), dict)
            ):
                result[key] = _recursive(tval, i_existing[key], i_submitted[key])
            elif tval in ("", None) and i_submitted.get(key) == "***":
                result[key] = i_existing.get(key, tval)
            else:
                result[key] = i_submitted.get(key, tval)
        return result

    return _recursive(template, existing, submitted)


# ---------------------------------------------------------------------------
# Config endpoint models
# ---------------------------------------------------------------------------


class ConfigResponse(BaseModel):
    config_schema: dict = Field(serialization_alias="schema")
    current: dict


class ConfigUpdate(BaseModel):
    values: dict


# ---------------------------------------------------------------------------
# GET /services/{name}/config
# ---------------------------------------------------------------------------


@app.get(
    "/services/{name}/config",
    response_model=ConfigResponse,
    summary="Get config.yaml schema and current values for a service",
    responses={404: {"model": ErrorDetail, "description": "Service has no config schema"}},
)
async def get_service_config(
    name: str,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    _auth: None = Depends(verify_auth),
) -> ConfigResponse:
    await _get_or_create_record(name, store)
    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for component '{name}'",
        )
    current_raw = await config_yaml_store.get_current(name) or template
    current_masked = _mask_secrets(template, current_raw)
    return ConfigResponse(config_schema=template, current=current_masked)


# ---------------------------------------------------------------------------
# PUT /services/{name}/config
# ---------------------------------------------------------------------------


@app.put(
    "/services/{name}/config",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Merge and save config.yaml values for a service",
    responses={404: {"model": ErrorDetail, "description": "Service has no config schema"}},
)
async def put_service_config(
    name: str,
    body: ConfigUpdate,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> None:
    await _get_or_create_record(name, store)
    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for component '{name}'",
        )
    existing = await config_yaml_store.get_current(name) or template
    merged = _merge_config(template, existing, body.values)
    await config_yaml_store.update_current(name, merged)
    config_vol = f"{name}-config"
    await backend.write_config_to_volume(config_vol, merged)


# ---------------------------------------------------------------------------
# DELETE /services/{name}
# ---------------------------------------------------------------------------


@app.delete(
    "/services/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["services"],
    summary="Remove an onboarded component and optionally its container",
)
async def delete_service(
    name: str,
    stop_container: bool = Query(
        default=True,
        description="Stop and remove the managed container (true) or leave it running (false)",
    ),
    store: ServiceStore = Depends(_get_store),
    config_store: ComponentConfigStore = Depends(_get_component_config_store),
    env_store: EnvStore = Depends(_get_env_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    _auth: None = Depends(verify_auth),
) -> None:
    # 1. Verify component exists in config store
    config = config_store.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Component '{name}' not found",
        )

    # 2. Resolve sibling pairs
    pairs = await _get_sibling_pairs(name, config, store)

    # 3. Get primary record (may be None if partially onboarded)
    record = await store.get(name)

    # 4. Best-effort container stop/remove
    if stop_container:
        if record is not None:
            try:
                await backend.stop(record)
            except Exception:
                logger.warning("stop failed for %s during delete", name, exc_info=True)
            try:
                await backend.remove_container(record)
            except Exception:
                logger.warning(
                    "remove_container failed for %s during delete", name, exc_info=True,
                )
        for _sib_cfg, sib_record in pairs:
            try:
                await backend.stop(sib_record)
            except Exception:
                logger.warning(
                    "stop failed for %s during delete", sib_record.name, exc_info=True,
                )
            try:
                await backend.remove_container(sib_record)
            except Exception:
                logger.warning(
                    "remove_container failed for %s during delete",
                    sib_record.name,
                    exc_info=True,
                )

    # 5. Delete sibling records and env
    for sib_cfg, sib_record in pairs:
        await store.delete(sib_record.name)
        await env_store.delete(f"{name}-{sib_cfg.service_key}")

    # 6. Delete primary record
    if record is not None:
        await store.delete(name)

    # 7. Delete primary env/secrets
    await env_store.delete(name)

    # 8. Delete primary config.yaml
    await config_yaml_store.delete(name)

    # 9. Delete from config store
    await config_store.delete(name)

    # 10. Remove from in-memory registry
    registry.unregister(name)


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

    from robotsix_central_deploy.onboard.fetcher import FetchError, fetch_repo_files
    from robotsix_central_deploy.onboard.parser import ConfigParseError, ParseError, parse_compose, parse_config_yaml

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

    # Fetch repo files (git clone is blocking → run in executor)
    loop = asyncio.get_running_loop()
    try:
        repo_files = await loop.run_in_executor(
            None, fetch_repo_files, req.git_url,
        )
    except FetchError as e:
        raise HTTPException(status_code=422, detail={"error": str(e)})

    # Parse compose
    try:
        derived_spec = parse_compose(repo_files.compose_bytes, req.name, req.git_url)
    except ParseError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": "compose validation failed", "violations": e.violations},
        )

    # Parse config/config.yaml if present
    if repo_files.config_yaml is not None:
        try:
            derived_spec.config_schema = parse_config_yaml(repo_files.config_yaml)
        except ConfigParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
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
        named_volumes=[m.host for m in spec.volume_mounts]
                      + [m.host for sib in spec.siblings for m in sib.volume_mounts],
        stateful_volumes=spec.stateful_volumes,
        siblings=[
            ServiceConfig(
                service_key=sib.service_key,
                container_name=sib.container_name,
                image=sib.image,
                ports=sib.ports,
                mounts=sib.volume_mounts,
                env=sib.env,
                claude_mount=sib.claude_mount,
                health_check=sib.health_check,
            )
            for sib in spec.siblings
        ],
    )

    # Persist config
    await component_config_store.put(config)

    # Register in-memory
    registry.register(config)

    # If config schema present, save template and write config.yaml to volume
    if spec.config_schema is not None:
        config_vol = f"{spec.name}-config"
        if config_vol not in config.named_volumes:
            config.named_volumes.append(config_vol)
        await config_yaml_store.save_template(spec.name, spec.config_schema)
        try:
            await backend.write_config_to_volume(config_vol, spec.config_schema)
        except Exception:
            await config_yaml_store.delete(spec.name)
            raise

    # Create and persist ServiceRecord
    record = ServiceRecord(
        name=spec.name,
        container_name=spec.container_name or spec.name,
        image=spec.image,
    )
    await store.put(record)

    # Deploy primary
    try:
        outcome = await backend.deploy(record, config, config.image)
    except Exception as exc:
        logger.exception("onboard deploy failed for '%s'", spec.name)
        # Best-effort rollback: remove config, record, and in-memory entry
        await config_yaml_store.delete(spec.name)
        await component_config_store.delete(config.id)
        registry.unregister(config.id)
        await store.delete(spec.name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": str(exc)},
        )

    # Update primary record state from outcome
    record.state = outcome.state
    record.image = config.image
    record.image_revision = outcome.deployed_digest
    record.deployed_image_digest = outcome.deployed_digest
    record.previous_image_digest = outcome.previous_digest
    await store.put(record)

    # Deploy siblings
    sibling_records_created: list[ServiceRecord] = []
    try:
        for sib in spec.siblings:
            sib_name = f"{spec.name}-{sib.service_key}"
            sib_component_config = ComponentConfig(
                id=sib_name,
                image=sib.image,
                container_name=sib.container_name,
                ports=sib.ports,
                mounts=sib.volume_mounts,
                env=sib.env,
                health_check=sib.health_check,
                claude_mount=sib.claude_mount,
                named_volumes=[m.host for m in sib.volume_mounts],
            )
            sib_record = ServiceRecord(
                name=sib_name,
                container_name=sib.container_name,
                image=sib.image,
                component_id=spec.name,
            )
            await store.put(sib_record)
            sibling_records_created.append(sib_record)

            sib_outcome = await backend.deploy(sib_record, sib_component_config, sib.image)
            sib_record.state = sib_outcome.state
            sib_record.image = sib.image
            sib_record.deployed_image_digest = sib_outcome.deployed_digest
            sib_record.previous_image_digest = sib_outcome.previous_digest
            await store.put(sib_record)
    except Exception as exc:
        logger.exception("onboard sibling deploy failed for '%s'", spec.name)
        # Delete all sibling records from store
        for sr in sibling_records_created:
            await store.delete(sr.name)
        # Undo primary (existing rollback path)
        await config_yaml_store.delete(spec.name)
        await component_config_store.delete(config.id)
        registry.unregister(config.id)
        await store.delete(spec.name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": str(exc)},
        )

    return OnboardConfirmResponse(
        name=spec.name,
        image=spec.image,
        state=record.state.value,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        content = dict(exc.detail)
        content.setdefault("error", str(exc.detail))
        content.setdefault("detail", "")
    elif isinstance(exc.detail, str):
        content = ErrorDetail(error=exc.detail, detail="").model_dump()
    else:
        content = ErrorDetail(error=str(exc.detail), detail="").model_dump()
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
