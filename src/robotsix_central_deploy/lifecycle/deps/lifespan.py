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
from ...caretaker.volume_audit.scheduler import VolumeAuditScheduler
from .background import _claude_auth_refresh_loop, _registry_check_loop
from .jobs import JobRegistry

logger = logging.getLogger(__name__)

#: Module-level state set during lifespan initialisation.
_config: LifecycleConfig | None = None
_store: ServiceStore | None = None
_backend: ExecutionBackend | None = None
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
            github_app_id=cfg.github_app_id.get_secret_value(),
            github_app_private_key=cfg.github_app_private_key.get_secret_value(),
            installation_id=cfg.installation_id.get_secret_value(),
        )
    if cfg.execution_backend == ExecutionBackendType.DOCKER:
        return DockerBackend()
    return NoopBackend()


# ---------------------------------------------------------------------------
# Lifespan — wire up store & backend from config
# ---------------------------------------------------------------------------


def _parse_self_contract_settings(config: LifecycleConfig) -> "SystemSettings | None":  # type: ignore[name-defined]  # noqa: F821
    """Parse central-deploy's own deploy contract and extract system settings.

    Reads the YAML file at ``config.self_contract_path``, looks for the
    primary service (or the first service), and extracts labels with the
    ``robotsix.deploy.settings.`` prefix.  Each label key suffix maps to
    a ``SystemSettings`` field.

    Returns ``None`` when the contract file does not exist or cannot be
    parsed — the caller should fall back to env-var defaults.
    """
    import json
    from pathlib import Path

    import yaml

    from ...registry.settings_store import SystemSettings

    contract_path = Path(config.self_contract_path)
    if not contract_path.exists():
        return None

    try:
        raw = yaml.safe_load(contract_path.read_bytes())
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("Self-contract %s: YAML parse failed — %s", contract_path, exc)
        return None

    if not isinstance(raw, dict):
        logger.warning(
            "Self-contract %s: expected a mapping, got %s",
            contract_path,
            type(raw).__name__,
        )
        return None

    services = raw.get("services")
    if not isinstance(services, dict) or not services:
        logger.warning("Self-contract %s: no services defined", contract_path)
        return None

    # Pick the primary service (robotsix.deploy.primary label) or fall
    # back to the first service.
    primary_name: str | None = None
    for svc_name, svc_def in services.items():
        if isinstance(svc_def, dict):
            svc_labels = svc_def.get("labels") or {}
            if (
                isinstance(svc_labels, dict)
                and svc_labels.get("robotsix.deploy.primary") == "true"
            ):
                primary_name = svc_name
                break
    if primary_name is None:
        primary_name = next(iter(services))

    svc_def = services[primary_name]
    if not isinstance(svc_def, dict):
        logger.warning(
            "Self-contract %s: service %s is not a mapping", contract_path, primary_name
        )
        return None

    labels = svc_def.get("labels") or {}
    if not isinstance(labels, dict):
        labels = {}

    PREFIX = "robotsix.deploy.settings."
    settings_kwargs: dict[str, object] = {}
    for label_key, label_value in labels.items():
        if not isinstance(label_key, str) or not label_key.startswith(PREFIX):
            continue
        field_name = label_key[len(PREFIX) :].replace("-", "_")
        str_value = (
            str(label_value) if not isinstance(label_value, str) else label_value
        )

        # Map the string value to the expected type for each known field.
        if field_name in (
            "auth_username",
            "auth_password",
            "log_level",
            "gateway_base_domain",
            "mill_component_id",
        ):
            settings_kwargs[field_name] = str_value
        elif field_name in (
            "registry_check_interval",
            "caretaker_interval_hours",
            "claude_auth_refresh_interval",
            "rate_limit_login_per_minute",
            "rate_limit_api_per_hour",
            "rate_limit_login_max_attempts",
            "rate_limit_login_lockout_seconds",
            "volume_audit_interval_seconds",
            "volume_audit_min_delta_bytes",
        ):
            try:
                settings_kwargs[field_name] = int(str_value)
            except ValueError:
                logger.warning(
                    "Self-contract %s: label %s value %r is not an integer — skipped",
                    contract_path,
                    label_key,
                    str_value,
                )
        elif field_name in ("disk_warn_pct", "volume_audit_growth_threshold_pct"):
            try:
                settings_kwargs[field_name] = float(str_value)
            except ValueError:
                logger.warning(
                    "Self-contract %s: label %s value %r is not a float — skipped",
                    contract_path,
                    label_key,
                    str_value,
                )
        elif field_name in (
            "caretaker_enabled",
            "image_auto_prune",
            "volume_audit_enabled",
        ):
            settings_kwargs[field_name] = str_value.lower() in ("true", "1", "yes")
        elif field_name == "llmio_tier_config":
            try:
                settings_kwargs[field_name] = json.loads(str_value)
            except json.JSONDecodeError:
                logger.warning(
                    "Self-contract %s: label %s value is not valid JSON — skipped",
                    contract_path,
                    label_key,
                )
        else:
            logger.debug(
                "Self-contract %s: unknown settings label %s — skipped",
                contract_path,
                label_key,
            )

    if not settings_kwargs:
        return None

    try:
        return SystemSettings(**settings_kwargs)
    except Exception as exc:
        logger.warning(
            "Self-contract %s: failed to build SystemSettings — %s", contract_path, exc
        )
        return None


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

    # Seed OVH website credentials from config (one-time, idempotent).
    await _seed_ovh_website_credentials(_env_store, _config)


