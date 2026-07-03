"""Settings API — operator-configured runtime parameters for central-deploy.

``GET  /settings``   — return current settings (secrets masked).
``PUT  /settings``   — update settings, persist, and hot-apply where possible.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from starlette.requests import Request

from ..lifecycle.auth import verify_auth
from ..registry.settings_store import SystemSettings, SystemSettingsStore

settings_router = APIRouter(tags=["settings"])

SECRET_MASK = "***"


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class SystemSettingsResponse(SystemSettings):
    """Response model — inherits all fields from SystemSettings."""


class SystemSettingsUpdate(SystemSettings):
    """Update model — inherits all fields and validators from SystemSettings."""


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
        caretaker_enabled=settings.caretaker_enabled,
        caretaker_interval_hours=settings.caretaker_interval_hours,
        mill_component_id=settings.mill_component_id,
        image_auto_prune=settings.image_auto_prune,
        claude_auth_helper_image=settings.claude_auth_helper_image,
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
        caretaker_enabled=effective_config.caretaker_enabled,
        caretaker_interval_hours=effective_config.caretaker_interval_hours,
        mill_component_id=effective_config.mill_component_id,
        image_auto_prune=effective_config.image_auto_prune,
        claude_auth_helper_image=effective_config.claude_auth_helper_image,
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
    - All settings *except* ``registry_check_interval`` take effect immediately
      without restart.

    # NOTE: ``registry_check_interval`` changes are persisted and reflected
      in ``app.state.config``, but the background ``_registry_check_loop``
      captures its interval at startup via a positional argument.  Updating
      the config alone does **not** alter the running task's sleep period.
      A full service restart is required for an interval change to take
      effect on the background loop.

    # NOTE: Backend-consumed settings (e.g. ``docker_socket_url``,
      ``docker_sdk_timeout``) are captured by
      ``DockerSdkBackend`` at construction time — the backend is now built
      *after* the ``system_settings.json`` overlay at startup, so a full
      service restart applies these settings.  Updating the config at
      runtime alone does **not** alter the running backend.
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
        caretaker_enabled=body.caretaker_enabled,
        caretaker_interval_hours=body.caretaker_interval_hours,
        mill_component_id=body.mill_component_id,
        image_auto_prune=body.image_auto_prune,
        claude_auth_helper_image=body.claude_auth_helper_image,
    )

    await settings_store.put(new)

    # Hot-apply: overlay into the running config so all dependency
    # injections pick up the changes immediately.
    new_config = settings_store.overlay(request.app.state.config)
    request.app.state.config = new_config

    # Hot-apply log_level immediately
    logging.getLogger().setLevel(new.log_level)

    return _mask_response(new)
