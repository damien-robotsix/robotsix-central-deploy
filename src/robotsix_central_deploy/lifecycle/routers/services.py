"""Service lifecycle endpoints for the lifecycle server."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from ..auth import verify_auth
from ..backends import ExecutionBackend
from .._config_utils import _sanitize_log
from ..deps import (
    _compute_overall_health,
    _get_backend,
    _get_component_config_store,
    _get_or_create_record,
    _get_registry,
    _get_registry_checker,
    _get_sibling_pairs,
    _get_store,
)
from ..models import (
    ContainerHealthSummary,
    ErrorDetail,
    ServiceHealthResponse,
    ServiceListItem,
    ServiceListResponse,
    ServiceStatus,
    SiblingUpdateSummary,
    UpdateState,
)
from ..schemas import (
    ComponentSuggestItem,
    ComponentSuggestResponse,
)
from ..store import ServiceStore
from ...registry.config_store import ComponentConfigStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig
from ...registry_check import RegistryChecker
from ._status_refresh import refresh_record_status


logger = logging.getLogger(__name__)

router = APIRouter(tags=["services"])


# ---------------------------------------------------------------------------
# Private helpers extracted from long route handlers
# ---------------------------------------------------------------------------


async def _gather_sibling_health(
    name: str,
    comp_config: ComponentConfig | None,
    store: ServiceStore,
    backend: ExecutionBackend,
) -> tuple[list[ContainerHealthSummary], list[SiblingUpdateSummary]]:
    """Collect health and update-state summaries for all siblings of *name* (best-effort)."""
    sibling_summaries: list[ContainerHealthSummary] = []
    sibling_update_states: list[SiblingUpdateSummary] = []
    if comp_config and comp_config.siblings:
        for _sib_config, sib_record in await _get_sibling_pairs(
            name, comp_config, store
        ):
            try:
                sib_inspect = await backend.status(sib_record)
            except Exception:
                logger.warning(
                    "failed to inspect sibling '%s'; skipping",
                    _sanitize_log(sib_record.name),
                )
                continue
            sib_changed = (
                sib_inspect.state != sib_record.state
                or sib_inspect.health != sib_record.health
            )
            if sib_changed:
                sib_record.state = sib_inspect.state
                sib_record.health = sib_inspect.health
                await store.put(sib_record)
            sibling_summaries.append(
                ContainerHealthSummary(
                    name=sib_record.name,
                    health=sib_inspect.health,
                    state=sib_inspect.state,
                )
            )
            # Compute per-sibling update state from stored digest data
            if (
                not sib_record.deployed_image_digest
                or not sib_record.latest_registry_digest
            ):
                sib_update = UpdateState.UNKNOWN
            elif sib_record.deployed_image_digest == sib_record.latest_registry_digest:
                sib_update = UpdateState.UP_TO_DATE
            else:
                sib_update = UpdateState.UPDATE_AVAILABLE
            sibling_update_states.append(
                SiblingUpdateSummary(
                    name=sib_record.name,
                    update_state=sib_update,
                )
            )
    return sibling_summaries, sibling_update_states


# ---------------------------------------------------------------------------
# GET /services
# ---------------------------------------------------------------------------


@router.get(
    "/services",
    response_model=ServiceListResponse,
    summary="List managed services",
)
async def list_services(
    store: ServiceStore = Depends(_get_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    _auth: None = Depends(verify_auth),
) -> ServiceListResponse:
    """Return all managed services with their current state and optional config metadata."""
    records = await store.list_all()
    items: list[ServiceListItem] = []
    for r in records:
        item = r.to_list_item()
        items.append(item)
    return ServiceListResponse(services=items)


# ---------------------------------------------------------------------------
# GET /components/suggest
# ---------------------------------------------------------------------------


@router.get(
    "/components/suggest",
    response_model=ComponentSuggestResponse,
    summary="List registered components for config-form URL suggestions",
)
async def list_component_suggestions(
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    _auth: None = Depends(verify_auth),
) -> ComponentSuggestResponse:
    """Return every registered component's id, container_name, and first
    container port so the dashboard config form can offer one-click URL
    suggestions for ``*_url`` / ``*_base_url`` fields.
    """
    items: list[ComponentSuggestItem] = []
    for cfg in component_config_store.all():
        container_port: int | None = cfg.ports[0].container if cfg.ports else None
        items.append(
            ComponentSuggestItem(
                id=cfg.id,
                container_name=cfg.container_name,
                container_port=container_port,
            )
        )
    return ComponentSuggestResponse(components=items)


# ---------------------------------------------------------------------------
# GET /services/{name}
# ---------------------------------------------------------------------------


@router.get(
    "/services/{name}",
    response_model=ServiceStatus,
    summary="Get service status",
    responses={404: {"model": ErrorDetail, "description": "Service not found"}},
)
async def get_service_status(
    name: str,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    registry: ComponentRegistry = Depends(_get_registry),
    _auth: None = Depends(verify_auth),
) -> ServiceStatus:
    """Return full status for a service: live state, health, image digests,
    update availability, and sibling health.

    Raises 404 if the service is not found. Persists refreshed state and
    digest data to the store.
    """
    record = await _get_or_create_record(name, store)
    checker: RegistryChecker = _get_registry_checker(request)
    inspect = await refresh_record_status(record, backend, store, checker)

    result = record.to_status()

    # -- Sibling health fan-out ------------------------------------------
    comp_config = registry.get(name)  # ComponentConfig or None
    sibling_summaries, sibling_update_states = await _gather_sibling_health(
        name, comp_config, store, backend
    )
    result.sibling_health = sibling_summaries
    result.sibling_update_states = sibling_update_states
    result.overall_health = _compute_overall_health(inspect.health, sibling_summaries)
    # -------------------------------------------------------------------

    return result


# ---------------------------------------------------------------------------
# GET /services/{name}/health
# ---------------------------------------------------------------------------


@router.get(
    "/services/{name}/health",
    response_model=ServiceHealthResponse,
    summary="Get service health",
    responses={404: {"model": ErrorDetail, "description": "Service not found"}},
)
async def get_service_health(
    name: str,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ServiceHealthResponse:
    """Return the current health status string for a service.

    Raises 404 if the service is not found.
    """
    record = await _get_or_create_record(name, store)
    inspect = await backend.status(record)
    if inspect.health != record.health:
        record.health = inspect.health
        await store.put(record)
    health = inspect.health if inspect.health else "unknown"
    return ServiceHealthResponse(name=name, health=health)


# ---------------------------------------------------------------------------
# GET /services/{name}/logs
# ---------------------------------------------------------------------------


@router.get(
    "/services/{name}/logs",
    summary="Stream container logs (auth-gated)",
    responses={
        404: {"model": ErrorDetail, "description": "Service not found"},
        422: {"description": "Validation error (tail out of range 1-10000)"},
    },
)
async def get_service_logs(
    name: str,
    tail: int = Query(100, ge=1, le=10000),
    since: str | None = Query(None, description="ISO 8601 or Unix timestamp"),
    follow: bool = Query(
        False, description="If true, stream new log lines as they arrive"
    ),
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> StreamingResponse:
    """Stream container log output as a plain-text response.

    Supports optional tail, since, and follow query parameters.
    Raises 404 if the service is not found.
    Raises 422 if tail is out of range (1-10000).
    """
    record = await _get_or_create_record(name, store)

    async def log_gen() -> AsyncIterator[bytes]:
        async for chunk in backend.stream_logs(
            record, tail=tail, since=since, follow=follow
        ):
            yield chunk

    return StreamingResponse(log_gen(), media_type="text/plain; charset=utf-8")
