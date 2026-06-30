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
import json
import logging
import posixpath
import re
import shutil
from collections.abc import AsyncIterator
import shlex
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.params import Body
from fastapi.responses import StreamingResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .auth import verify_auth
from .backend import DockerBackend, DockerSdkBackend, ExecutionBackend, NoopBackend
from .config import LifecycleConfig
from .error_handlers import register_error_handlers
from .models import (
    ActionResponse,
    DeployRequest,
    DeployResponse,
    DiskUsageResponse,
    ErrorDetail,
    ReclaimResponse,
    RollbackResponse,
    ServiceHealthResponse,
    ServiceListItem,
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
from ..registry.models import ComponentConfig, ConfigAssistSeed, ServiceConfig
from ..registry.secret_key import SecretKeyManager
from ..registry_check import RegistryChecker
from ..ui.router import router as ui_router
from ..volume_audit.models import VolumeAuditResponse
from ..volume_audit.scheduler import VolumeAuditScheduler
from .store import FileStore, InMemoryStore, ServiceStore

logger = logging.getLogger(__name__)

#: Module-level registry checker (set by lifespan, used by endpoints).
_registry_checker: RegistryChecker | None = None
_http_client: httpx.AsyncClient | None = None
_volume_audit_scheduler: VolumeAuditScheduler | None = None

#: Maximum bytes returned by ``GET /volumes/{name}/cat`` (1 MiB).
VOLUME_CAT_MAX_BYTES: int = 1_048_576


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
    config_values: dict | None = None  # optional, for config.yaml repos


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


class VolumeEntryModel(BaseModel):
    name: str
    is_dir: bool
    size_bytes: int


class VolumeLsResponse(BaseModel):
    entries: list[VolumeEntryModel]


# ---------------------------------------------------------------------------
# Volume browser models
# ---------------------------------------------------------------------------


class VolumeEntry(BaseModel):
    name: str
    type: str  # "file" or "dir"
    size_bytes: int


class VolumeListResponse(BaseModel):
    entries: list[VolumeEntry]


class VolumeFileResponse(BaseModel):
    size_bytes: int
    content: str | None
    binary: bool
    truncated: bool


# ---------------------------------------------------------------------------
# Path traversal guard for volume browser
# ---------------------------------------------------------------------------


def _validate_volume_path(rel_path: str) -> str:
    """Normalise and validate a volume-relative path.

    Returns the normalised form (leading ``/`` stripped, ``.`` → ``""``).
    Raises ``HTTPException(400)`` on traversal / NUL.
    """
    if "\x00" in rel_path:
        raise HTTPException(status_code=400, detail="Path contains NUL byte")
    # Strip a single leading slash so callers can pass "/" or "/foo".
    if rel_path.startswith("/"):
        rel_path = rel_path[1:]
    # Collapse to a clean relative path.
    norm = str(Path(rel_path))
    if norm == ".":
        norm = ""
    if ".." in Path(norm).parts:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    return norm


def _assert_volume_browsable(name: str, store: ComponentConfigStore) -> None:
    """Raise 404 if *name* is not in any component's ``named_volumes``."""
    allowed: set[str] = set()
    for cfg in store.all():
        allowed.update(cfg.named_volumes)
    if name not in allowed:
        raise HTTPException(
            status_code=404,
            detail=f"Volume '{name}' not found or not browsable",
        )


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
        return DockerSdkBackend(
            socket_url=cfg.docker_socket_url,
            claude_host_mount_path=cfg.claude_host_mount_path,
        )
    if cfg.execution_backend == "docker":
        return DockerBackend()
    return NoopBackend()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _config, _store, _backend, _registry_checker, _http_client
    _config = LifecycleConfig()
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

    # -- System settings store (overlay persisted settings onto _config) ---
    from ..registry.settings_store import SystemSettings, SystemSettingsStore

    settings_store = SystemSettingsStore(_config.effective_system_settings_path)
    app.state.settings_store = settings_store

    # Seed on first boot: write a settings file so the dashboard always
    # shows a non-blank username. Uses the env-var value if set, else "admin".
    if not settings_store._path.exists():
        await settings_store.put(
            SystemSettings(
                auth_username=_config.auth_username or "admin",
                auth_password=_config.auth_password,
                disk_warn_pct=_config.disk_warn_pct,
                registry_check_interval=_config.registry_check_interval,
                log_level=_config.log_level,
                gateway_base_domain=_config.gateway_base_domain,
                claude_host_mount_path=_config.claude_host_mount_path,
            )
        )

    _config = settings_store.overlay(
        _config
    )  # returns new LifecycleConfig (or same if no file)
    app.state.config = _config  # replace with overlaid version

    # -- Session store (in-memory, no I/O) ------------------------------
    from .session import SessionStore

    app.state.session_store = SessionStore()

    # Apply log_level from (possibly overlaid) config
    logging.getLogger().setLevel(_config.log_level)

    # -- Registry checker ------------------------------------------------
    http_client = httpx.AsyncClient(timeout=10.0)
    registry_checker = RegistryChecker(
        http_client,
        ttl_seconds=_config.registry_check_ttl,
    )
    app.state.registry_checker = registry_checker
    _registry_checker = registry_checker
    _http_client = http_client

    bg_task = None
    if _config.registry_check_interval > 0:
        bg_task = asyncio.create_task(
            _registry_check_loop(
                _store,
                registry_checker,
                _backend,
                _config.registry_check_interval,
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
            await _store.put(
                ServiceRecord(
                    name=dyn_config.id,
                    container_name=dyn_config.container_name,
                    image=dyn_config.image,
                )
            )
        # Seed sibling records
        for sib in dyn_config.siblings:
            sib_name = f"{dyn_config.id}-{sib.service_key}"
            existing_sib = await _store.get(sib_name)
            if existing_sib is None:
                await _store.put(
                    ServiceRecord(
                        name=sib_name,
                        container_name=sib.container_name,
                        image=sib.image,
                        component_id=dyn_config.id,
                    )
                )
        logger.info("Loaded dynamic component config for '%s'", dyn_config.id)

    # -- Migrate existing config templates to use the SECRET sentinel ------
    for _dyn_cfg in component_config_store.all():
        _tmpl = await _config_yaml_store.get_template(_dyn_cfg.id)
        if _tmpl:
            _annotated = _annotate_secret_sentinels(_tmpl)
            if _annotated != _tmpl:
                await _config_yaml_store.save_template(
                    _dyn_cfg.id,
                    _annotated,  # type: ignore[arg-type]
                )
                logger.info("Migrated config template sentinels for %s", _dyn_cfg.id)

    # --- Volume audit subsystem ---
    global _volume_audit_scheduler
    _volume_audit_task: asyncio.Task[Any] | None = None
    if _config.volume_audit_enabled:
        _volume_audit_scheduler = VolumeAuditScheduler(
            _config, _backend, component_config_store
        )
        app.state.volume_audit_scheduler = _volume_audit_scheduler
        _volume_audit_task = asyncio.create_task(
            _volume_audit_scheduler.loop(_config.volume_audit_interval_seconds)
        )
    else:
        _volume_audit_scheduler = None
        app.state.volume_audit_scheduler = None

    yield

    if _volume_audit_task and not _volume_audit_task.done():
        _volume_audit_task.cancel()
        with suppress(asyncio.CancelledError):
            await _volume_audit_task
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
        401: {
            "model": ErrorDetail,
            "description": "Unauthorized — invalid or missing credentials",
        },
    },
)

register_error_handlers(app)

app.include_router(ui_router)

from .settings_router import settings_router  # noqa: E402

app.include_router(settings_router)

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def _get_store(request: Request) -> ServiceStore:
    store = request.app.state.store
    assert store is not None, "store not initialised"
    return store  # type: ignore[no-any-return]


async def _get_backend(request: Request) -> ExecutionBackend:
    backend = request.app.state.backend
    assert backend is not None, "backend not initialised"
    return backend  # type: ignore[no-any-return]


async def _get_config(request: Request) -> LifecycleConfig:
    config = request.app.state.config
    assert config is not None, "config not initialised"
    return config  # type: ignore[no-any-return]


async def _get_registry(request: Request) -> ComponentRegistry:
    """Return the ComponentRegistry from app state."""
    return request.app.state.registry  # type: ignore[no-any-return]


def _get_registry_checker(request: Request) -> RegistryChecker:
    return request.app.state.registry_checker  # type: ignore[no-any-return]


async def _get_component_config_store(request: Request) -> ComponentConfigStore:
    return request.app.state.component_config_store  # type: ignore[no-any-return]


def _safe_volume_path(raw: str) -> str:
    """Normalise *raw* path and return a safe relative path confined to the volume root.

    posixpath.normpath clamps all '..' components that would escape '/', so
    normpath('/' + '../../etc/passwd') == '/etc/passwd'. Stripping the leading '/'
    gives 'etc/passwd', which inside the container maps to '/vol/etc/passwd' — safe.
    Empty string is valid (volume root).
    """
    normalized = posixpath.normpath("/" + raw.strip("/"))
    return normalized.lstrip("/")


_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3"}
)


