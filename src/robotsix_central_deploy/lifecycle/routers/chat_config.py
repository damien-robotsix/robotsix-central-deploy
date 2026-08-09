"""Config endpoints for the chat agent — extracted from chat.py.

Only the read endpoint remains. The two write endpoints wrote a whole
config document derived from the deploy plane's *stored template*, which is
a copy of the component's schema rather than the component's own config.
That made them destructive in both directions: keys the template did not
know about were dropped from the submitted payload, and keys the component
had since removed were written back in. Per the robotsix-standards
config-ownership standard a component owns its config, so writes go to the
component's own ``PUT /config`` / ``POST /config/rollback``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from .._config_utils import (
    _mask_secrets,
    _merge_config,
    read_component_config,
)
from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import (
    _get_backend,
    _get_component_config_store,
    _get_config_yaml_store,
)
from ..schemas import ChatAgentConfigRollbackResponse
from ._chat_common import _require_allowed_service

router = APIRouter(tags=["chat"])


_RETIRED_DETAIL = (
    "This endpoint is retired: it wrote a config document rebuilt from the "
    "deploy plane's stored schema template, which silently dropped keys the "
    "template did not know about and reinstated keys the component had "
    "removed. A component owns its own config — send the update to the "
    "component's own 'PUT /config' (or 'POST /config/rollback') instead."
)


# ---------------------------------------------------------------------------
# GET /chat/config/{name}
# ---------------------------------------------------------------------------


@router.get(
    "/chat/config/{name}",
    response_model=ChatAgentConfigRollbackResponse,
    summary="Read the current config for an allowlisted service (secrets redacted)",
    responses={
        403: {"description": "Service not allowlisted"},
        404: {"description": "Service has no config schema"},
    },
)
async def chat_get_config(
    name: str,
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> ChatAgentConfigRollbackResponse:
    """Return the current config for an allowlisted service.

    Secret values are redacted — the chat agent sees ``"***"`` for set
    secrets and ``""`` for unset secrets.
    """
    await _require_allowed_service(name, component_config_store, action="access")
    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for component '{name}'",
        )
    current_raw = await read_component_config(backend, component_config_store.get(name))
    if not current_raw:
        current_raw = _merge_config(template, {}, {})
    masked = _mask_secrets(template, current_raw)
    return ChatAgentConfigRollbackResponse(
        component=name,
        restored=masked,
        detail="Current config (secrets redacted).",
    )


# ---------------------------------------------------------------------------
# Retired write endpoints
# ---------------------------------------------------------------------------
#
# These stay registered so callers get a 410 explaining where writes moved,
# rather than a 404 that reads as "component not found". No request body is
# declared: a payload must reach the 410 rather than be rejected as invalid
# against a schema that no longer applies.


@router.put(
    "/chat/config/{name}",
    summary="Retired — write to the component's own PUT /config instead",
    status_code=status.HTTP_410_GONE,
    responses={410: {"description": "Endpoint retired"}},
)
async def chat_update_config_retired(
    name: str,
    _auth: None = Depends(verify_auth),
) -> None:
    """Always 410. Config writes belong to the component that owns the config."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_RETIRED_DETAIL)


@router.post(
    "/chat/config/{name}/rollback",
    summary="Retired — use the component's own POST /config/rollback instead",
    status_code=status.HTTP_410_GONE,
    responses={410: {"description": "Endpoint retired"}},
)
async def chat_rollback_config_retired(
    name: str,
    _auth: None = Depends(verify_auth),
) -> None:
    """Always 410. Rollback restored a template-shaped snapshot over the volume."""
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_RETIRED_DETAIL)
