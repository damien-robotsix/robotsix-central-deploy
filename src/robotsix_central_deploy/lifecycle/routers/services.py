"""Service lifecycle endpoints for the lifecycle server."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import (
    _compute_overall_health,
    _fetch_component_repo_files,
    _get_backend,
    _get_component_config_store,
    _get_config_yaml_store,
    _get_env_store,
    _get_or_create_record,
    _get_registry,
    _get_registry_checker,
    _get_sibling_pairs,
    _get_store,
    _namespace_spec_volumes,
    _build_component_config_from_spec,
)
from ._sibling_utils import _fanout_siblings_best_effort
from ..models import (
    ActionResponse,
    ActionType,
    ContainerHealthSummary,
    ErrorDetail,
    ServiceHealthResponse,
    ServiceListItem,
    ServiceListResponse,
    ServiceState,
    ServiceStatus,
    can_transition,
)
from ..schemas import (
    ComponentSuggestItem,
    ComponentSuggestResponse,
    ContractRefreshResponse,
)
from ..store import ServiceStore
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig
from ...registry_check import RegistryChecker


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
) -> list[ContainerHealthSummary]:
    """Collect health summaries for all siblings of *name* (best-effort)."""
    sibling_summaries: list[ContainerHealthSummary] = []
    if comp_config and comp_config.siblings:
        for _sib_config, sib_record in await _get_sibling_pairs(
            name, comp_config, store
        ):
            try:
                sib_inspect = await backend.status(sib_record)
            except Exception:
                logger.warning(
                    "failed to inspect sibling '%s'; skipping", sib_record.name
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
    return sibling_summaries


async def _delete_component_volumes(
    name: str,
    config: ComponentConfig,
    pairs: list[tuple[Any, Any]],
    backend: ExecutionBackend,
) -> None:
    """Best-effort removal of volumes for *name* and its siblings."""
    volumes: list[str] = list(config.named_volumes)
    for sib_cfg, _sib_record in pairs:
        volumes.extend(m.host for m in sib_cfg.mounts)
    seen: set[str] = set()
    for vol in volumes:
        if vol in seen:
            continue
        seen.add(vol)
        logger.info("delete %s: removing volume %s (remove_volumes=true)", name, vol)
        try:
            await backend.remove_volume(vol)
        except Exception:
            logger.warning(
                "remove_volume failed for %s during delete of %s",
                vol,
                name,
                exc_info=True,
            )


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
        config = component_config_store.get(r.name)
        if config is not None:
            item.has_config_yaml = config.has_config_yaml
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
    # Refresh live state from backend (best-effort).
    inspect = await backend.status(record)
    changed = (
        inspect.state != record.state
        or inspect.image_revision != record.image_revision
        or inspect.health != record.health
    )
    if changed:
        record.state = inspect.state
        record.image_revision = inspect.image_revision
        record.health = inspect.health
        await store.put(record)

    # Persist running_digest from image inspect if available
    if (
        inspect.running_digest
        and inspect.running_digest != record.deployed_image_digest
    ):
        record.deployed_image_digest = inspect.running_digest
        await store.put(record)

    # Registry check — update if we have image+digest and checker is available
    checker: RegistryChecker = _get_registry_checker(request)
    if record.image and record.deployed_image_digest:
        try:
            latest = await checker.get_latest_digest(record.image)
            if latest is not None:
                new_ua = latest != record.deployed_image_digest
                if (
                    record.update_available != new_ua
                    or record.latest_registry_digest != latest
                ):
                    record.update_available = new_ua
                    record.latest_registry_digest = latest
                    await store.put(record)
        except Exception:  # noqa: BLE001
            pass  # degrade gracefully; return last known update_available

    result = record.to_status()
    cfg = component_config_store.get(name)
    if cfg is not None:
        result.has_config_yaml = cfg.has_config_yaml

    # -- Sibling health fan-out ------------------------------------------
    comp_config = registry.get(name)  # ComponentConfig or None
    sibling_summaries = await _gather_sibling_health(name, comp_config, store, backend)
    result.sibling_health = sibling_summaries
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


# ---------------------------------------------------------------------------
# POST /services/{name}/start
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/start",
    response_model=ActionResponse,
    summary="Start a service (idempotent)",
    responses={
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail, "description": "Already in requested state"},
    },
)
async def start_service(
    name: str,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    _auth: None = Depends(verify_auth),
) -> ActionResponse:
    """Start a service. Idempotent — returns success if already running or starting.

    Transitions the service through STARTING to RUNNING (or FAILED on error).
    Raises 404 on missing service, 409 if the current state does not allow a
    start, and 500 on backend failure. Sibling services are started on a
    best-effort basis.
    """
    record = await _get_or_create_record(name, store)
    previous = record.state

    # Idempotency: already running (or starting).
    if record.state == ServiceState.RUNNING:
        return ActionResponse(
            name=name,
            action=ActionType.START,
            previous_state=previous,
            current_state=ServiceState.RUNNING,
            detail="Service is already running",
        )
    if record.state == ServiceState.STARTING:
        return ActionResponse(
            name=name,
            action=ActionType.START,
            previous_state=previous,
            current_state=ServiceState.STARTING,
            detail="Start already in progress",
        )

    # Validate transition.
    if not can_transition(record.state, ServiceState.STARTING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot start from state '{record.state.value}'",
        )

    # Mark starting, then execute.
    record.state = ServiceState.STARTING
    await store.put(record)

    try:
        final_state = await backend.start(record)
    except Exception as exc:
        logger.exception("start %s failed", name)
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Start failed: {exc}",
        )

    record.state = final_state
    record.last_error = (
        "" if final_state == ServiceState.RUNNING else "backend reported failure"
    )
    await store.put(record)

    # Fan out to siblings (best-effort per sibling)
    config = registry.get(name)
    if config and config.siblings:
        await _fanout_siblings_best_effort(name, config, store, backend, "start")

    return ActionResponse(
        name=name,
        action=ActionType.START,
        previous_state=previous,
        current_state=record.state,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/stop
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/stop",
    response_model=ActionResponse,
    summary="Stop a service (idempotent)",
    responses={
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail},
    },
)
async def stop_service(
    name: str,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    _auth: None = Depends(verify_auth),
) -> ActionResponse:
    """Stop a service. Idempotent — returns success if already stopped or stopping.

    Transitions the service through STOPPING to STOPPED (or FAILED on error).
    Raises 404 on missing service, 409 if the current state does not allow a
    stop, and 500 on backend failure. Sibling services are stopped on a
    best-effort basis.
    """
    record = await _get_or_create_record(name, store)
    previous = record.state

    # Idempotency.
    if record.state == ServiceState.STOPPED:
        return ActionResponse(
            name=name,
            action=ActionType.STOP,
            previous_state=previous,
            current_state=ServiceState.STOPPED,
            detail="Service is already stopped",
        )
    if record.state == ServiceState.STOPPING:
        return ActionResponse(
            name=name,
            action=ActionType.STOP,
            previous_state=previous,
            current_state=ServiceState.STOPPING,
            detail="Stop already in progress",
        )

    if not can_transition(record.state, ServiceState.STOPPING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot stop from state '{record.state.value}'",
        )

    record.state = ServiceState.STOPPING
    await store.put(record)

    try:
        final_state = await backend.stop(record)
    except Exception as exc:
        logger.exception("stop %s failed", name)
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Stop failed: {exc}",
        )

    record.state = final_state
    record.last_error = (
        "" if final_state == ServiceState.STOPPED else "backend reported failure"
    )
    await store.put(record)

    # Stop siblings (best-effort per sibling)
    config = registry.get(name)
    if config and config.siblings:
        await _fanout_siblings_best_effort(name, config, store, backend, "stop")

    return ActionResponse(
        name=name,
        action=ActionType.STOP,
        previous_state=previous,
        current_state=record.state,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/restart
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/restart",
    response_model=ActionResponse,
    summary="Restart a service (idempotent)",
    responses={
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail},
    },
)
async def restart_service(
    name: str,
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    _auth: None = Depends(verify_auth),
) -> ActionResponse:
    """Restart a service. Idempotent — returns success if a restart is already in progress.

    Transitions the service through RESTARTING to RUNNING (or FAILED on error).
    Raises 404 on missing service, 409 if the current state does not allow a
    restart, and 500 on backend failure. Sibling services are restarted on a
    best-effort basis.
    """
    record = await _get_or_create_record(name, store)
    previous = record.state

    # Idempotency — if already restarting, let it continue.
    if record.state == ServiceState.RESTARTING:
        return ActionResponse(
            name=name,
            action=ActionType.RESTART,
            previous_state=previous,
            current_state=ServiceState.RESTARTING,
            detail="Restart already in progress",
        )

    if not can_transition(record.state, ServiceState.RESTARTING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot restart from state '{record.state.value}'",
        )

    record.state = ServiceState.RESTARTING
    await store.put(record)

    try:
        final_state = await backend.restart(record)
    except Exception as exc:
        logger.exception("restart %s failed", name)
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Restart failed: {exc}",
        )

    record.state = final_state
    record.last_error = (
        "" if final_state == ServiceState.RUNNING else "backend reported failure"
    )
    await store.put(record)

    # Restart siblings (best-effort per sibling)
    config = registry.get(name)
    if config and config.siblings:
        await _fanout_siblings_best_effort(name, config, store, backend, "restart")

    return ActionResponse(
        name=name,
        action=ActionType.RESTART,
        previous_state=previous,
        current_state=record.state,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/refresh-contract
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/refresh-contract",
    response_model=ContractRefreshResponse,
    summary="Refetch deploy/docker-compose.yml from the repo and update stored contract",
    responses={
        400: {
            "model": ErrorDetail,
            "description": "Component has no git_url",
        },
        404: {
            "model": ErrorDetail,
            "description": "Component not found or repo has no deploy/docker-compose.yml",
        },
        422: {
            "model": ErrorDetail,
            "description": "Repo fetch failed or compose parse failed",
        },
    },
)
async def refresh_contract(
    name: str,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    registry: ComponentRegistry = Depends(_get_registry),
    _auth: None = Depends(verify_auth),
) -> ContractRefreshResponse:
    """Re-parse the component's deploy/docker-compose.yml and update stored settings.

    Contract-derived fields (image, ports, mounts, command, entrypoint,
    health check, siblings, labels, etc.) are refreshed from the repo HEAD.
    Operator-set fields (repo_id, caretaker_auto_update, mem_limit) and
    environment overrides in the EnvStore are left untouched.  The endpoint
    returns which fields changed so the operator can decide whether a
    redeploy is needed.
    """
    from robotsix_central_deploy.onboard.parser import (  # noqa: PLC0415
        ParseError,
        parse_compose,
    )

    comp_cfg, repo_files = await _fetch_component_repo_files(
        name, component_config_store
    )

    loop = asyncio.get_running_loop()

    if repo_files.compose_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Repo of '{name}' has no deploy/docker-compose.yml — "
                "the component must commit a deploy contract first"
            ),
        )

    try:
        spec = await loop.run_in_executor(
            None, parse_compose, repo_files.compose_bytes, name, comp_cfg.git_url
        )
    except ParseError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"deploy/docker-compose.yml parse failed: {'; '.join(exc.violations)}",
        ) from exc

    # Namespace volume names (same as onboard confirm)
    spec = _namespace_spec_volumes(spec, name)

    # Build the new ComponentConfig from the DerivedSpec (same logic as onboard confirm).
    # Preserve operator-set / system-set fields from the existing config.
    new_config = _build_component_config_from_spec(
        spec,
        git_url=comp_cfg.git_url,
        repo_id=comp_cfg.repo_id,
        caretaker_auto_update=comp_cfg.caretaker_auto_update,
    )

    # Diff: collect which contract-derived fields changed.
    _CONTRACT_FIELDS = (
        "image",
        "container_name",
        "ports",
        "mounts",
        "env",
        "health_check",
        "command",
        "entrypoint",
        "tmpfs",
        "mem_limit",
        "claude_mount",
        "host_docker_sock",
        "named_volumes",
        "siblings",
        "config_volume",
        "config_assist_command",
        "config_assist_seeds",
        "llmio_tier_level",
        "allow_chat_access",
        "user",
    )
    changed: list[str] = []
    previous: dict[str, Any] = {}
    current: dict[str, Any] = {}
    for field in _CONTRACT_FIELDS:
        old_val = getattr(comp_cfg, field)
        new_val = getattr(new_config, field)
        if old_val != new_val:
            changed.append(field)
            # Serialize model fields for the response
            if hasattr(old_val, "model_dump"):
                previous[field] = old_val.model_dump()
            elif (
                isinstance(old_val, list)
                and old_val
                and hasattr(old_val[0], "model_dump")
            ):
                previous[field] = [v.model_dump() for v in old_val]
            else:
                previous[field] = old_val
            if hasattr(new_val, "model_dump"):
                current[field] = new_val.model_dump()
            elif (
                isinstance(new_val, list)
                and new_val
                and hasattr(new_val[0], "model_dump")
            ):
                current[field] = [v.model_dump() for v in new_val]
            else:
                current[field] = new_val

    # Persist the updated config
    await component_config_store.put(new_config)
    registry.register(new_config)

    # If the config schema changed (new or removed), refresh the stored template.
    if spec.config_schema is not None:
        await config_yaml_store.save_template(name, spec.config_schema)
    # Note: we do NOT remove the template if the schema is now absent —
    # the operator may still want the old schema in the dashboard.

    logger.info(
        "Refreshed contract for %s from repo: %d field(s) changed (%s)",
        name.replace("\n", "\\n"),
        len(changed),
        ", ".join(changed) if changed else "none",
    )
    return ContractRefreshResponse(
        name=name,
        changed_fields=changed,
        previous=previous,
        current=current,
    )


# ---------------------------------------------------------------------------
# DELETE /services/{name}
# ---------------------------------------------------------------------------


@router.delete(
    "/services/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an onboarded component and optionally its container",
)
async def delete_service(
    name: str,
    stop_container: bool = Query(
        default=True,
        description="Stop and remove the managed container (true) or leave it running (false)",
    ),
    remove_volumes: bool = Query(
        default=False,
        description="Also delete the component's data volumes (IRREVERSIBLE — destroys stored data)",
    ),
    store: ServiceStore = Depends(_get_store),
    config_store: ComponentConfigStore = Depends(_get_component_config_store),
    env_store: EnvStore = Depends(_get_env_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    _auth: None = Depends(verify_auth),
) -> None:
    """Remove an onboarded component and optionally its container and volumes.

    Deletes the service record, env/secrets, config.json, and component config.
    Optionally stops and removes the Docker container (``stop_container``) and
    deletes data volumes (``remove_volumes``, irreversible).  Idempotent —
    succeeds even when some persisted state is already absent (e.g. the
    component config was cleared).  Raises 404 only when *neither* a service
    record nor a component config exists for *name*.
    """
    # 1. Look up primary record and config independently (either may be absent)
    record = await store.get(name)
    config = config_store.get(name)

    # 2. If neither exists, there is nothing to tear down
    if record is None and config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Component '{name}' not found",
        )

    # 3. Resolve sibling pairs (requires config; fall back to prefix scan)
    if config is not None:
        pairs = await _get_sibling_pairs(name, config, store)

    # 4. Best-effort container stop/remove (only when config is present)
    if stop_container and config is not None:
        if record is not None:
            try:
                await backend.stop(record)
            except Exception:
                logger.warning("stop failed for %s during delete", name, exc_info=True)
            try:
                await backend.remove_container(record)
            except Exception:
                logger.warning(
                    "remove_container failed for %s during delete",
                    name,
                    exc_info=True,
                )
        for _sib_cfg, sib_record in pairs:
            try:
                await backend.stop(sib_record)
            except Exception:
                logger.warning(
                    "stop failed for %s during delete",
                    sib_record.name,
                    exc_info=True,
                )
            try:
                await backend.remove_container(sib_record)
            except Exception:
                logger.warning(
                    "remove_container failed for %s during delete",
                    sib_record.name,
                    exc_info=True,
                )

    # 4b. Best-effort volume removal (opt-in; IRREVERSIBLE; requires config).
    if remove_volumes and config is not None:
        await _delete_component_volumes(name, config, pairs, backend)

    # 5. Delete sibling records and env
    if config is not None:
        for sib_cfg, sib_record in pairs:
            await store.delete(sib_record.name)
            await env_store.delete(f"{name}-{sib_cfg.service_key}")
    else:
        # Discover siblings by prefix scan on the service store when
        # the component config is absent (e.g. already cleared).
        all_records = await store.list_all()
        for r in all_records:
            if r.name.startswith(f"{name}-"):
                await store.delete(r.name)
                await env_store.delete(r.name)

    # 6. Delete primary record
    if record is not None:
        await store.delete(name)

    # 7. Delete primary env/secrets
    await env_store.delete(name)

    # 8. Delete primary config.json
    await config_yaml_store.delete(name)

    # 9. Delete from config store (no-op if absent)
    await config_store.delete(name)

    # 10. Remove from in-memory registry (no-op if absent)
    registry.unregister(name)