def _is_binary(rel_path: str, data: bytes) -> bool:
    """Return True when the file should be refused as binary."""
    ext = posixpath.splitext(rel_path)[1].lower()
    if ext in _BINARY_EXTENSIONS:
        return True
    return b"\x00" in data[:8192]  # NUL-byte sniff


def _namespace_spec_volumes(spec: "DerivedSpec", component_name: str) -> "DerivedSpec":
    """Prefix all named-volume hosts with the component name.

    Converts image-hardcoded names (e.g. ``auto-mail-config``) into
    per-component names (e.g. ``mail-auto-mail-config``) so two components
    from the same image never share storage.
    """
    from robotsix_central_deploy.onboard.models import SiblingDerivedSpec  # noqa: PLC0415
    from robotsix_central_deploy.registry.models import VolumeMount  # noqa: PLC0415

    old_to_new: dict[str, str] = {}

    def _rename(vm: VolumeMount) -> VolumeMount:
        new_host = f"{component_name}-{vm.host}"
        old_to_new[vm.host] = new_host
        return vm.model_copy(update={"host": new_host})

    new_primary_mounts = [_rename(vm) for vm in spec.volume_mounts]

    new_siblings: list[SiblingDerivedSpec] = [
        sib.model_copy(
            update={"volume_mounts": [_rename(vm) for vm in sib.volume_mounts]}
        )
        for sib in spec.siblings
    ]

    new_stateful = [old_to_new.get(v, v) for v in spec.stateful_volumes]
    new_config_vol = (
        old_to_new.get(spec.config_volume, spec.config_volume)
        if spec.config_volume is not None
        else None
    )

    return spec.model_copy(
        update={
            "volume_mounts": new_primary_mounts,
            "siblings": new_siblings,
            "stateful_volumes": new_stateful,
            "config_volume": new_config_vol,
        }
    )


def _fetch_fresh_config_assist(
    git_url: str, name: str
) -> tuple[str | None, list[ConfigAssistSeed]]:
    """Re-fetch config-assist fields from the repo's compose at HEAD.

    Blocking (runs git clone). Call via run_in_executor.
    Raises FetchError or ParseError on failure; callers must handle.
    """
    from robotsix_central_deploy.onboard.fetcher import fetch_compose_bytes
    from robotsix_central_deploy.onboard.parser import parse_compose

    compose_bytes = fetch_compose_bytes(git_url)
    spec = parse_compose(compose_bytes, name, git_url)
    return spec.config_assist_command, spec.config_assist_seeds


async def _get_env_store(request: Request) -> EnvStore:
    return request.app.state.env_store  # type: ignore[no-any-return]


async def _get_config_yaml_store(request: Request) -> ConfigYamlStore:
    return request.app.state.config_yaml_store  # type: ignore[no-any-return]


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
        warn_threshold_pct=config.disk_warn_pct,
        docker=docker_df,
    )


@app.post("/disk/reclaim", response_model=ReclaimResponse)
async def reclaim_build_cache(
    _auth: None = Depends(verify_auth),
    backend: ExecutionBackend = Depends(_get_backend),
) -> ReclaimResponse:
    """Prune Docker build cache and return bytes freed."""
    space_reclaimed = await backend.prune_builds()
    return ReclaimResponse(space_reclaimed_bytes=space_reclaimed)


# ---------------------------------------------------------------------------
# GET /volumes/audit
# ---------------------------------------------------------------------------


@app.get("/volumes/audit", response_model=VolumeAuditResponse)
async def get_volume_audit(
    _auth: None = Depends(verify_auth),
    config: LifecycleConfig = Depends(_get_config),
) -> VolumeAuditResponse:
    """Current volume audit state (sizes and growth). Returns enabled=false when subsystem is off."""
    if not config.volume_audit_enabled:
        return VolumeAuditResponse(enabled=False)
    scheduler: VolumeAuditScheduler = app.state.volume_audit_scheduler
    return scheduler.get_audit_response()


@app.get("/volumes/{name}/ls", response_model=VolumeLsResponse)
async def list_volume_path_route(
    name: str,
    path: str = Query(default=""),
    _auth: None = Depends(verify_auth),
    backend: ExecutionBackend = Depends(_get_backend),
) -> VolumeLsResponse:
    rel_path = _safe_volume_path(path)
    entries = await backend.list_volume_path(name, rel_path)
    return VolumeLsResponse(entries=[VolumeEntryModel(**e) for e in entries])


