"""FastAPI dependency factories, helper functions, and lifespan for the lifecycle server.

Extracted from the monolithic server.py so that each router module can
import shared dependencies without importing the FastAPI app.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

import httpx
from fastapi import FastAPI, HTTPException, Request, status

from .backend import DockerBackend, DockerSdkBackend, ExecutionBackend, NoopBackend
from .config import LifecycleConfig
from .models import (
    ContainerHealthSummary,
    ExecutionBackendType,
    HealthStatus,
    ServiceRecord,
    VolumeStat,
    StoreBackend,
)
from ..registry.config_store import ComponentConfigStore
from ..registry.config_yaml_store import ConfigYamlStore
from ..registry.env_store import EnvStore
from ..registry.loader import ComponentRegistry
from ..registry.models import ComponentConfig, ServiceConfig
from ..registry.secret_key import SecretKeyManager
from ..registry_check import RegistryChecker
from ..caretaker.scheduler import CaretakerScheduler
from ..volume_audit.scheduler import VolumeAuditScheduler
from .store import FileStore, InMemoryStore, ServiceStore

if TYPE_CHECKING:
    from ..registry.models import ConfigAssistSeed
    from robotsix_central_deploy.onboard.models import DerivedSpec

logger = logging.getLogger(__name__)

#: Module-level registry checker (set by lifespan, used by endpoints).
_registry_checker: RegistryChecker | None = None
_http_client: httpx.AsyncClient | None = None
_volume_audit_scheduler: VolumeAuditScheduler | None = None

#: Maximum bytes returned by ``GET /volumes/{name}/cat`` (1 MiB).
VOLUME_CAT_MAX_BYTES: int = 1_048_576

_config: LifecycleConfig | None = None
_store: ServiceStore | None = None
_backend: ExecutionBackend | None = None


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


async def _compute_orphan_volumes(
    backend: ExecutionBackend, store: ComponentConfigStore
) -> list[VolumeStat]:
    """Return Docker volumes safe to prune: owned by no registered component
    AND not currently attached to any container.

    A volume declared in some component's ``named_volumes`` is deliberately
    excluded even when the component is stopped — its data must survive. A
    volume attached to a container (``in_use``) is excluded because Docker
    would refuse to remove it anyway and it is clearly still needed.
    """
    owned: set[str] = set()
    for cfg in store.all():
        owned.update(cfg.named_volumes)
    df = await backend.disk_df()
    return [v for v in df.volumes if v.name and v.name not in owned and not v.in_use]


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
# Store & backend factory helpers
# ---------------------------------------------------------------------------


def _build_store(cfg: LifecycleConfig) -> ServiceStore:
    if cfg.store_backend == StoreBackend.FILE:
        return FileStore(cfg.effective_store_path)
    return InMemoryStore()


def _build_backend(cfg: LifecycleConfig) -> ExecutionBackend:
    if cfg.execution_backend == ExecutionBackendType.DOCKER_SDK:
        return DockerSdkBackend(
            socket_url=cfg.docker_socket_url,
            claude_host_mount_path=cfg.claude_host_mount_path,
            timeout=cfg.docker_sdk_timeout,
        )
    if cfg.execution_backend == ExecutionBackendType.DOCKER:
        return DockerBackend()
    return NoopBackend()


# ---------------------------------------------------------------------------
# Onboard job registry (in-memory, single-process)
# ---------------------------------------------------------------------------

OnboardJobPhase = Literal[
    "writing_config",
    "deploying_primary",
    "waiting_health",
    "deploying_siblings",
    "done",
    "failed",
]


class OnboardJob:
    """In-memory record of one onboard confirm background deploy job."""

    __slots__ = ("job_id", "component", "phase", "error", "name", "image", "state")

    def __init__(self, job_id: str, component: str) -> None:
        self.job_id: str = job_id
        self.component: str = component
        self.phase: OnboardJobPhase = "writing_config"
        self.error: str | None = None
        self.name: str | None = None
        self.image: str | None = None
        self.state: str | None = None


class JobRegistry:
    """Thread-safe-ish in-memory registry for onboard background deploy jobs.

    The app is single-process asyncio; no lock is needed for simple
    dict access under the same event loop.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, OnboardJob] = {}
        self._counter: int = 0

    def create(self, component: str) -> str:
        """Create a new job and return its id."""
        self._counter += 1
        job_id = f"{component}-{self._counter}"
        self._jobs[job_id] = OnboardJob(job_id=job_id, component=component)
        return job_id

    def get(self, job_id: str) -> OnboardJob | None:
        """Return a job by id, or None."""
        return self._jobs.get(job_id)

    def update_phase(self, job_id: str, phase: OnboardJobPhase) -> None:
        """Update the phase of a job."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = phase

    def mark_failed(self, job_id: str, error: str) -> None:
        """Mark a job as failed with an error string."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = "failed"
            job.error = error

    def mark_done(self, job_id: str, name: str, image: str, state: str) -> None:
        """Mark a job as done with terminal fields."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = "done"
            job.name = name
            job.image = image
            job.state = state

    def has_active_job_for(self, component: str) -> bool:
        """Return True when a job for *component* is still in flight."""
        return any(
            j.component == component and j.phase not in ("done", "failed")
            for j in self._jobs.values()
        )


# ---------------------------------------------------------------------------
# Lifespan — wire up store & backend from config
# ---------------------------------------------------------------------------


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
    app.state.job_registry = JobRegistry()

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
    app.state.http_client = http_client

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

    # --- Caretaker subsystem ---
    caretaker_scheduler = CaretakerScheduler(
        config=_config,
        backend=_backend,
        registry=registry,
        service_store=_store,
        component_config_store=component_config_store,
        volume_audit_scheduler=_volume_audit_scheduler,
        settings_store=settings_store,
        http_client=http_client,
    )
    app.state.caretaker_scheduler = caretaker_scheduler

    initial_settings = await settings_store.get()
    _caretaker_task = asyncio.create_task(caretaker_scheduler.loop())

    if not initial_settings.caretaker_enabled:
        # Caretaker disabled at startup: start the standalone volume audit
        # loop if configured. When caretaker is hot-enabled later the
        # volume-audit loop continues — double-scan on phase_volumes is
        # acceptable for this ticket.
        if _config.volume_audit_enabled and _volume_audit_task is None:
            assert _volume_audit_scheduler is not None
            _volume_audit_task = asyncio.create_task(
                _volume_audit_scheduler.loop(_config.volume_audit_interval_seconds)
            )

    yield

    _caretaker_task.cancel()
    with suppress(asyncio.CancelledError):
        await _caretaker_task
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


async def _get_env_store(request: Request) -> EnvStore:
    return request.app.state.env_store  # type: ignore[no-any-return]


async def _get_config_yaml_store(request: Request) -> ConfigYamlStore:
    return request.app.state.config_yaml_store  # type: ignore[no-any-return]


async def _get_job_registry(request: Request) -> JobRegistry:
    return request.app.state.job_registry  # type: ignore[no-any-return]


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


def _compute_overall_health(
    primary_health: str,
    siblings: list["ContainerHealthSummary"],
) -> str:
    """Rollup health across primary + healthchecked siblings.

    Containers without a Docker healthcheck report health='' and are
    treated as neutral (excluded from the rollup).
    Returns '' when no container has a healthcheck configured.
    """
    candidates = [primary_health] + [s.health for s in siblings]
    checked = [h for h in candidates if h]  # non-empty → has healthcheck
    if not checked:
        return ""
    if any(h == HealthStatus.UNHEALTHY for h in checked):
        return HealthStatus.UNHEALTHY
    if any(h == HealthStatus.STARTING for h in checked):
        return HealthStatus.STARTING
    if all(h == HealthStatus.HEALTHY for h in checked):
        return HealthStatus.HEALTHY
    return ""


# ---------------------------------------------------------------------------
# Namespace & fetch helpers
# ---------------------------------------------------------------------------


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
) -> tuple[str | None, list["ConfigAssistSeed"]]:
    """Re-fetch config-assist fields from the repo's compose at HEAD.

    Blocking (runs git clone). Call via run_in_executor.
    Raises FetchError or ParseError on failure; callers must handle.
    """
    from robotsix_central_deploy.onboard.fetcher import fetch_compose_bytes
    from robotsix_central_deploy.onboard.parser import parse_compose

    compose_bytes = fetch_compose_bytes(git_url)
    spec = parse_compose(compose_bytes, name, git_url)
    return spec.config_assist_command, spec.config_assist_seeds


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


_CONFIG_SECRET_SENTINEL = "SECRET"
"""Template authors mark a sensitive leaf by setting its value to
``_CONFIG_SECRET_SENTINEL`` (the string ``"SECRET"``) in their
``config/config.yaml``. Any other value (empty string, default text,
integer, etc.) is NOT treated as a secret."""


def _annotate_secret_sentinels(template: object) -> object:
    """Walk *template* (from parse_config_yaml) and normalise secret leaves.

    Secrets are detected **purely by the explicit ``SECRET`` sentinel** —
    a template author marks a sensitive leaf by setting its value to
    ``_CONFIG_SECRET_SENTINEL`` (the string ``"SECRET"``) in the
    component's ``config/config.yaml``. There is no name-based heuristic:
    a field named ``api_key`` or ``password`` is a plain editable field
    unless its template value is ``"SECRET"`` (this is what lets a genuinely
    non-secret ``langfuse.public_key`` render as an ordinary input).

    Rules:
    - value already equals ``_CONFIG_SECRET_SENTINEL`` → keep
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
        else:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# canonical hash (config drift detection)
