"""Config-related route handlers for the lifecycle server."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from .._config_utils import (
    _mask_secrets,
    _merge_config,
    read_component_config,
)
from .._langfuse_config import build_central_deploy_langfuse_config
from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import (
    _get_backend,
    _get_component_config_store,
    _get_config,
    _get_config_yaml_store,
    _get_or_create_record,
    _get_store,
)
from ..models import ErrorDetail
from ..schemas import ConfigExportResponse, ConfigResponse
from ..store import ServiceStore

logger = logging.getLogger(__name__)


router = APIRouter(tags=["services"])


# ---------------------------------------------------------------------------
# Config ownership (deploy-plane vs component-owned) — robotsix-standards
# config-ownership standard.
#
# Boundary split — two categories of persisted settings:
#
# 1. Docker-boundary (stays in central-deploy — NOT deprecated):
#    * image references (ComponentConfig.image)
#    * port mappings (ComponentConfig.ports)
#    * volume mounts (ComponentConfig.mounts, named_volumes)
#    * boot-time env vars and secrets (EnvStore)
#    * container_name, health_check, siblings, claude_mount, etc.
#    These are deployment-infrastructure concerns that cannot be handled
#    inside a running container and are managed through the deploy UI,
#    env tab, or lifecycle API.
#
# 2. Runtime config (moving to each component — config.json):
#    * application settings stored in config.json per the
#      robotsix-standards per-component settings standard.
#    * The ONLY deploy-plane key in config.json is ``robotsix_config_file``
#      (tells the deploy system where to mount/write the file).
#    * All other config.json keys are component-owned and should be edited
#      through the component's own Settings panel.
#
# The ``DEPLOY_PLANE_KEYS`` frozenset below marks the config.json keys
# that remain managed by the deploy plane.  Everything else in config.json
# is component-owned.
# ---------------------------------------------------------------------------

# Deploy-plane keys: infrastructure-level settings that the deploy system
# manages.  Everything else in a component's config.json is component-owned
# and should be edited through the component's own Settings panel once that
# surface is live.
DEPLOY_PLANE_KEYS: frozenset[str] = frozenset(
    {
        # The ROBOTSIX_CONFIG_FILE pointer tells the deploy system where
        # the component expects its config file inside the container.
        # This is a deploy-plane concern because the deploy system must
        # mount/write the file at the correct path.
        "robotsix_config_file",
    }
)


def _annotate_config_ownership(schema: dict[str, Any]) -> dict[str, Any]:
    """Walk *schema* properties and annotate each with ``x-deploy-plane``.

    Returns *schema* (mutated in-place for convenience).  Properties whose
    name is in ``DEPLOY_PLANE_KEYS`` are marked ``"deploy"``; all others are
    marked ``"component"``.
    """

    def _walk(obj: dict[str, Any], parent_key: str) -> None:
        props = obj.get("properties")
        if not isinstance(props, dict):
            return
        for key, prop_schema in props.items():
            if not isinstance(prop_schema, dict):
                continue
            full_key = f"{parent_key}.{key}" if parent_key else key
            plane = "deploy" if key in DEPLOY_PLANE_KEYS else "component"
            # Annotate the original wrapper so the annotation survives
            # even when prop_schema is a $ref wrapper — _resolve_ref
            # returns a *new* merged dict, so writing to that temporary
            # object alone would silently drop the annotation.
            prop_schema["x-deploy-plane"] = plane
            resolved = prop_schema
            if "$ref" in prop_schema:
                resolved = _resolve_ref(prop_schema, obj.get("$defs", {}))
                # Also annotate the resolved dict so the walker can
                # inspect the correct ownership when recursing into
                # nested objects behind $ref.
                resolved["x-deploy-plane"] = plane
            if resolved.get("type") == "object":
                _walk(resolved, full_key)

    _walk(schema, "")
    return schema


def _resolve_ref(prop_schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Resolve a single ``$ref`` pointer against *defs*."""
    ref = prop_schema.get("$ref", "")
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return prop_schema
    parts = ref[len("#/$defs/") :].split("/")
    resolved: Any = defs
    for part in parts:
        if isinstance(resolved, dict):
            resolved = resolved.get(part)
        else:
            return prop_schema
    if not isinstance(resolved, dict):
        return prop_schema
    # Merge resolved onto a copy of the original so that top-level
    # overrides (description, default, …) are preserved.
    merged = dict(resolved)
    merged.update({k: v for k, v in prop_schema.items() if k != "$ref"})
    return merged


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /services/{name}/config
# ---------------------------------------------------------------------------


