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
    ghcr_token: str = ""
    auth_username: str = ""
    auth_password: str = ""
    disk_warn_percent: float = 10.0
    registry_check_interval: int = 300
    log_level: str = "INFO"
    gateway_base_domain: str = ""
    claude_host_mount_path: str = ""


class SystemSettingsUpdate(BaseModel):
    ghcr_token: str = ""
    auth_username: str = ""
    auth_password: str = ""
    disk_warn_percent: float = 10.0
    registry_check_interval: int = 300
    log_level: str = "INFO"
    gateway_base_domain: str = ""
    claude_host_mount_path: str = ""

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        normalised = v.upper()
        if normalised not in VALID_LOG_LEVELS:
            raise ValueError(
                f"Unknown log level '{v}'. Valid: {', '.join(sorted(VALID_LOG_LEVELS))}"
            )
        return normalised


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask_response(settings: SystemSettings) -> SystemSettingsResponse:
    return SystemSettingsResponse(
        ghcr_token=SECRET_MASK if settings.ghcr_token else "",
        auth_username=settings.auth_username,
        auth_password=SECRET_MASK if settings.auth_password else "",
        disk_warn_percent=settings.disk_warn_percent,
        registry_check_interval=settings.registry_check_interval,
        log_level=settings.log_level,
        gateway_base_domain=settings.gateway_base_domain,
        claude_host_mount_path=settings.claude_host_mount_path,
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
        ghcr_token=effective_config.ghcr_token,
        auth_username=effective_config.auth_username,
        auth_password=effective_config.auth_password,
        disk_warn_percent=effective_config.disk_warn_percent,
        registry_check_interval=effective_config.registry_check_interval,
        log_level=effective_config.log_level,
        gateway_base_domain=effective_config.gateway_base_domain,
        claude_host_mount_path=effective_config.claude_host_mount_path,
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
        ghcr_token=body.ghcr_token if body.ghcr_token != SECRET else current.ghcr_token,
        auth_username=body.auth_username,
        auth_password=body.auth_password
        if body.auth_password != SECRET
        else current.auth_password,
        disk_warn_percent=body.disk_warn_percent,
        registry_check_interval=body.registry_check_interval,
        log_level=body.log_level,
        gateway_base_domain=body.gateway_base_domain,
        claude_host_mount_path=body.claude_host_mount_path,
    )

    await settings_store.put(new)

    # Hot-apply: overlay into the running config so all dependency
    # injections pick up the changes immediately.
    new_config = settings_store.overlay(request.app.state.config)
    request.app.state.config = new_config

    # Hot-apply log_level immediately
    logging.getLogger().setLevel(new.log_level)

    # Hot-apply ghcr_token to the running RegistryChecker so that
    # future registry polls use the updated token without restart.
    checker = getattr(request.app.state, "registry_checker", None)
    if checker is not None:
        checker.set_ghcr_token(new.ghcr_token)

    return _mask_response(new)
