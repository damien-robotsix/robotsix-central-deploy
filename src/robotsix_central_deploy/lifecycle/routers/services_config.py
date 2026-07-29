"""Config-related route handlers for the lifecycle server."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

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
from ..schemas import ConfigResponse
from ..store import ServiceStore
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from .._config_utils import (  # noqa: E402
    _canonical_hash,
    _mask_secrets,
    _merge_config,
)

logger = logging.getLogger(__name__)


router = APIRouter(tags=["services"])


# ---------------------------------------------------------------------------
# Config ownership (deploy-plane vs component-owned) — robotsix-standards
# config-ownership standard.
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
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    backend: ExecutionBackend = Depends(_get_backend),
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
    current_raw = await config_yaml_store.get_current(name)
    if current_raw is None:
        current_raw = _merge_config(template, {}, {})
    current_masked = _mask_secrets(template, current_raw)
    comp_cfg = component_config_store.get(name)

    drift = False
    if comp_cfg and comp_cfg.config_volume:
        stored_hash = await config_yaml_store.get_volume_hash(name)
        if stored_hash is not None:
            live_dict = await backend.read_config_from_volume(comp_cfg.config_volume)
            drift = _canonical_hash(live_dict) != stored_hash

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
    except Exception:
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
# Removed config-write endpoints — 404 guards
#
# These routes were removed as part of the config-ownership migration
# (robotsix-standards: the deploy plane must not write component-internal
# config.json).  Explicit 404 handlers prevent the requests from falling
# through to the gateway catch-all (/{path:path}) which uses session-auth
# and returns 303 instead of 404.
# ---------------------------------------------------------------------------


@router.put(
    "/services/{name}/config",
    status_code=status.HTTP_404_NOT_FOUND,
    summary="[Removed] PUT config",
)
async def put_service_config(
    name: str,
    _auth: None = Depends(verify_auth),
) -> dict[str, str]:
    """This endpoint was removed. Config is now component-owned."""
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="PUT /services/{name}/config has been removed. "
        "Config is owned by the component, not the deploy plane.",
    )


@router.post(
    "/services/{name}/config/import",
    status_code=status.HTTP_404_NOT_FOUND,
    summary="[Removed] POST config import",
)
async def config_import(
    name: str,
    _auth: None = Depends(verify_auth),
) -> dict[str, str]:
    """This endpoint was removed. Config is now component-owned."""
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="POST /services/{name}/config/import has been removed. "
        "Config is owned by the component, not the deploy plane.",
    )


@router.post(
    "/services/{name}/config/refresh-schema",
    status_code=status.HTTP_404_NOT_FOUND,
    summary="[Removed] POST config refresh-schema",
)
async def config_refresh_schema(
    name: str,
    _auth: None = Depends(verify_auth),
) -> dict[str, str]:
    """This endpoint was removed. Config is now component-owned."""
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="POST /services/{name}/config/refresh-schema has been removed. "
        "Config is owned by the component, not the deploy plane.",
    )


@router.post(
    "/services/{name}/config/assist",
    status_code=status.HTTP_404_NOT_FOUND,
    summary="[Removed] POST config assist",
)
async def config_assist(
    name: str,
    _auth: None = Depends(verify_auth),
) -> dict[str, str]:
    """This endpoint was removed. Config is now component-owned."""
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="POST /services/{name}/config/assist has been removed. "
        "Config is owned by the component, not the deploy plane.",
    )
