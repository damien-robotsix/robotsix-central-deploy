"""Settings API — operator-configured runtime parameters for central-deploy.

``GET  /settings``   — return current settings (secrets masked).
``PUT  /settings``   — update settings, persist, and hot-apply where possible.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from starlette.requests import Request

from ..lifecycle.auth import verify_auth
from ..registry.settings_store import (
    VALID_LOG_LEVELS,
    SystemSettings,
    SystemSettingsStore,
)

settings_router = APIRouter(tags=["settings"])

SECRET_MASK = "***"


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class SystemSettingsResponse(BaseModel):
    auth_username: str = ""
    auth_password: str = ""
    disk_warn_pct: float = 10.0
    registry_check_interval: int = 300
    log_level: str = "INFO"
    gateway_base_domain: str = ""
    claude_host_mount_path: str = ""
    caretaker_enabled: bool = False
    caretaker_interval_hours: int = 24
    mill_component_id: str = "mill"


class SystemSettingsUpdate(BaseModel):
    auth_username: str = ""
    auth_password: str = ""
    disk_warn_pct: float = 10.0
    registry_check_interval: int = 300
    log_level: str = "INFO"
    gateway_base_domain: str = ""
    claude_host_mount_path: str = ""
    caretaker_enabled: bool = False
    caretaker_interval_hours: int = 24
    mill_component_id: str = "mill"

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        normalised = v.upper()
        if normalised not in VALID_LOG_LEVELS:
            raise ValueError(
                f"Unknown log level '{v}'. Valid: {', '.join(sorted(VALID_LOG_LEVELS))}"
            )
        return normalised

    @field_validator("caretaker_interval_hours")
    @classmethod
    def _validate_caretaker_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("caretaker_interval_hours must be >= 1")
        return v

    @field_validator("mill_component_id")
    @classmethod
    def _validate_mill_component_id(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("mill_component_id must not be empty")
        return stripped


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask_response(settings: SystemSettings) -> SystemSettingsResponse:
    return SystemSettingsResponse(
        auth_username=settings.auth_username,
        auth_password=SECRET_MASK if settings.auth_password else "",
        disk_warn_pct=settings.disk_warn_pct,
        registry_check_interval=settings.registry_check_interval,
        log_level=settings.log_level,
        gateway_base_domain=settings.gateway_base_domain,
        claude_host_mount_path=settings.claude_host_mount_path,
        caretaker_enabled=settings.caretaker_enabled,
        caretaker_interval_hours=settings.caretaker_interval_hours,
        mill_component_id=settings.mill_component_id,
    )


async def _get_settings_store(request: Request) -> SystemSettingsStore:
    store: object = request.app.state.settings_store
    return store  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# GET /settings
# ---------------------------------------------------------------------------


@settings_router.get("/settings", response_model=SystemSettingsResponse)
async def get_settings(
    request: Request,
    settings_store: SystemSettingsStore = Depends(_get_settings_store),
    _auth: None = Depends(verify_auth),
) -> SystemSettingsResponse:
    """Return current system settings. Secrets are returned as ``"***"``
    when set, or ``""`` when empty.

    Reads from the *effective* config (env-var values overlaid by any
    stored settings) so that env-var credentials are visible in the UI
    even before the operator has saved settings via the UI.
    """
    effective_config = settings_store.overlay(request.app.state.config)
    effective = SystemSettings(
        auth_username=effective_config.auth_username,
        auth_password=effective_config.auth_password,
        disk_warn_pct=effective_config.disk_warn_pct,
        registry_check_interval=effective_config.registry_check_interval,
        log_level=effective_config.log_level,
        gateway_base_domain=effective_config.gateway_base_domain,
        claude_host_mount_path=effective_config.claude_host_mount_path,
        caretaker_enabled=effective_config.caretaker_enabled,
        caretaker_interval_hours=effective_config.caretaker_interval_hours,
        mill_component_id=effective_config.mill_component_id,
    )
    return _mask_response(effective)


# ---------------------------------------------------------------------------
# PUT /settings
# ---------------------------------------------------------------------------


@settings_router.put("/settings", response_model=SystemSettingsResponse)
async def put_settings(
    body: SystemSettingsUpdate,
    request: Request,
    settings_store: SystemSettingsStore = Depends(_get_settings_store),
    _auth: None = Depends(verify_auth),
) -> SystemSettingsResponse:
    """Update system settings, persist to disk, and hot-apply where possible.

    - Secret fields sent as ``"***"`` preserve the existing stored value.
    - ``log_level`` is validated via Pydantic; an invalid value returns 422.
    - All settings *except* ``registry_check_interval`` and
      ``claude_host_mount_path`` take effect immediately without restart.

    # NOTE: ``registry_check_interval`` changes are persisted and reflected
      in ``app.state.config``, but the background ``_registry_check_loop``
      captures its interval at startup via a positional argument.  Updating
      the config alone does **not** alter the running task's sleep period.
      A full service restart is required for an interval change to take
      effect on the background loop.

    # NOTE: ``claude_host_mount_path`` is captured by ``DockerSdkBackend``
      at construction time.  Updating the config alone does **not** alter
      the running backend's mount path.  A full service restart is required
      for a change to take effect.
    """
    SECRET = SECRET_MASK
    current = await settings_store.get()

    # Preserve secrets when masked value sent
    new = SystemSettings(
        auth_username=body.auth_username,
        auth_password=body.auth_password
        if body.auth_password != SECRET
        else current.auth_password,
        disk_warn_pct=body.disk_warn_pct,
        registry_check_interval=body.registry_check_interval,
        log_level=body.log_level,
        gateway_base_domain=body.gateway_base_domain,
        claude_host_mount_path=body.claude_host_mount_path,
        caretaker_enabled=body.caretaker_enabled,
        caretaker_interval_hours=body.caretaker_interval_hours,
        mill_component_id=body.mill_component_id,
    )

    await settings_store.put(new)

    # Hot-apply: overlay into the running config so all dependency
    # injections pick up the changes immediately.
    new_config = settings_store.overlay(request.app.state.config)
    request.app.state.config = new_config

    # Hot-apply log_level immediately
    logging.getLogger().setLevel(new.log_level)

    return _mask_response(new)
