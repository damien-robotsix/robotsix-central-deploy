"""Settings API — operator-configured runtime parameters for central-deploy.

``GET  /settings``   — return current settings (secrets masked).
``PUT  /settings``   — update settings, persist, and hot-apply where possible.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
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
    disk_warn_bytes: int = 5_368_709_120
    registry_check_interval: int = 300
    log_level: str = "INFO"


class SystemSettingsUpdate(BaseModel):
    ghcr_token: str = ""
    auth_username: str = ""
    auth_password: str = ""
    disk_warn_bytes: int = 5_368_709_120
    registry_check_interval: int = 300
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        normalised = v.upper()
        if normalised not in VALID_LOG_LEVELS:
            raise ValueError(
                f"Unknown log level '{v}'. "
                f"Valid: {', '.join(sorted(VALID_LOG_LEVELS))}"
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
        disk_warn_bytes=settings.disk_warn_bytes,
        registry_check_interval=settings.registry_check_interval,
        log_level=settings.log_level,
    )


async def _get_settings_store(request: Request) -> SystemSettingsStore:
    return request.app.state.settings_store


# ---------------------------------------------------------------------------
# GET /settings
# ---------------------------------------------------------------------------


@settings_router.get("/settings", response_model=SystemSettingsResponse)
async def get_settings(
    settings_store: SystemSettingsStore = Depends(_get_settings_store),
    _auth: None = Depends(verify_auth),
) -> SystemSettingsResponse:
    """Return current system settings. Secrets are returned as ``"***"``
    when set, or ``""`` when empty."""
    stored = await settings_store.get()
    return _mask_response(stored)


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
    - All settings *except* ``registry_check_interval`` take effect
      immediately without restart.

    # NOTE: ``registry_check_interval`` changes are persisted and reflected
      in ``app.state.config``, but the background ``_registry_check_loop``
      captures its interval at startup via a positional argument.  Updating
      the config alone does **not** alter the running task's sleep period.
      A full service restart is required for an interval change to take
      effect on the background loop.
    """
    SECRET = SECRET_MASK
    current = await settings_store.get()

    # Preserve secrets when masked value sent
    new = SystemSettings(
        ghcr_token=body.ghcr_token if body.ghcr_token != SECRET else current.ghcr_token,
        auth_username=body.auth_username,
        auth_password=body.auth_password if body.auth_password != SECRET else current.auth_password,
        disk_warn_bytes=body.disk_warn_bytes,
        registry_check_interval=body.registry_check_interval,
        log_level=body.log_level,
    )

    await settings_store.put(new)

    # Hot-apply: overlay into the running config so all dependency
    # injections pick up the changes immediately.
    new_config = settings_store.overlay(request.app.state.config)
    request.app.state.config = new_config

    # Hot-apply log_level immediately
    logging.getLogger().setLevel(new.log_level)

    return _mask_response(new)