async def _init_settings(app: FastAPI) -> None:
    """Read self-contract settings, persist to store, overlay onto config,
    and construct the execution backend.

    Attaches ``settings_store``, ``backend``, ``session_store``, and
    ``job_registry`` to ``app.state``.  Applies the (possibly overlaid)
    log level to the root logger.  Updates the module-level ``_config``
    and ``_backend`` globals.
    """
    global _config, _backend
    assert _config is not None
    from ...registry.settings_store import SystemSettings, SystemSettingsStore

    from .._settings_defaults import SETTINGS_DEFAULTS

    settings_store = SystemSettingsStore(_config.effective_system_settings_path)
    app.state.settings_store = settings_store

    # 1. Parse self-contract (deploy/docker-compose.yml) to extract system
    #    settings from labels (robotsix.deploy.settings.*).
    contract_settings = _parse_self_contract_settings(_config)

    # 2. Seed the store: on first boot, write settings from the self-contract
    #    (or fall back to env-var defaults when no contract file exists).
    if not settings_store._path.exists():
        if contract_settings is not None:
            # Merge contract settings with env-var overrides — contract
            # provides the base, env-var LifecycleConfig values override.
            contract_dict = contract_settings.model_dump()
            for key, default_val in SETTINGS_DEFAULTS.items():
                env_val = getattr(_config, key, default_val)
                # SecretStr fields need their value extracted for comparison
                # and for passing to SystemSettings (which uses plain str).
                if hasattr(env_val, "get_secret_value"):
                    env_val = env_val.get_secret_value()
                if env_val != default_val:
                    contract_dict[key] = env_val
            # Special case: when auth_username is empty everywhere, fall back to "admin".
            if not contract_dict.get("auth_username"):
                contract_dict["auth_username"] = "admin"
            await settings_store.put(SystemSettings(**contract_dict))
            logger.info(
                "Seeded system settings from self-contract (%s)",
                _config.self_contract_path,
            )
        else:
            await settings_store.put(
                SystemSettings(
                    auth_username=_config.auth_username or "admin",
                    auth_password=_config.auth_password.get_secret_value(),
                    disk_warn_pct=_config.disk_warn_pct,
                    registry_check_interval=_config.registry_check_interval,
                    log_level=_config.log_level,
                    gateway_base_domain=_config.gateway_base_domain,
                    caretaker_enabled=_config.caretaker_enabled,
                    caretaker_interval_hours=_config.caretaker_interval_hours,
                    claude_auth_refresh_interval=_config.claude_auth_refresh_interval,
                )
            )
            logger.info(
                "Seeded system settings from env-var defaults "
                "(self-contract %s not found)",
                _config.self_contract_path,
            )

    # 3. Overlay persisted settings onto LifecycleConfig.
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
    ``app.state``.  Sets the module-level ``_http_client`` global.
    """
    global _http_client
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


async def _seed_ovh_website_credentials(
    env_store: EnvStore, config: LifecycleConfig
) -> None:
    """Seed OVH website SFTP credentials into the encrypted store on first boot.

    Reads ``ovh_sftp.host``, ``ovh_sftp.port``, ``ovh_sftp.user``, and
    ``ovh_sftp.password`` from the loaded ``LifecycleConfig``.  If any of
    the four are set AND the ``ovh-website-credentials`` entry does not
    already exist in the store, the values are encrypted and stored with
    scope tag ``website:ovh``.  Already-stored credentials are never
    overwritten.
    """
    ovh = config.ovh_sftp
    host = ovh.host.strip()
    port = str(ovh.port)
    user = ovh.user.strip()
    password = ovh.password.get_secret_value()

    if not (host and port and user and password):
        return  # not fully configured — nothing to seed

    existing = await env_store.get("ovh-website-credentials")
    if existing.env or existing.secret_tokens:
        return  # already seeded — don't overwrite

    await env_store.upsert(
        "ovh-website-credentials",
        env={"OVH_SFTP_HOST": host, "OVH_SFTP_PORT": port, "OVH_SFTP_USER": user},
        secrets={"OVH_SFTP_PASSWORD": password},
        env_scopes={
            "OVH_SFTP_HOST": "website:ovh",
            "OVH_SFTP_PORT": "website:ovh",
            "OVH_SFTP_USER": "website:ovh",
        },
        secret_scopes={"OVH_SFTP_PASSWORD": "website:ovh"},
    )
    logger.info("Seeded OVH website SFTP credentials (scope 'website:ovh')")


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
            "The chat agent must be restarted to pick up the "
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

    # -- Self-managed central-deploy service ---------------------------------
    # Register central-deploy itself so it appears in GET /services and the
    # chat agent can restart/update it through the allowlisted chat endpoints.
    if component_config_store.get("central-deploy") is None:
        try:
            self_info = await _backend.inspect_self()
        except NotImplementedError:
            self_info = None
        if self_info is not None:
            central_deploy_cfg = ComponentConfig(
                id="central-deploy",
                image=self_info.image_ref,
                container_name=self_info.container_name,
                chat_agent_mutatable=True,
                is_virtual=False,
                allow_chat_access=False,
            )
            component_config_store.register(central_deploy_cfg)
            registry.register(central_deploy_cfg)
            existing = await _store.get("central-deploy")
            if existing is None:
                await _store.put(
                    ServiceRecord(
                        name="central-deploy",
                        container_name=self_info.container_name,
                        image=self_info.image_ref,
                    )
                )
            logger.info("Registered self-managed 'central-deploy' service")

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
        _ = await _caretaker_task

    _volume_audit_task: asyncio.Task[Any] | None = app.state._volume_audit_task
    if _volume_audit_task and not _volume_audit_task.done():
        _volume_audit_task.cancel()
        with suppress(asyncio.CancelledError):
            _ = await _volume_audit_task

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