@app.get("/volumes/{name}/cat")
async def cat_volume_file(
    name: str,
    path: str = Query(...),
    _auth: None = Depends(verify_auth),
    backend: ExecutionBackend = Depends(_get_backend),
) -> PlainTextResponse:
    rel_path = _safe_volume_path(path)
    if not rel_path:
        raise HTTPException(
            status_code=400,
            detail="path must point to a file, not the volume root",
        )
    data = await backend.read_volume_file(name, rel_path)
    if _is_binary(rel_path, data):
        raise HTTPException(
            status_code=415,
            detail=(
                "Binary file — cannot display. "
                "Unsupported format (database, binary blob)."
            ),
        )
    return PlainTextResponse(data.decode("utf-8", errors="replace"))


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
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    _auth: None = Depends(verify_auth),
) -> ServiceListResponse:
    records = await store.list_all()
    items: list[ServiceListItem] = []
    for r in records:
        item = r.to_list_item()
        config = component_config_store.get(r.name)
        if config is not None:
            item.stateful_volumes = config.stateful_volumes
            item.has_config_yaml = config.has_config_yaml
        items.append(item)
    return ServiceListResponse(services=items)


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
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
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
    if (
        inspect.running_digest
        and inspect.running_digest != record.deployed_image_digest
    ):
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

    result = record.to_status()
    cfg = component_config_store.get(name)
    if cfg is not None:
        result.has_config_yaml = cfg.has_config_yaml
    return result


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
    follow: bool = Query(
        False, description="If true, stream new log lines as they arrive"
    ),
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> StreamingResponse:
    record = await _get_or_create_record(name, store)

    async def log_gen() -> AsyncIterator[bytes]:
        async for chunk in backend.stream_logs(
            record, tail=tail, since=since, follow=follow
        ):
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
            name=name,
            action="start",
            previous_state=previous,
            current_state=ServiceState.RUNNING,
            detail="Service is already running",
        )
    if record.state == ServiceState.STARTING:
        return ActionResponse(
            name=name,
            action="start",
            previous_state=previous,
            current_state=ServiceState.STARTING,
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
    record.last_error = (
        "" if final_state == ServiceState.RUNNING else "backend reported failure"
    )
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
        name=name,
        action="start",
        previous_state=previous,
        current_state=record.state,
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
            name=name,
            action="stop",
            previous_state=previous,
            current_state=ServiceState.STOPPED,
            detail="Service is already stopped",
        )
    if record.state == ServiceState.STOPPING:
        return ActionResponse(
            name=name,
            action="stop",
            previous_state=previous,
            current_state=ServiceState.STOPPING,
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
    record.last_error = (
        "" if final_state == ServiceState.STOPPED else "backend reported failure"
    )
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
        name=name,
        action="stop",
        previous_state=previous,
        current_state=record.state,
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
            name=name,
            action="restart",
            previous_state=previous,
            current_state=ServiceState.RESTARTING,
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
    record.last_error = (
        "" if final_state == ServiceState.RUNNING else "backend reported failure"
    )
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
        name=name,
        action="restart",
        previous_state=previous,
        current_state=record.state,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/deploy
# ---------------------------------------------------------------------------


@app.post(
    "/services/{name}/deploy",
    response_model=DeployResponse,
    summary="Deploy a new image version for a service",
    responses={
        404: {
            "model": ErrorDetail,
            "description": "Service or component config not found",
        },
        503: {"model": ErrorDetail, "description": "Registry not loaded"},
    },
)
async def deploy_service(
    name: str,
    request: Request,
    body: DeployRequest | None = Body(default=None),  # type: ignore[assignment]
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
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

    # Write merged config.yaml into the config volume before starting the container.
    if config.has_config_yaml and config.config_volume:
        merged_cfg = await config_yaml_store.get_current(
            name
        ) or await config_yaml_store.get_template(name)
        if merged_cfg:
            try:
                await backend.write_config_to_volume(config.config_volume, merged_cfg)
            except Exception as exc:
                logger.warning(
                    "deploy %s: could not write config.yaml to volume %s: %s",
                    name,
                    config.config_volume,
                    exc,
                )
                # non-fatal: container may still start if config was written earlier

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
        for sib_config, sib_record in await _get_sibling_pairs(
            name, config_fresh, store
        ):
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
                host_docker_sock=sib_config.host_docker_sock,
                named_volumes=[m.host for m in sib_config.mounts],
                command=sib_config.command,
                entrypoint=sib_config.entrypoint,
            )
            try:
                sib_outcome = await backend.deploy(
                    sib_record, effective_sib, sib_config.image
                )
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
        for sib_config, sib_record in await _get_sibling_pairs(
            name, config_fresh, store
        ):
            if not sib_record.previous_image_digest:
                logger.warning(
                    "rollback sibling '%s-%s': no prior digest — skipping",
                    name,
                    sib_config.service_key,
                )
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
                host_docker_sock=sib_config.host_docker_sock,
                named_volumes=[m.host for m in sib_config.mounts],
                command=sib_config.command,
                entrypoint=sib_config.entrypoint,
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


_CONFIG_SECRET_SENTINEL = "SECRET"
"""Template authors mark a sensitive leaf by setting its value to
``_CONFIG_SECRET_SENTINEL`` (the string ``"SECRET"``) in their
``config/config.yaml``. Any other value (empty string, default text,
integer, etc.) is NOT treated as a secret."""

_SECRET_NAME_TOKENS: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "key",
)


def _is_secret_name(key: str) -> bool:
    """Return True if *key* (a YAML field name) looks like a secret field.

    Checked at template-generation time only.  Substring match is
    intentional: ``smtp_password``, ``api_key``, ``oauth_token`` all hit.
    """
    lower = key.lower()
    return any(tok in lower for tok in _SECRET_NAME_TOKENS)


def _annotate_secret_sentinels(template: object) -> object:
    """Walk *template* (from parse_config_yaml) and mark secret leaves.

    Rules (applied in priority order):
    - value already equals ``_CONFIG_SECRET_SENTINEL`` → keep
    - ``_is_secret_name(key)`` is True for a scalar leaf → replace with
      ``_CONFIG_SECRET_SENTINEL``
    - dict value → recurse
    - list where first item is a dict → annotate first item, return
      ``[annotated_item]`` (single-element template list, consistent with
      the array-of-objects schema convention used by ``_mask_secrets`` and
      ``_merge_config``)
    - anything else (scalar, scalar list) → leave unchanged
    """
    if not isinstance(template, dict):
        return template
    result: dict[str, object] = {}
    for key, val in template.items():
        if isinstance(val, dict):
            result[key] = _annotate_secret_sentinels(val)
        elif isinstance(val, list) and val and isinstance(val[0], dict):
            result[key] = [_annotate_secret_sentinels(val[0])]
        elif val == _CONFIG_SECRET_SENTINEL or _is_secret_name(key):
            result[key] = _CONFIG_SECRET_SENTINEL
        else:
            result[key] = val
    return result


