"""Lifespan init/teardown — wires up store, backend, registry, and background tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from ..backends import DockerBackend, DockerSdkBackend, ExecutionBackend, NoopBackend
from ..config import LifecycleConfig, VirtualComponentEntry
from ..models import ExecutionBackendType, ServiceRecord, StoreBackend
from ..store import FileStore, InMemoryStore, ServiceStore
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.deploy_history_store import DeployHistoryStore
from ...registry.chat_agent_audit_store import ChatAgentAuditStore
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig
from ...registry.secret_key import SecretKeyManager
from ...registry_check import RegistryChecker
from ...caretaker.scheduler import CaretakerScheduler
from ...volume_audit.scheduler import VolumeAuditScheduler
from .background import _claude_auth_refresh_loop, _registry_check_loop
from .jobs import JobRegistry

logger = logging.getLogger(__name__)

#: Module-level state set during lifespan initialisation.
_config: LifecycleConfig | None = None
_store: ServiceStore | None = None
_backend: ExecutionBackend | None = None
_registry_checker: RegistryChecker | None = None
_http_client: httpx.AsyncClient | None = None
_volume_audit_scheduler: VolumeAuditScheduler | None = None


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
    _chat_agent_audit_store = ChatAgentAuditStore(
        _config.effective_chat_agent_audit_store_path
    )
    app.state.config = _config
    app.state.store = _store
    app.state.key_manager = _key_manager
    app.state.env_store = _env_store
    app.state.config_yaml_store = _config_yaml_store
    app.state.deploy_history_store = _deploy_history_store
    app.state.chat_agent_audit_store = _chat_agent_audit_store
    app.state.chat_agent_rate_limits = {}


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
    from ...registry.settings_store import SystemSettings, SystemSettingsStore

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
    from ..session import SessionStore

    app.state.session_store = SessionStore()
    app.state.job_registry = JobRegistry()

    # -- Rate limiter (in-memory, no I/O) -------------------------------
    from ..rate_limiter import RateLimitStore

    app.state.rate_limit_store = RateLimitStore()

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


async def _seed_component_registry(
    store: ServiceStore,
    component_config_store: ComponentConfigStore,
    registry: ComponentRegistry,
    virtual_components: list[VirtualComponentEntry],
) -> None:
    """Merge persisted component configs into the in-memory registry and
    ServiceStore, then seed any virtual (non-Docker) components not yet
    registered.

    Virtual components are never Docker containers, so they must never get
    a ``ServiceRecord`` — that would surface them as permanently
    "unknown"-status rows in the dashboard. Any ``ServiceRecord`` that
    already exists for one (e.g. leaked in by a previous restart before
    this guard existed) is deleted here so the fix self-heals. Configs
    persisted before ``is_virtual`` existed are backfilled by matching
    against the current ``virtual_components`` ids.
    """
    virtual_ids = {ventry.id for ventry in virtual_components}
    for dyn_config in component_config_store.all():
        if dyn_config.id in virtual_ids and not dyn_config.is_virtual:
            dyn_config = dyn_config.model_copy(update={"is_virtual": True})
            component_config_store.register(dyn_config)
        registry.register(dyn_config)
        if dyn_config.is_virtual:
            await store.delete(dyn_config.id)
            continue
        existing = await store.get(dyn_config.id)
        if existing is None:
            await store.put(
                ServiceRecord(
                    name=dyn_config.id,
                    container_name=dyn_config.container_name,
                    image=dyn_config.image,
                )
            )
        # Seed sibling records
        for sib in dyn_config.siblings:
            sib_name = f"{dyn_config.id}-{sib.service_key}"
            existing_sib = await store.get(sib_name)
            if existing_sib is None:
                await store.put(
                    ServiceRecord(
                        name=sib_name,
                        container_name=sib.container_name,
                        image=sib.image,
                        component_id=dyn_config.id,
                    )
                )
        logger.info("Loaded dynamic component config for '%s'", dyn_config.id)

    # -- Seed virtual (non-Docker) components from config --------------------
    for ventry in virtual_components:
        if component_config_store.get(ventry.id) is not None:
            continue  # already registered; don't overwrite
        virtual_cfg = ComponentConfig(
            id=ventry.id,
            image="",
            container_name=ventry.id,
            is_virtual=True,
            allow_chat_access=True,
            chat_base_url=ventry.chat_base_url or None,
            chat_skill_endpoint=ventry.chat_skill_endpoint,
            chat_skill=ventry.chat_skill,
            auth_type=ventry.auth_type,
            auth_header_name=ventry.auth_header_name,
            auth_username_env=ventry.auth_username_env,
            auth_password_env=ventry.auth_password_env,
            auth_token_env=ventry.auth_token_env,
        )
        component_config_store.register(virtual_cfg)
        registry.register(virtual_cfg)
        logger.info("Seeded virtual component '%s'", ventry.id)

    if virtual_components:
        logger.info(
            "Virtual components seeded into the chat-agent roster. "
            "The robotsix-chat agent must be restarted to pick up the "
            "updated roster (POST /chat/services/chat/restart)."
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

    await _seed_component_registry(
        _store, component_config_store, registry, _config.virtual_components
    )

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
        env_store=app.state.env_store,
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await _init_config(app)
    await _init_settings(app)
    await _init_background_tasks(app)
    await _init_component_registry(app)
    yield
    await _teardown(app)