# ---------------------------------------------------------------------------


def _canonical_hash(d: dict[str, Any]) -> str:
    """SHA-256 of a canonically serialised YAML dict.

    Serialises via ``yaml.dump`` with ``sort_keys=True`` before hashing so
    key-insertion-order differences and Python-vs-docker-exec YAML
    formatting differences do not cause false drift positives.
    Returns the full 64-char hex digest.
    """
    serialised = yaml.dump(
        d, default_flow_style=False, allow_unicode=True, sort_keys=True
    )
    return hashlib.sha256(serialised.encode()).hexdigest()


# ---------------------------------------------------------------------------
# secret masking
# ---------------------------------------------------------------------------


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
        except ValueError, TypeError:
            return sval
    return sval


def _merge_config(
    template: dict[str, Any],
    existing: dict[str, Any],
    submitted: dict[str, Any],
    *,
    prefer_existing_for_unset: bool = False,
) -> dict[str, Any]:
    """Deep-merge *submitted* over *existing*, respecting secret sentinel.

    For each key in *template*:
    - If the key is a nested dict in all three, recurse.
    - If the template leaf is ``_CONFIG_SECRET_SENTINEL`` AND
      ``submitted[key] == "***"``: keep ``existing[key]`` unchanged (or
      fall back to ``""`` when there is no existing value).
    - Else: use the submitted value (coerced back to the template leaf's
      type, since the UI submits everything as strings) or, when the key
      was not submitted, fall back to the template default.

    *prefer_existing_for_unset*: when True, a key absent from *submitted*
    falls back to ``existing[key]`` (not the template default) whenever the
    operator already has a value for it. This is for callers that pass a
    SPARSE submission — e.g. config-assist, whose seed values include only
    the few fields the operator typed. Without it, every field the operator
    did not re-type (secrets like an LLM api_key, other config sections)
    would be reset to the template default and silently lost. The Save form,
    which renders every field, keeps the default (False) so an absent key
    correctly means "cleared → reset to default". Repo-agnostic: no
    knowledge of any particular config key.
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
            elif prefer_existing_for_unset and key in i_existing:
                # Sparse submission (e.g. config-assist): a key the operator
                # did not re-type keeps their existing value instead of being
                # reset to the template default and lost.
                result[key] = i_existing[key]
            else:
                result[key] = tval
        return result

    merged = _recursive(template, existing, submitted)
    # Belt-and-suspenders: a secret leaf that was never submitted (or whose
    # whole parent section was absent from the form) would otherwise reach
    # storage as the literal "SECRET" sentinel — and be ingested as a real
    # credential by components that read config.yaml directly (e.g. auto-mail,
    # which has no split-config sanitiser). Strip every residual sentinel.
    # ``merged`` is always a dict at the top level, so the recursive strip
    # returns a dict here; the typed local narrows the ``Any`` for mypy.
    stripped: dict[str, Any] = _strip_secret_sentinels(merged)
    return stripped


def _strip_secret_sentinels(value: Any) -> Any:
    """Recursively replace any residual ``_CONFIG_SECRET_SENTINEL`` scalar
    with ``""`` so the deployed config never contains the literal sentinel."""
    if value == _CONFIG_SECRET_SENTINEL:
        return ""
    if isinstance(value, dict):
        return {k: _strip_secret_sentinels(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_secret_sentinels(v) for v in value]
    return value


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
    values: dict[str, Any],
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
    src_acct: dict[str, Any] = accts[src_idx] if src_idx < len(accts) else {}
    dst_acct: dict[str, Any] = accts[dst_idx]

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
                except ValueError, IndexError:
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
