"""FastAPI dependency factories, helper functions, and lifespan for the lifecycle server.

Extracted from the monolithic server.py so that each router module can
import shared dependencies without importing the FastAPI app.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status

from .backends import DockerBackend, DockerSdkBackend, ExecutionBackend, NoopBackend
from .config import LifecycleConfig
from .models import (
    ExecutionBackendType,
    HealthStatus,
    ServiceRecord,
    VolumeStat,
    StoreBackend,
)
from .schemas import DeployJobPhase, OnboardJobPhase
from ..registry.config_store import ComponentConfigStore
from ..registry.config_yaml_store import ConfigYamlStore
from ..registry.deploy_history_store import DeployHistoryStore
from ..registry.env_store import EnvStore
from ..registry.loader import ComponentRegistry
from ..registry.models import ComponentConfig, ServiceConfig
from ..registry.secret_key import SecretKeyManager
from ..registry_check import RegistryChecker
from ..caretaker.scheduler import CaretakerScheduler
from .volume_audit.scheduler import VolumeAuditScheduler
from .store import FileStore, InMemoryStore, ServiceStore

if TYPE_CHECKING:
    from .models import ContainerHealthSummary
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

# -- Claude auth background refresh ---------------------------------------

CLAUDE_AUTH_VOLUME = "claude-auth"
CLAUDE_AUTH_REFRESH_BEFORE_SECONDS = 3600  # refresh when ≤ 1 hour until expiry
CLAUDE_AUTH_USER_AGENT = "claude-cli/2.1.199 (external, cli)"  # noqa: E501 — avoids Cloudflare 403
CLAUDE_AUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"  # noqa: S105 — URL, not a password
CLAUDE_AUTH_CLIENT_ID = (
    "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # gitleaks:allow — public OAuth client id
)

#: Module-level state for the last Claude auth refresh attempt.
_claude_auth_refresh_state: dict[str, Any] = {
    "last_refresh": None,  # float — monotonic timestamp of last attempt
    "last_error": None,  # str | None — error message if last refresh failed
}


def get_claude_auth_refresh_state() -> dict[str, Any]:
    """Return a snapshot of the Claude auth refresh state.

    Keys: ``last_refresh`` (float | None), ``last_error`` (str | None).
    Callers can derive ``refresh_status`` — ``"ok"`` when last_refresh is
    set and last_error is None, ``"failed"`` when last_error is set, or
    ``"never"`` otherwise.
    """
    return dict(_claude_auth_refresh_state)


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
# Background Claude auth credential refresh loop
# ---------------------------------------------------------------------------


async def _claude_auth_refresh_loop(
    backend: ExecutionBackend,
    interval_sec: int,
) -> None:
    """Periodically check and refresh Claude auth credentials in the
    ``claude-auth`` named volume.

    Reads ``.credentials.json``, checks whether the access token expires
    within *CLAUDE_AUTH_REFRESH_BEFORE_SECONDS*, and POSTs a refresh_token
    grant to the Anthropic OAuth token endpoint when needed.  Rotated
    refresh tokens are persisted immediately — losing the rotated token
    strands the volume until a manual re-login.
    """
    global _claude_auth_refresh_state
    try:
        while True:
            await asyncio.sleep(interval_sec)
            try:
                # Check current status — skip if not authenticated.
                status = await backend.check_claude_auth(CLAUDE_AUTH_VOLUME)
            except NotImplementedError:
                return  # backend does not support claude auth -> nothing to do
            except Exception:
                logger.debug(
                    "Claude auth refresh: check_claude_auth failed", exc_info=True
                )
                continue

            if status.get("status") != "authenticated":
                continue

            # Read credentials to inspect expiry and refresh token.
            try:
                creds = await backend.read_claude_credentials(CLAUDE_AUTH_VOLUME)
            except Exception:
                logger.debug(
                    "Claude auth refresh: read_claude_credentials failed", exc_info=True
                )
                continue

            oauth = creds.get("claudeAiOauth", {})
            if not isinstance(oauth, dict):
                continue

            refresh_token = oauth.get("refreshToken")
            expires_at_ms = oauth.get("expiresAt")

            if not refresh_token or not expires_at_ms:
                continue  # nothing to refresh without these

            now_ms = int(time.time() * 1000)
            if expires_at_ms - now_ms > CLAUDE_AUTH_REFRESH_BEFORE_SECONDS * 1000:
                continue  # not close enough to expiry

            # --- Perform the refresh --------------------------------------
            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    resp = await client.post(
                        CLAUDE_AUTH_TOKEN_URL,
                        json={
                            "grant_type": "refresh_token",
                            "refresh_token": refresh_token,
                            "client_id": CLAUDE_AUTH_CLIENT_ID,
                        },
                        headers={"User-Agent": CLAUDE_AUTH_USER_AGENT},
                    )
                except Exception as exc:
                    _claude_auth_refresh_state = {
                        "last_refresh": time.monotonic(),
                        "last_error": f"Token endpoint unreachable: {exc}",
                    }
                    logger.warning("Claude auth refresh: request failed: %s", exc)
                    continue

            if resp.status_code != 200:
                error_detail = resp.text[:500]
                try:
                    error_detail = (
                        resp.json().get("error", {}).get("message", error_detail)
                    )
                except Exception:  # noqa: S110 — non-JSON body is fine
                    pass
                _claude_auth_refresh_state = {
                    "last_refresh": time.monotonic(),
                    "last_error": f"Refresh failed ({resp.status_code}): {error_detail}",
                }
                logger.warning("Claude auth refresh: %s", error_detail)
                continue

            try:
                payload: dict[str, Any] = resp.json()
            except Exception as exc:
                _claude_auth_refresh_state = {
                    "last_refresh": time.monotonic(),
                    "last_error": f"Invalid JSON in refresh response: {exc}",
                }
                logger.warning("Claude auth refresh: bad response JSON: %s", exc)
                continue

            access_token = payload.get("access_token")
            new_refresh_token = payload.get("refresh_token", refresh_token)
            expires_in = payload.get("expires_in", 0)

            if not access_token:
                _claude_auth_refresh_state = {
                    "last_refresh": time.monotonic(),
                    "last_error": "No access_token in refresh response",
                }
                logger.warning("Claude auth refresh: no access_token in response")
                continue

            # Build new credentials blob — always persist the rotated
            # refresh token from the server (the ticket gotcha).
            new_creds: dict[str, Any] = {
                "claudeAiOauth": {
                    "accessToken": access_token,
                    "refreshToken": new_refresh_token,
                    "expiresAt": int((time.time() + float(expires_in)) * 1000),
                    "scopes": oauth.get("scopes", ["user:inference"]),
                }
            }
            # Preserve optional fields from the original credential blob.
            for key in ("subscriptionType", "rateLimitTier"):
                if key in oauth:
                    new_creds["claudeAiOauth"][key] = oauth[key]

            try:
                await backend.write_claude_credentials(
                    CLAUDE_AUTH_VOLUME, json.dumps(new_creds, indent=2)
                )
            except Exception as exc:
                _claude_auth_refresh_state = {
                    "last_refresh": time.monotonic(),
                    "last_error": f"Failed to write refreshed credentials: {exc}",
                }
                logger.warning("Claude auth refresh: write failed: %s", exc)
                continue

            _claude_auth_refresh_state = {
                "last_refresh": time.monotonic(),
                "last_error": None,
            }
            logger.info("Claude auth credentials refreshed successfully")

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
            timeout=cfg.docker_sdk_timeout,
        )
    if cfg.execution_backend == ExecutionBackendType.DOCKER:
        return DockerBackend()
    return NoopBackend()


# ---------------------------------------------------------------------------
# Onboard job registry (in-memory, single-process)
# ---------------------------------------------------------------------------


class OnboardJob:
    """In-memory record of one onboard confirm background deploy job."""

    __slots__ = (
        "job_id",
        "component",
        "phase",
        "error",
        "name",
        "image",
        "state",
        "warnings",
    )

    def __init__(self, job_id: str, component: str) -> None:
        self.job_id: str = job_id
        self.component: str = component
        self.phase: OnboardJobPhase = "writing_config"
        self.error: str | None = None
        self.name: str | None = None
        self.image: str | None = None
        self.state: str | None = None
        self.warnings: list[str] = []


class DeployJob:
    """In-memory record of one background deploy job (API-initiated)."""

    __slots__ = (
        "job_id",
        "component",
        "phase",
        "error",
        "name",
        "image",
        "state",
        "warnings",
    )

    def __init__(self, job_id: str, component: str) -> None:
        self.job_id: str = job_id
        self.component: str = component
        self.phase: DeployJobPhase = "deploying"
        self.error: str | None = None
        self.name: str | None = None
        self.image: str | None = None
        self.state: str | None = None
        self.warnings: list[str] = []


class JobRegistry:
    """Thread-safe-ish in-memory registry for onboard and deploy background jobs.

    The app is single-process asyncio; no lock is needed for simple
    dict access under the same event loop.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, OnboardJob] = {}
        self._deploy_jobs: dict[str, DeployJob] = {}
        self._counter: int = 0

    # -- onboard jobs -------------------------------------------------------

    def create(self, component: str) -> str:
        """Create a new onboard job and return its id."""
        self._counter += 1
        job_id = f"{component}-{self._counter}"
        self._jobs[job_id] = OnboardJob(job_id=job_id, component=component)
        return job_id

    def get(self, job_id: str) -> OnboardJob | None:
        """Return an onboard job by id, or None."""
        return self._jobs.get(job_id)

    def update_phase(self, job_id: str, phase: OnboardJobPhase) -> None:
        """Update the phase of an onboard job."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = phase

    def mark_failed(self, job_id: str, error: str) -> None:
        """Mark an onboard job as failed with an error string."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = "failed"
            job.error = error

    def mark_done(
        self,
        job_id: str,
        name: str,
        image: str,
        state: str,
        warnings: list[str] | None = None,
    ) -> None:
        """Mark an onboard job as done with terminal fields."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.phase = "done"
            job.name = name
            job.image = image
            job.state = state
            job.warnings = warnings or []

    def has_active_job_for(self, component: str) -> bool:
        """Return True when an onboard job for *component* is still in flight."""
        return any(
            j.component == component and j.phase not in ("done", "failed")
            for j in self._jobs.values()
        )

    # -- deploy jobs --------------------------------------------------------

    def create_deploy(self, component: str) -> str:
        """Create a new deploy job and return its id."""
        self._counter += 1
        job_id = f"{component}-{self._counter}"
        self._deploy_jobs[job_id] = DeployJob(job_id=job_id, component=component)
        return job_id

    def get_deploy(self, job_id: str) -> DeployJob | None:
        """Return a deploy job by id, or None."""
        return self._deploy_jobs.get(job_id)

    def update_deploy_phase(self, job_id: str, phase: DeployJobPhase) -> None:
        """Update the phase of a deploy job."""
        job = self._deploy_jobs.get(job_id)
        if job is not None:
            job.phase = phase

    def mark_deploy_failed(self, job_id: str, error: str) -> None:
        """Mark a deploy job as failed with an error string."""
        job = self._deploy_jobs.get(job_id)
        if job is not None:
            job.phase = "failed"
            job.error = error

    def mark_deploy_done(
        self,
        job_id: str,
        name: str,
        image: str,
        state: str,
        warnings: list[str] | None = None,
    ) -> None:
        """Mark a deploy job as done with terminal fields."""
        job = self._deploy_jobs.get(job_id)
        if job is not None:
            job.phase = "done"
            job.name = name
            job.image = image
            job.state = state
            job.warnings = warnings or []

    def active_deploy_job_id_for(self, component: str) -> str | None:
        """Return the job_id of an active deploy job for *component*, or None."""
        for job in self._deploy_jobs.values():
            if job.component == component and job.phase not in ("done", "failed"):
                return job.job_id
        return None


# ---------------------------------------------------------------------------
# Lifespan — wire up store & backend from config
# ---------------------------------------------------------------------------


async def _init_config(app: FastAPI) -> None:
    """Load config from environment and construct core stores.

    Attaches ``config``, ``store``, ``key_manager``, ``env_store``,
    ``config_yaml_store``, and ``deploy_history_store`` to ``app.state``.
    Sets the module-level ``_config`` and ``_store`` globals.
    """
    global _config, _store
    import robotsix_config

    _config = robotsix_config.load_config(LifecycleConfig)
    _store = _build_store(_config)
    _key_manager = SecretKeyManager(Path(_config.secret_key_path))
    _env_store = EnvStore(Path(_config.env_store_path), _key_manager)
    _config_yaml_store = ConfigYamlStore(Path(_config.config_yaml_store_path))
    _deploy_history_store = DeployHistoryStore(
        _config.effective_deploy_history_store_path
    )
    app.state.config = _config
    app.state.store = _store
    app.state.key_manager = _key_manager
    app.state.env_store = _env_store
    app.state.config_yaml_store = _config_yaml_store
    app.state.deploy_history_store = _deploy_history_store


async def _init_settings(app: FastAPI) -> None:
    """Seed system settings on first boot, overlay persisted settings onto
    config, and construct the execution backend.

    Attaches ``settings_store``, ``backend``, ``session_store``, and
    ``job_registry`` to ``app.state``.  Applies the (possibly overlaid)
    log level to the root logger.  Updates the module-level ``_config``
    and ``_backend`` globals.
    """
    global _config, _backend
    assert _config is not None
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
                caretaker_enabled=_config.caretaker_enabled,
                caretaker_interval_hours=_config.caretaker_interval_hours,
                claude_auth_refresh_interval=_config.claude_auth_refresh_interval,
            )
        )

    _config = settings_store.overlay(
        _config
    )  # returns new LifecycleConfig (or same if no file)
    app.state.config = _config  # replace with overlaid version

    # -- Backend (constructed from overlaid config) ----------------------
    _backend = _build_backend(_config)
    app.state.backend = _backend

    # -- Session store (in-memory, no I/O) ------------------------------
    from .session import SessionStore

    app.state.session_store = SessionStore()
    app.state.job_registry = JobRegistry()

    # Apply log_level from (possibly overlaid) config
    logging.getLogger().setLevel(_config.log_level)


async def _init_background_tasks(app: FastAPI) -> None:
    """Create shared HTTP client and registry checker; start the registry-
    check background loop when configured.

    Attaches ``registry_checker``, ``http_client``, and ``_bg_task`` to
    ``app.state``.  Sets the module-level ``_registry_checker`` and
    ``_http_client`` globals.
    """
    global _registry_checker, _http_client
    assert _config is not None
    assert _store is not None
    assert _backend is not None

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
    app.state._bg_task = bg_task

    # -- Claude auth credential refresh ----------------------------------
    claude_auth_task = None
    if _config.claude_auth_refresh_interval > 0:
        claude_auth_task = asyncio.create_task(
            _claude_auth_refresh_loop(
                _backend,
                _config.claude_auth_refresh_interval,
            )
        )
    app.state._claude_auth_task = claude_auth_task

    logger.info(
        "lifecycle server starting — store=%s backend=%s auth=%s",
        type(_store).__name__,
        type(_backend).__name__,
        "on" if _config.auth_required else "off",
    )


async def _init_component_registry(app: FastAPI) -> None:
    """Load persisted component configs into the in-memory registry, seed
    sibling service records, and start the volume-audit and caretaker
    subsystems.

    Attaches ``registry``, ``component_config_store``,
    ``volume_audit_scheduler``, ``caretaker_scheduler``, and the
    background-task handles (``_caretaker_task``, ``_volume_audit_task``)
    to ``app.state``.  Sets the module-level ``_volume_audit_scheduler``
    global.
    """
    global _volume_audit_scheduler
    assert _config is not None
    assert _store is not None
    assert _backend is not None
    assert _http_client is not None

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

    # --- Volume audit subsystem ---
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
    app.state._volume_audit_task = _volume_audit_task

    # --- Caretaker subsystem ---
    caretaker_scheduler = CaretakerScheduler(
        config=_config,
        backend=_backend,
        registry=registry,
        service_store=_store,
        component_config_store=component_config_store,
        volume_audit_scheduler=_volume_audit_scheduler,
        settings_store=app.state.settings_store,
        http_client=_http_client,
        deploy_history_store=app.state.deploy_history_store,
    )
    app.state.caretaker_scheduler = caretaker_scheduler

    initial_settings = await app.state.settings_store.get()
    _caretaker_task = asyncio.create_task(caretaker_scheduler.loop())
    app.state._caretaker_task = _caretaker_task

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
            app.state._volume_audit_task = _volume_audit_task


async def _teardown(app: FastAPI) -> None:
    """Cancel background tasks and close the shared HTTP client.

    Reads task references from ``app.state`` (``_caretaker_task``,
    ``_volume_audit_task``, ``_bg_task``) that were stored during
    initialisation.
    """
    assert _http_client is not None
    _caretaker_task: asyncio.Task[Any] = app.state._caretaker_task
    _caretaker_task.cancel()
    with suppress(asyncio.CancelledError):
        await _caretaker_task

    _volume_audit_task: asyncio.Task[Any] | None = app.state._volume_audit_task
    if _volume_audit_task and not _volume_audit_task.done():
        _volume_audit_task.cancel()
        with suppress(asyncio.CancelledError):
            await _volume_audit_task

    bg_task: asyncio.Task[Any] | None = app.state._bg_task
    if bg_task:
        bg_task.cancel()
        await asyncio.gather(bg_task, return_exceptions=True)

    claude_auth_task: asyncio.Task[Any] | None = getattr(
        app.state, "_claude_auth_task", None
    )
    if claude_auth_task:
        claude_auth_task.cancel()
        await asyncio.gather(claude_auth_task, return_exceptions=True)

    await _http_client.aclose()
    logger.info("lifecycle server shutting down")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan — initialise stores, backends, background tasks,
    and component registry; tear down on shutdown."""
    await _init_config(app)
    await _init_settings(app)
    await _init_background_tasks(app)
    await _init_component_registry(app)

    yield

    await _teardown(app)


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


async def _get_deploy_history_store(request: Request) -> DeployHistoryStore:
    return request.app.state.deploy_history_store  # type: ignore[no-any-return]


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

    new_config_vol = (
        old_to_new.get(spec.config_volume, spec.config_volume)
        if spec.config_volume is not None
        else None
    )

    return spec.model_copy(
        update={
            "volume_mounts": new_primary_mounts,
            "siblings": new_siblings,
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


def _validate_config_or_422(schema: dict[str, Any], values: dict[str, Any]) -> None:
    """Validate *values* against JSON Schema, raising HTTP 422 on failure."""
    import jsonschema

    try:
        jsonschema.validate(instance=values, schema=schema)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path)
        loc = f" at '{path}'" if path else ""
        raise HTTPException(
            status_code=422,
            detail={
                "error": f"Config validation error{loc}: {exc.message}",
            },
        )


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