async def _current_config_values(
    name: str,
    component_config_store: ComponentConfigStore,
    backend: ExecutionBackend,
    request: Request,
) -> dict[str, Any] | None:
    """Return the component's current config values for display/export.

    central-deploy is not a managed component: it has no config volume, and
    its only "component config" is a Langfuse view derived from its own
    settings plus what auto-discovery found. That view is computed here
    rather than read from a stored copy — see
    :func:`build_central_deploy_langfuse_config`.
    """
    if name == "central-deploy":
        return build_central_deploy_langfuse_config(
            request.app.state.config,
            getattr(request.app.state, "auto_langfuse_projects", {}) or {},
        )
    return await read_component_config(backend, component_config_store.get(name))


@router.get(
    "/services/{name}/config",
    response_model=ConfigResponse,
    summary="Get config.json schema and current values for a service",
    responses={
        404: {"model": ErrorDetail, "description": "Service has no config schema"}
    },
)
async def get_service_config(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ConfigResponse:
    """Return the config.json schema and current masked values for a service.

    Raises 404 if the service has no config schema.

    The returned schema is annotated with ``x-deploy-plane`` on every
    property so the UI can distinguish deploy-plane keys (managed here)
    from component-owned keys (managed in the component's own Settings
    panel).
    """
    await _get_or_create_record(name, store)
    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for component '{name}'",
        )
    current_raw = await _current_config_values(
        name, component_config_store, backend, request
    )
    if not current_raw:
        # A component with no config file yet shows the template defaults.
        # read_component_config returns {} where the old store returned None,
        # so this must test falsiness, not identity.
        current_raw = _merge_config(template, {}, {})
    current_masked = _mask_secrets(template, current_raw)
    comp_cfg = component_config_store.get(name)

    # Always False: the deploy plane no longer keeps a copy of component
    # config values, so there is no second version for the component's own
    # file to drift from. The field is retained so the response shape does
    # not change for existing clients.
    drift = False

    # Annotate the schema with config-ownership metadata per the
    # robotsix-standards config-ownership standard.
    annotated_schema = _annotate_config_ownership(dict(template))

    # Compute the component's own Settings URL when the gateway is
    # configured so the UI can link operators to the correct surface
    # for editing component-owned keys.
    component_settings_url: str | None = None
    try:
        config = await _get_config(request)
        base_domain = config.gateway_base_domain
        if base_domain:
            component_settings_url = f"https://{name}.{base_domain}/ui"
    except Exception:  # noqa: BLE001
        component_settings_url = None

    return ConfigResponse(
        config_schema=annotated_schema,
        current=current_masked,
        drift=drift,
        config_assist_command=comp_cfg.config_assist_command if comp_cfg else None,
        config_assist_seeds=comp_cfg.config_assist_seeds if comp_cfg else [],
        component_settings_url=component_settings_url,
    )


# ---------------------------------------------------------------------------
# GET /services/{name}/config/export  (migration-only)
# ---------------------------------------------------------------------------


@router.get(
    "/services/{name}/config/export",
    response_model=ConfigExportResponse,
    summary="[Migration] Export full config including unmasked secrets",
    responses={
        403: {"description": "Not localhost"},
        404: {"model": ErrorDetail, "description": "Service has no config schema"},
    },
)
async def export_service_config(
    name: str,
    request: Request,
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ConfigExportResponse:
    """Export the full current config INCLUDING unmasked secret values.

    Restricted to localhost + API-key auth.  This is a **migration-only**
    endpoint — components call it exactly once to import their config, then
    the endpoint is decommissioned.

    After migration, config ownership lives in the component per the
    robotsix-standards per-component settings standard.  The deploy plane
    retains only Docker-boundary settings (image, ports, mounts, env/secrets).
    """
    # Restrict to localhost — this endpoint returns plaintext secrets.
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Config export is restricted to localhost.",
        )

    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for component '{name}'",
        )
    current_raw = await _current_config_values(
        name, component_config_store, backend, request
    )
    if not current_raw:
        # A component with no config file yet shows the template defaults.
        # read_component_config returns {} where the old store returned None,
        # so this must test falsiness, not identity.
        current_raw = _merge_config(template, {}, {})

    logger.info(
        "Config export for component '%s' from %s",
        name.replace("\n", "\\n"),
        client_host,
    )

    return ConfigExportResponse(
        component=name,
        values=current_raw,
    )