def _mask_secrets(template: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Return *current* with secret leaf values replaced by ``"***"``.

    A leaf in *template* is treated as a secret when its value is
    ``_CONFIG_SECRET_SENTINEL`` (the string ``"SECRET"``). Template
    authors mark sensitive fields explicitly — no name-based heuristic
    is applied.

    * If the template value is ``"SECRET"`` and *current* has a
      non-empty, non-sentinel string value: mask as ``"***"``.
    * If the template value is ``"SECRET"`` but *current* is missing or
      also ``"SECRET"``: return ``""`` (unconfigured secret).
    * Otherwise: pass through from *current* (or fall back to *template*).
    """

    def _recursive(
        i_template: dict[str, Any], i_current: dict[str, Any]
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, tval in i_template.items():
            cval = i_current.get(key)
            if isinstance(tval, dict) and isinstance(cval, dict):
                result[key] = _recursive(tval, cval)
            elif isinstance(tval, list) and isinstance(cval, list):
                # Object-array: mask each item using the first schema item as the template.
                item_template = tval[0] if tval and isinstance(tval[0], dict) else None
                if item_template:
                    result[key] = [
                        _recursive(item_template, item)
                        if isinstance(item, dict)
                        else item
                        for item in cval
                    ]
                else:
                    result[key] = cval  # scalar array — pass through unchanged
            elif (
                tval == _CONFIG_SECRET_SENTINEL
                and isinstance(cval, str)
                and cval
                and cval != _CONFIG_SECRET_SENTINEL
            ):
                # Configured secret → mask it
                result[key] = "***"
            elif tval == _CONFIG_SECRET_SENTINEL:
                # Unconfigured secret (no current value, or current == sentinel) → empty
                result[key] = ""
            else:
                result[key] = cval if key in i_current else tval
        return result

    return _recursive(template, current)


def _coerce_to_template(tval: object, sval: object) -> object:
    """Coerce a submitted form value back to the template leaf's type.

    The config UI renders every leaf as a text/password ``<input>`` and
    therefore submits all values as strings.  Without coercion, typed
    scalars in ``config.yaml`` (``port: 8080``, ``enabled: true``) would
    be silently rewritten as strings on the next Save.  This re-derives
    the intended type from the template leaf.

    Best-effort: a value that cannot be parsed into the template type is
    returned unchanged (the submitted string) rather than raising, so a
    Save never fails on an unexpected value.
    """
    if not isinstance(sval, str):
        return sval
    # bool is a subclass of int — must be checked before int.
    if isinstance(tval, bool):
        low = sval.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off", ""):
            return False
        return sval
    if isinstance(tval, int):
        try:
            return int(sval)
        except ValueError:
            return sval
    if isinstance(tval, float):
        try:
            return float(sval)
        except ValueError:
            return sval
    if isinstance(tval, (list, dict)):
        try:
            return json.loads(sval)
        except (ValueError, TypeError):
            return sval
    return sval


def _merge_config(
    template: dict[str, Any], existing: dict[str, Any], submitted: dict[str, Any]
) -> dict[str, Any]:
    """Deep-merge *submitted* over *existing*, respecting secret sentinel.

    For each key in *template*:
    - If the key is a nested dict in all three, recurse.
    - If the template leaf is ``_CONFIG_SECRET_SENTINEL`` AND
      ``submitted[key] == "***"``: keep ``existing[key]`` unchanged (or
      fall back to ``""`` when there is no existing value).
    - Else: use the submitted value (coerced back to the template leaf's
      type, since the UI submits everything as strings) or, when the key
      was not submitted, the template default.
    """

    def _recursive(
        i_template: dict[str, Any],
        i_existing: dict[str, Any],
        i_submitted: dict[str, Any],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, tval in i_template.items():
            if isinstance(tval, dict) and isinstance(i_submitted.get(key), dict):
                existing_sub = (
                    i_existing[key] if isinstance(i_existing.get(key), dict) else {}
                )
                result[key] = _recursive(tval, existing_sub, i_submitted[key])
            elif (
                isinstance(tval, list)
                and isinstance(i_submitted.get(key), list)
                and tval
                and isinstance(tval[0], dict)
            ):
                # Array of objects: merge each submitted item against the corresponding existing item,
                # preserving secret sentinels ("***") per field within each item.
                item_template = tval[0]
                submitted_list = i_submitted[key]
                raw_existing = i_existing.get(key)
                existing_list = raw_existing if isinstance(raw_existing, list) else []
                result[key] = [
                    _recursive(
                        item_template,
                        existing_list[idx]
                        if idx < len(existing_list)
                        and isinstance(existing_list[idx], dict)
                        else {},
                        sitem if isinstance(sitem, dict) else {},
                    )
                    for idx, sitem in enumerate(submitted_list)
                ]
            elif tval == _CONFIG_SECRET_SENTINEL and i_submitted.get(key) == "***":
                result[key] = i_existing.get(key, "")
            elif key in i_submitted:
                result[key] = _coerce_to_template(tval, i_submitted[key])
            else:
                result[key] = tval
        return result

    return _recursive(template, existing, submitted)


_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_account_ids(merged: dict[str, Any]) -> None:
    """Raise HTTP 422 when any account id contains disallowed characters.

    auto-mail enforces account_id =~ ^[A-Za-z0-9._-]+$ at startup.
    The @ character (e.g. from using an email address as the id) triggers
    a crash-loop.  Validate before writing to storage.
    """
    for item in merged.get("accounts", []):
        if not isinstance(item, dict):
            continue
        id_val = item.get("id", "")
        if id_val and not _ACCOUNT_ID_RE.fullmatch(id_val):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"account_id {id_val!r} must match ^[A-Za-z0-9._-]+$ "
                    f"(no @ or spaces — use the slug derived from the email address)"
                ),
            )


def _prune_unset(merged: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    """Remove template-default empty fields that were absent from existing.

    Prevents empty-string placeholders for unused template sections
    (e.g. ``archive.namespace``, ``calendar.broker_*``) from being
    written to stored config after they fall through merge with
    empty/no value.

    Rules:
    - Empty string (``""``) or ``None`` at a scalar leaf: prune unless the key
      was already in *existing*.
    - Non-empty scalars (including int/float/bool and 0/False): always kept.
    - Dict values: recurse; include the sub-dict only when non-empty or
      the key was already present in *existing*.
    - List-of-dicts: recurse per item against the corresponding
      *existing* item (or ``{}`` for out-of-range indices).
    """
    result: dict[str, Any] = {}
    for k, v in merged.items():
        if isinstance(v, dict):
            sub_existing = existing[k] if isinstance(existing.get(k), dict) else {}
            pruned = _prune_unset(v, sub_existing)
            if pruned or k in existing:
                result[k] = pruned
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            ex_val = existing.get(k)
            ex_list: list[Any] = ex_val if isinstance(ex_val, list) else []
            result[k] = [
                _prune_unset(
                    item,
                    ex_list[i]
                    if i < len(ex_list) and isinstance(ex_list[i], dict)
                    else {},
                )
                for i, item in enumerate(v)
            ]
        elif v in ("", None) and k not in existing:
            pass  # skip: field was absent from existing and no new value set
        else:
            result[k] = v
    return result


def _seed_for_detect(
    template: dict[str, Any],
    existing: dict[str, Any],
    submitted: dict[str, Any],
) -> dict[str, Any]:
    """Build a sparse seed config for the pre-detect volume write.

    Only keys present in *submitted* are emitted (recursively).
    ``"***"`` sentinel values are resolved from *existing* for secret fields.
    Template-default empty strings are skipped even when present in
    *submitted*, so the detect program sees absent fields and fills them
    in correctly.  Dict/list results that are entirely empty are also
    omitted.
    """
    result: dict[str, Any] = {}
    for key, val in submitted.items():
        tval = template.get(key) if isinstance(template, dict) else None
        ex_val = existing.get(key) if isinstance(existing, dict) else None

        if isinstance(val, str) and val == "":
            # Template default — skip, let detect fill it in.
            continue
        if isinstance(val, str) and val == "***":
            # Secret restoration: use existing value, or empty string if none.
            result[key] = ex_val if ex_val is not None else ""
        elif isinstance(val, str):
            result[key] = val
        elif isinstance(val, dict):
            sub = _seed_for_detect(
                tval if isinstance(tval, dict) else {},
                ex_val if isinstance(ex_val, dict) else {},
                val,
            )
            if sub:
                result[key] = sub
        elif isinstance(val, list):
            if isinstance(tval, list) and tval and isinstance(tval[0], dict):
                item_template = tval[0]
                ex_list = ex_val if isinstance(ex_val, list) else []
                items: list[dict[str, Any]] = []
                for i, item in enumerate(val):
                    if isinstance(item, dict):
                        sub = _seed_for_detect(
                            item_template,
                            ex_list[i]
                            if i < len(ex_list) and isinstance(ex_list[i], dict)
                            else {},
                            item,
                        )
                        if sub:
                            items.append(sub)
                    else:
                        items.append(item)
                if items:
                    result[key] = items
            else:
                result[key] = val
        else:
            # Any other type (bool, int, float): include as-is.
            result[key] = val
    return result


def _relocate_account_seed_values(
    values: dict,
    seeds: list["ConfigAssistSeed"],
    src_idx: int,
    dst_idx: int,
) -> None:
    """Move seed values from ``accounts[src_idx]`` to ``accounts[dst_idx]`` in-place.

    For each seed whose key starts with ``accounts.<src_idx>.``, extracts
    the value from the source slot and sets it on the destination slot —
    but ONLY when the destination slot does not already carry a non-empty
    value for that same seed key (so pre-populated multi-account submits
    from tests are not double-moved).

    ``"***"`` sentinels (unchanged secrets) are skipped — they already
    carry the correct meaning at the source and should not be relocated.
    """
    accts: list[dict[str, Any]] = values.setdefault("accounts", [])
    while len(accts) <= max(src_idx, dst_idx):
        accts.append({})
    src_acct: dict = accts[src_idx] if src_idx < len(accts) else {}
    dst_acct: dict = accts[dst_idx]

    for seed in seeds:
        parts = seed.key.split(".")
        if len(parts) < 3 or parts[0] != "accounts" or parts[1] != str(src_idx):
            continue

        # Check whether destination already has a non-empty value.
        dst_node: dict[str, Any] = dst_acct
        for p in parts[2:-1]:
            if not isinstance(dst_node, dict):
                dst_node = {}
                break
            dst_node = dst_node.get(p, {})
        if (
            isinstance(dst_node, dict)
            and parts[-1] in dst_node
            and dst_node[parts[-1]] not in (None, "", "***")
        ):
            continue  # already present at destination — nothing to move

        # Navigate to the leaf dict containing the key at source.
        node: dict[str, Any] = src_acct
        for p in parts[2:-1]:
            if not isinstance(node, dict):
                node = {}
                break
            node = node.get(p, {})
        last = parts[-1]
        if not isinstance(node, dict) or last not in node:
            continue
        val = node[last]
        if isinstance(val, str) and val == "***":
            continue  # unchanged secret — stays at source
        del node[last]
        # Place at destination.
        dst_node2: dict[str, Any] = dst_acct
        for p in parts[2:-1]:
            if isinstance(dst_node2, dict):
                dst_node2 = dst_node2.setdefault(p, {})
            else:
                break
        if isinstance(dst_node2, dict):
            dst_node2[last] = val


def _derive_account_id(
    seeds: list["ConfigAssistSeed"],
    partial: dict[str, Any],
    n: int,
) -> str:
    """Derive a slug-based account ID for a new account slot at index *n*.

    Looks for the first ConfigAssistSeed whose last path segment is
    ``username`` or ``email``, then navigates *partial* (replacing the
    hardcoded ``0`` index in the seed key with *n*) to get the submitted
    value.  Slugifies it (lower-case, non-alnum chars → ``-``, max 40
    chars).  Falls back to ``f'accounts-{n}'``.
    """
    import re as _re

    for seed in seeds:
        parts = seed.key.split(".")
        if parts[-1] not in ("username", "email"):
            continue
        nav_parts = [str(n) if p == "0" else p for p in parts]
        node: object = partial
        for part in nav_parts:
            if isinstance(node, dict):
                node = node.get(part)
            elif isinstance(node, list):
                try:
                    node = node[int(part)]
                except (ValueError, IndexError):
                    node = None
            else:
                node = None
            if node is None:
                break
        if isinstance(node, str) and node:
            slug = _re.sub(r"[^a-z0-9]+", "-", node.lower()).strip("-")
            return slug[:40] or f"accounts-{n}"
    return f"accounts-{n}"


def _resolve_placeholders(command_str: str, values: dict[str, Any]) -> str:
    """Substitute ``{dotted.path}`` placeholders in *command_str* from *values*.

    Each placeholder is a dot-separated path of dict keys and list indices
    (e.g. ``accounts.0.auth.username``) into the nested *values* dict.
    Unresolvable placeholders are left as-is.
    """

    def _navigate(path: str) -> str | None:
        parts = path.split(".")
        node: object = values
        for part in parts:
            if isinstance(node, dict):
                node = node.get(part, _MISSING)
            elif isinstance(node, list):
                try:
                    idx = int(part)
                except ValueError:
                    return None
                if idx < 0 or idx >= len(node):
                    return None
                node = node[idx]
            else:
                return None
            if node is _MISSING:
                return None
        if isinstance(node, (str, int, float, bool)):
            return str(node)
        return None

    _MISSING = object()

    def _replacer(m: re.Match[str]) -> str:
        resolved = _navigate(m.group(1))
        return resolved if resolved is not None else m.group(0)

    return re.sub(r"\{([^{}]+)\}", _replacer, command_str)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overlay* into *base*, returning a new dict.

    Leaf values from *overlay* overwrite *base*; nested dicts are merged
    recursively.  Keys only in *base* are preserved.
    """
    result = dict(base)
    for key, val in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# Config endpoint models
# ---------------------------------------------------------------------------


class ConfigResponse(BaseModel):
    config_schema: dict[str, Any] = Field(serialization_alias="schema")
    current: dict[str, Any]
    config_assist_command: str | None = None
    config_assist_seeds: list[ConfigAssistSeed] = []


class ConfigUpdate(BaseModel):
    values: dict[str, Any]


class ConfigAssistRequest(BaseModel):
    values: dict[
        str, Any
    ]  # current (partial) form values — same shape as ConfigUpdate.values
    target_account_index: int | None = None
    # None  → infer: first-setup if no accounts exist, else add-new
    # int N → update account N if N < len(existing_accounts), else add-new
    account_name: str | None = None
    # Optional account name for add-new mode — overrides the derive-from-email
    # heuristic. Slugified automatically before use.


class ConfigAssistResponse(BaseModel):
    config: dict[
        str, Any
    ]  # the auto-filled config dict read back from the volume after the command ran
    output: str  # captured stdout+stderr from the one-shot container


# ---------------------------------------------------------------------------
# GET /services/{name}/config
# ---------------------------------------------------------------------------


@app.get(
    "/services/{name}/config",
    response_model=ConfigResponse,
    summary="Get config.yaml schema and current values for a service",
    responses={
        404: {"model": ErrorDetail, "description": "Service has no config schema"}
    },
)
async def get_service_config(
    name: str,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
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
    comp_cfg = component_config_store.get(name)
    return ConfigResponse(
        config_schema=template,
        current=current_masked,
        config_assist_command=comp_cfg.config_assist_command if comp_cfg else None,
        config_assist_seeds=comp_cfg.config_assist_seeds if comp_cfg else [],
    )


# ---------------------------------------------------------------------------
# PUT /services/{name}/config
# ---------------------------------------------------------------------------


@app.put(
    "/services/{name}/config",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Merge and save config.yaml values for a service",
    responses={
        404: {"model": ErrorDetail, "description": "Service has no config schema"}
    },
)
async def put_service_config(
    name: str,
    body: ConfigUpdate,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
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
    if "accounts" in merged:
        _validate_account_ids(merged)  # Bug 2: reject invalid id slugs
    merged = _prune_unset(merged, existing)  # Bug 3: prune resurrected empty fields
    await config_yaml_store.update_current(name, merged)

    # Write to the actual config volume (not synthetic "{name}-config")
    comp_cfg = component_config_store.get(name)
    if comp_cfg and comp_cfg.config_volume:
        await backend.write_config_to_volume(comp_cfg.config_volume, merged)
        # Restart primary + siblings sharing the same config volume so the
        # running container(s) pick up the new values immediately.
        registry: ComponentRegistry = request.app.state.registry
        store2: ServiceStore = store  # local alias for clarity
        record = await store2.get(name)
        if record and record.state == ServiceState.RUNNING:
            try:
                await backend.restart(record)
            except Exception as exc:
                logger.warning("config saved for %s but restart failed: %s", name, exc)
        # Fan out to siblings that share the same config volume
        config = registry.get(name) if registry else None
        if config and config.siblings:
            for sib, sib_record in await _get_sibling_pairs(name, config, store2):
                if sib_record.state != ServiceState.RUNNING:
                    continue
                try:
                    await backend.restart(sib_record)
                except Exception as exc:
                    logger.warning(
                        "config saved for %s but sibling '%s' restart failed: %s",
                        name,
                        sib_record.name,
                        exc,
                    )
    else:
        logger.warning(
            "put_service_config: no config_volume for %s — config written to store only",
            name,
        )


# ---------------------------------------------------------------------------
# POST /services/{name}/config/assist
# ---------------------------------------------------------------------------


@app.post(
    "/services/{name}/config/assist",
    response_model=ConfigAssistResponse,
    summary="Run a repo-declared config-assist command in a one-shot container and return auto-filled config",
    responses={
        400: {
            "model": ErrorDetail,
            "description": "No config-assist command or config volume configured",
        },
        404: {"model": ErrorDetail, "description": "Component not found"},
        504: {"model": ErrorDetail, "description": "Assist command timed out"},
    },
)
async def run_config_assist(
    name: str,
    body: ConfigAssistRequest,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    env_store: EnvStore = Depends(_get_env_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ConfigAssistResponse:
    comp_cfg = component_config_store.get(name)
    if comp_cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Component '{name}' not found",
        )

    # Re-read config-assist fields from repo HEAD. Write back if changed so
    # GET /config and future assist calls also see the fresh values.
    try:
        loop = asyncio.get_running_loop()
        fresh_cmd, fresh_seeds = await loop.run_in_executor(
            None, _fetch_fresh_config_assist, comp_cfg.git_url, name
        )
        if (
            fresh_cmd != comp_cfg.config_assist_command
            or fresh_seeds != comp_cfg.config_assist_seeds
        ):
            comp_cfg = comp_cfg.model_copy(
                update={
                    "config_assist_command": fresh_cmd,
                    "config_assist_seeds": fresh_seeds,
                }
            )
            await component_config_store.put(comp_cfg)
            logger.info("Refreshed config-assist fields for %s from repo", name)
    except Exception as exc:
        logger.warning(
            "Could not refresh config-assist fields for %s from repo (%s); "
            "using stored values",
            name,
            exc,
        )

    if comp_cfg.config_assist_command is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No config-assist command configured for '{name}'",
        )
    if comp_cfg.config_volume is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No config volume for '{name}' — add robotsix.deploy.config-target label",
        )

    # Fetch config template + existing current
    template = await config_yaml_store.get_template(name) or {}
    current_raw = await config_yaml_store.get_current(name)
    existing = current_raw or template
    partial = _merge_config(template, existing, body.values)

    # --- Account-aware mode resolution ---
    existing_accounts: list[dict[str, Any]] = (
        [
            a
            for a in current_raw.get("accounts", [])
            if isinstance(a, dict) and a.get("id")
        ]
        if current_raw is not None and isinstance(current_raw.get("accounts"), list)
        else []
    )
    req_idx = body.target_account_index

    if req_idx is not None and req_idx < len(existing_accounts):
        mode, target_idx = "update", req_idx
    elif existing_accounts:  # req_idx is None OR req_idx >= len
        mode, target_idx = "add_new", len(existing_accounts)
    else:
        mode, target_idx = "first_setup", 0

    # Rewrite accounts.0.* placeholders to the target index in the command.
    import re as _re  # noqa: PLC0415

    assist_command = comp_cfg.config_assist_command
    if target_idx != 0:
        assist_command = _re.sub(
            r"\{accounts\.0\.",
            f"{{accounts.{target_idx}.",
            assist_command,
        )

    # For add_new: the frontend seed bar collects values under the
    # template index (accounts.0.*); relocate them to the target slot
    # so the volume seed write targets the new account (not the existing
    # one) and {accounts.N.*} placeholders resolve correctly.
    if mode == "add_new":
        _relocate_account_seed_values(
            body.values, comp_cfg.config_assist_seeds, 0, target_idx
        )
        # Restore existing account slots verbatim from storage so the seed
        # bar's overwrite of accounts[0].* and the form's empty-string secret
        # fields do not corrupt existing accounts during the re-merge.
        submitted_accts: list[dict[str, Any]] = body.values.setdefault("accounts", [])
        for i, ea in enumerate(existing_accounts):
            if i < len(submitted_accts):
                submitted_accts[i] = dict(ea)
            else:
                submitted_accts.append(dict(ea))
        # Re-merge partial now that seed values are at the target index.
        partial = _merge_config(template, existing, body.values)

        new_id = _derive_account_id(comp_cfg.config_assist_seeds, partial, target_idx)
        if body.account_name:
            _name_slug = _re.sub(r"[^a-z0-9]+", "-", body.account_name.lower()).strip(
                "-"
            )[:40]
            if _name_slug:
                new_id = _name_slug
        acct_list: list[dict[str, Any]] = partial.setdefault("accounts", [])
        while len(acct_list) <= target_idx:
            acct_list.append({})
        acct_list[target_idx]["id"] = new_id
        _validate_account_ids(partial)  # fail fast: id must match ^[A-Za-z0-9._-]+$

    # Write sparse seed config into the volume (only submitted keys, no
    # template-default empty strings).  This lets the detect program fill
    # in absent/null fields correctly instead of treating pre-existing
    # empty strings as "already configured".
    if mode == "add_new":
        # Write existing accounts verbatim so detect does not re-validate them.
        # Write only the new account's seed fields (not template defaults).
        item_template = (template.get("accounts") or [{}])[0]
        new_acct_vals = (
            submitted_accts[target_idx] if target_idx < len(submitted_accts) else {}
        )
        new_acct_seed = _seed_for_detect(item_template, {}, new_acct_vals)
        detect_seed: dict[str, Any] = {
            k: v for k, v in existing.items() if k != "accounts"
        }
        detect_seed["accounts"] = list(existing.get("accounts", [])) + [new_acct_seed]
        await backend.write_config_to_volume(comp_cfg.config_volume, detect_seed)
    else:
        await backend.write_config_to_volume(
            comp_cfg.config_volume,
            _seed_for_detect(template, existing, body.values),
        )

    # Resolve the container-side mount path for the config volume
    volume_mount_path = next(
        (m.container for m in comp_cfg.mounts if m.host == comp_cfg.config_volume),
        "/config",  # safe fallback (matches busybox writer convention)
    )

    # Fetch decrypted env+secrets
    merged_env = await env_store.get_merged_env(name, comp_cfg.env)

    # Substitute {seed} placeholders in the command with submitted values
    # Substitute from the MERGED config (template+existing+submitted), not just
    # body.values — so placeholders like {accounts.0.id} (not user-submitted, but
    # present in the config) resolve instead of leaking the literal "{...}".
    # Split into args FIRST, substitute per-arg, then re-quote — so a value
    # containing spaces (e.g. a Google app password "abcd efgh ijkl mnop") stays
    # a SINGLE argument instead of being split apart by the backend's shlex.split.
    resolved_command = shlex.join(
        _resolve_placeholders(arg, partial) for arg in shlex.split(assist_command)
    )

    # For add_new: the config-assist command template always includes
    # --overwrite, but adding a new account should NOT overwrite.
    if mode == "add_new":
        resolved_command = shlex.join(
            arg for arg in shlex.split(resolved_command) if arg != "--overwrite"
        )

    # Run the one-shot container (60 s timeout)
    try:
        output = await backend.run_config_assist(
            image=comp_cfg.image,
            command_str=resolved_command,
            volume_name=comp_cfg.config_volume,
            volume_mount_path=volume_mount_path,
            env_dict=merged_env,
            timeout_seconds=60,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        )
    except RuntimeError as exc:
        output = str(exc)

    # Read back the updated config from the volume
    filled = await backend.read_config_from_volume(comp_cfg.config_volume)

    # Merge detected fields into the submitted config so the detected
    # output never clobbers other fields the user already entered.
    if mode == "add_new":
        # _deep_merge replaces the accounts list wholesale. Guard: always take
        # existing accounts from storage (not from what the detect program may
        # have re-written), and only take the new account's slot from filled.
        filled_accts = filled.get("accounts", [])
        # Prefer the detected slot at target_idx; fall back to last entry.
        new_acct_from_filled = (
            filled_accts[target_idx]
            if target_idx < len(filled_accts)
            else (filled_accts[-1] if filled_accts else {})
        )
        new_acct_partial = (
            partial["accounts"][target_idx]
            if target_idx < len(partial.get("accounts", []))
            else {}
        )
        merged_new_acct = _deep_merge(new_acct_partial, new_acct_from_filled)
        # Merge non-accounts keys normally.
        merged = _deep_merge(
            {k: v for k, v in partial.items() if k != "accounts"},
            {k: v for k, v in filled.items() if k != "accounts"},
        )
        assert (
            current_raw is not None
        )  # add_new mode only reachable when current_raw is set
        merged["accounts"] = list(current_raw.get("accounts", [])) + [merged_new_acct]
    else:
        merged = _deep_merge(partial, filled)

    # Post-process: drop unconfigured accounts (e.g. the leftover onboard
    # template slot such as an empty 'main') and ensure default_account points
    # at a real configured account — otherwise the board fails to load with
    # "default_account_id ... is not one of the configured accounts".
    accts_obj = merged.get("accounts")
    if isinstance(accts_obj, list):
        kept: list[Any] = []
        for a in accts_obj:
            if not isinstance(a, dict):
                continue
            auth = a.get("auth")
            imap = a.get("imap")
            user = auth.get("username") if isinstance(auth, dict) else None
            host = imap.get("host") if isinstance(imap, dict) else None
            if user or host:
                kept.append(a)
        merged["accounts"] = kept
        kept_ids = [a.get("id") for a in kept]
        if kept and merged.get("default_account") not in kept_ids:
            merged["default_account"] = kept[0].get("id", "")

        # Office365 accounts: ensure oauth2_provider is flagged and prompt operator
        _O365_SUFFIX = "office365.com"
        _o365_detected = False
        for _acct in kept:
            _imap = _acct.get("imap")
            _smtp = _acct.get("smtp")
            _imap_host = _imap.get("host", "") if isinstance(_imap, dict) else ""
            _smtp_host = _smtp.get("host", "") if isinstance(_smtp, dict) else ""
            if _imap_host.endswith(_O365_SUFFIX) or _smtp_host.endswith(_O365_SUFFIX):
                _acct_auth: Any = _acct.get("auth")
                if not isinstance(_acct_auth, dict):
                    _acct_auth = {}
                    _acct["auth"] = _acct_auth
                _acct_auth["oauth2_provider"] = "microsoft"
                _acct_auth.pop("password", None)
                _o365_detected = True
        if _o365_detected:
            _o365_msg = (
                "Microsoft/Office365 account detected — authorize it from the "
                "mail board (Authorize button) to connect."
            )
            output = f"{output}\n{_o365_msg}" if output.strip() else _o365_msg

    # Write the cleaned config back to the volume so the board reads the
    # de-stubbed config with a valid default_account (the detect output left
    # the empty template slot and/or default_account='main').
    await backend.write_config_to_volume(comp_cfg.config_volume, merged)
    # Persist detected config so GET /config shows it and Save is idempotent
    await config_yaml_store.update_current(name, merged)

    return ConfigAssistResponse(config=merged, output=output)


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
                    "remove_container failed for %s during delete",
                    name,
                    exc_info=True,
                )
        for _sib_cfg, sib_record in pairs:
            try:
                await backend.stop(sib_record)
            except Exception:
                logger.warning(
                    "stop failed for %s during delete",
                    sib_record.name,
                    exc_info=True,
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
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
) -> OnboardPreflightResponse:
    """Fetch and parse a service repo's docker-compose.yml, returning a DerivedSpec.

    The caller reviews the spec before confirming onboarding via `/onboard/confirm`.
    """
    import re

    from robotsix_central_deploy.onboard.fetcher import FetchError, fetch_repo_files
    from robotsix_central_deploy.onboard.parser import (
        ConfigParseError,
        ParseError,
        parse_compose,
        parse_config_yaml,
    )

    # Validate name slug
    if not re.fullmatch(r"^[a-z0-9][a-z0-9-]*$", req.name):
        raise HTTPException(
            status_code=422,
            detail={
                "error": f"Invalid name '{req.name}': must match ^[a-z0-9][a-z0-9-]*$"
            },
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
            None,
            fetch_repo_files,
            req.git_url,
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
            derived_spec.config_schema = _annotate_secret_sentinels(
                parse_config_yaml(repo_files.config_yaml)
            )  # type: ignore[assignment]
        except ConfigParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Preflight gate: config/config.yaml present but no config-target label
    if derived_spec.config_schema is not None and derived_spec.config_volume is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": (
                    "repo has config/config.yaml but no service declares "
                    "`robotsix.deploy.config-target` — add the label to "
                    "deploy/docker-compose.yml pointing to the full in-container "
                    "path of the config file (e.g. /home/mailbot/config/config.yaml)"
                ),
            },
        )

    # Volume-collision preflight: check that would-be namespaced volume names
    # do not collide with any existing component's named_volumes.
    candidate_volumes: set[str] = {
        f"{req.name}-{vm.host}" for vm in derived_spec.volume_mounts
    } | {
        f"{req.name}-{vm.host}"
        for sib in derived_spec.siblings
        for vm in sib.volume_mounts
    }
    if candidate_volumes:
        collisions: list[str] = []
        for existing_cfg in component_config_store.all():  # synchronous
            for vol in sorted(candidate_volumes & set(existing_cfg.named_volumes)):
                collisions.append(
                    f"'{vol}' is already owned by component '{existing_cfg.id}'"
                )
        if collisions:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "onboarding would create volume name collision(s) with existing component(s)",
                    "collisions": collisions,
                },
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
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    env_store: EnvStore = Depends(_get_env_store),
) -> OnboardConfirmResponse:
    """Persist a reviewed DerivedSpec, deploy the container, and register the component."""
    spec = req.spec

    # Namespace volume names so two components from the same image
    # never share Docker named volumes.
    spec = _namespace_spec_volumes(spec, spec.name)

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
        command=spec.command,
        entrypoint=spec.entrypoint,
        claude_mount=spec.claude_mount,
        host_docker_sock=spec.host_docker_sock,
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
                host_docker_sock=sib.host_docker_sock,
                health_check=sib.health_check,
                command=sib.command,
                entrypoint=sib.entrypoint,
            )
            for sib in spec.siblings
        ],
        git_url=spec.git_url,
        has_config_yaml=(spec.config_schema is not None),
    )
    # Wire the real config volume name (resolved by parser from the label)
    config.config_volume = spec.config_volume  # None if no config-target label
    config.config_assist_command = spec.config_assist_command
    config.config_assist_seeds = spec.config_assist_seeds

    # Persist config
    await component_config_store.put(config)

    # Register in-memory
    registry.register(config)

    # Seed EnvStore from the repo's env contract — first onboard only
    existing_env = await env_store.get(spec.name)
    if not existing_env.env and not existing_env.secret_tokens:
        seeded_env = {k: v for k, v in spec.env.items() if v}
        seeded_secrets = {k: "" for k, v in spec.env.items() if not v}
        if seeded_env or seeded_secrets:
            await env_store.upsert(spec.name, seeded_env, seeded_secrets)

    # If config schema present, save template + user values and write merged
    # config.yaml to the real config volume so the container starts healthy.
    if spec.config_schema is not None:
        await config_yaml_store.save_template(spec.name, spec.config_schema)
        merged = _merge_config(spec.config_schema, {}, req.config_values or {})
        await config_yaml_store.update_current(spec.name, merged)
        if spec.config_volume is not None:
            try:
                await backend.write_config_to_volume(spec.config_volume, merged)
            except Exception:
                await config_yaml_store.delete(spec.name)
                raise
        # Do NOT add a synthetic "{name}-config" volume — the real volume
        # is already in config.named_volumes (it came from spec.volume_mounts).

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
                host_docker_sock=sib.host_docker_sock,
                named_volumes=[m.host for m in sib.volume_mounts],
                command=sib.command,
                entrypoint=sib.entrypoint,
            )
            sib_record = ServiceRecord(
                name=sib_name,
                container_name=sib.container_name,
                image=sib.image,
                component_id=spec.name,
            )
            await store.put(sib_record)
            sibling_records_created.append(sib_record)

            sib_outcome = await backend.deploy(
                sib_record, sib_component_config, sib.image
            )
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

    cfg = LifecycleConfig()
    uvicorn.run(
        "robotsix_central_deploy.lifecycle.server:app",
        host=cfg.host,
        port=cfg.port,
        reload=False,
    )