# ---------------------------------------------------------------------------
# Deprecated config-write endpoints — 410 Gone guards
#
# These routes are deprecated as part of the config-ownership migration
# (robotsix-standards: the deploy plane must not write component-internal
# config.json).  Explicit 410 handlers prevent the requests from falling
# through to the gateway catch-all (/{path:path}) which uses session-auth
# and returns 303 instead of 410.
#
# Use GET /services/{name}/config/export to retrieve config for migration.
# ---------------------------------------------------------------------------


@router.put(
    "/services/{name}/config",
    status_code=status.HTTP_410_GONE,
    summary="[Deprecated] PUT config",
    deprecated=True,
)
async def put_service_config(
    name: str,
    _auth: None = Depends(verify_auth),
) -> JSONResponse:
    """Deprecated: config is now component-owned, not deploy-plane."""
    return JSONResponse(
        status_code=status.HTTP_410_GONE,
        content={
            "detail": (
                "PUT /services/{name}/config is deprecated. "
                "Config is owned by the component, not the deploy plane. "
                "Use GET /services/{name}/config/export to retrieve config "
                "for migration."
            )
        },
        headers={
            "Deprecation": "true",
            "Sunset": "Sun, 01 Feb 2026 00:00:00 GMT",
        },
    )


@router.post(
    "/services/{name}/config/import",
    status_code=status.HTTP_410_GONE,
    summary="[Deprecated] POST config import",
    deprecated=True,
)
async def config_import(
    name: str,
    _auth: None = Depends(verify_auth),
) -> JSONResponse:
    """Deprecated: config is now component-owned, not deploy-plane."""
    return JSONResponse(
        status_code=status.HTTP_410_GONE,
        content={
            "detail": (
                "POST /services/{name}/config/import is deprecated. "
                "Config is owned by the component, not the deploy plane."
            )
        },
        headers={
            "Deprecation": "true",
            "Sunset": "Sun, 01 Feb 2026 00:00:00 GMT",
        },
    )


@router.post(
    "/services/{name}/config/refresh-schema",
    status_code=status.HTTP_410_GONE,
    summary="[Deprecated] POST config refresh-schema",
    deprecated=True,
)
async def config_refresh_schema(
    name: str,
    _auth: None = Depends(verify_auth),
) -> JSONResponse:
    """Deprecated: config is now component-owned, not deploy-plane."""
    return JSONResponse(
        status_code=status.HTTP_410_GONE,
        content={
            "detail": (
                "POST /services/{name}/config/refresh-schema is deprecated. "
                "Config is owned by the component, not the deploy plane."
            )
        },
        headers={
            "Deprecation": "true",
            "Sunset": "Sun, 01 Feb 2026 00:00:00 GMT",
        },
    )


@router.post(
    "/services/{name}/config/assist",
    status_code=status.HTTP_410_GONE,
    summary="[Deprecated] POST config assist",
    deprecated=True,
)
async def config_assist(
    name: str,
    _auth: None = Depends(verify_auth),
) -> JSONResponse:
    """Deprecated: config is now component-owned, not deploy-plane."""
    return JSONResponse(
        status_code=status.HTTP_410_GONE,
        content={
            "detail": (
                "POST /services/{name}/config/assist is deprecated. "
                "Config is owned by the component, not the deploy plane."
            )
        },
        headers={
            "Deprecation": "true",
            "Sunset": "Sun, 01 Feb 2026 00:00:00 GMT",
        },
    )
