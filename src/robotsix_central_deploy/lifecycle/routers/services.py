"""Service lifecycle endpoints for the lifecycle server."""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from collections.abc import AsyncIterator
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.params import Body
from fastapi.responses import StreamingResponse

from ..auth import verify_auth
from ..backends import ExecutionBackend, collect_protected_image_refs
from ..deps import (
    _canonical_hash,
    _compute_overall_health,
    _derive_account_id,
    _get_backend,
    _get_component_config_store,
    _get_config_yaml_store,
    _get_deploy_history_store,
    _get_env_store,
    _get_or_create_record,
    _get_registry,
    _get_registry_checker,
    _get_sibling_pairs,
    _get_store,
    _mask_secrets,
    _merge_config,
    _prune_unset,
    _relocate_account_seed_values,
    _resolve_placeholders,
    _seed_for_detect,
    _validate_account_ids,
    _validate_config_or_422,
)
from ..models import (
    ActionResponse,
    ContainerHealthSummary,
    DeployHistoryEntry,
    DeployHistoryResponse,
    DeployRequest,
    DeployResponse,
    ErrorDetail,
    RollbackRequest,
    RollbackResponse,
    ServiceHealthResponse,
    ServiceListItem,
    ServiceListResponse,
    ServiceState,
    ServiceStatus,
    can_transition,
)
from ..schemas import (
    ConfigAssistRequest,
    ConfigAssistResponse,
    ConfigDriftConflict,
    ConfigImportResponse,
    ConfigSchemaRefreshResponse,
    ConfigResponse,
    ConfigUpdate,
    EnvResponse,
    EnvSyncResponse,
    EnvUpdate,
)
from ..store import ServiceStore
from ...registry.config_store import ComponentConfigStore
from ...registry.config_yaml_store import ConfigYamlStore
from ...registry.deploy_history_store import DeployHistoryStore
from ...registry.env_store import EnvStore
from ...registry.loader import ComponentRegistry
from ...registry.models import ComponentConfig
from ...registry_check import RegistryChecker


def _deep_merge(
    base: dict[str, object], override: dict[str, object]
) -> dict[str, object]:
    """Recursively merge *override* into *base*; override values win on conflict."""
    result: dict[str, object] = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(
                cast("dict[str, object]", result[key]),
                cast("dict[str, object]", val),
            )
        else:
            result[key] = val
    return result


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


async def _fanout_deploy_siblings(
    name: str,
    store: ServiceStore,
    backend: ExecutionBackend,
    registry: ComponentRegistry,
    env_store: EnvStore,
    deploy_history_store: DeployHistoryStore,
) -> None:
    """Deploy all siblings of *name* (best-effort per sibling)."""
    config_fresh = registry.get(name)
    if not config_fresh or not config_fresh.siblings:
        return
    for sib_config, sib_record in await _get_sibling_pairs(name, config_fresh, store):
        sib_name = f"{name}-{sib_config.service_key}"
        merged_env = await env_store.get_merged_env(sib_name, sib_config.env)
        effective_sib = ComponentConfig(
            id=sib_name,
            image=sib_config.image,
            container_name=sib_config.container_name,
            ports=sib_config.ports,
            mounts=sib_config.mounts,
            env=merged_env,
            health_check=sib_config.health_check,
            claude_mount=sib_config.claude_mount,
            host_docker_sock=sib_config.host_docker_sock,
            named_volumes=[m.host for m in sib_config.mounts],
            command=sib_config.command,
            entrypoint=sib_config.entrypoint,
        )
        try:
            sib_outcome = await backend.deploy(
                sib_record, effective_sib, sib_config.image
            )
            sib_record.state = sib_outcome.state
            sib_record.image = sib_config.image
            sib_record.deployed_image_digest = sib_outcome.deployed_digest
            sib_record.previous_image_digest = sib_outcome.previous_digest
            await store.put(sib_record)
            # Record sibling history (best-effort)
            try:
                await deploy_history_store.append(
                    sib_name,
                    DeployHistoryEntry(
                        digest=sib_outcome.deployed_digest,
                        image_ref=sib_config.image,
                        timestamp=time.time(),
                        source="manual",
                        previous_digest=sib_outcome.previous_digest,
                    ),
                )
            except Exception:
                logger.warning(
                    "deploy sibling '%s': failed to record history",
                    repr(sib_name),
                    exc_info=True,
                )
        except Exception:
            logger.warning("deploy sibling '%s' failed", repr(sib_name))


async def _fanout_rollback_siblings(
    name: str,
    store: ServiceStore,
    backend: ExecutionBackend,
    registry: ComponentRegistry,
    env_store: EnvStore,
) -> None:
    """Roll back all siblings of *name* (best-effort per sibling)."""
    config_fresh = registry.get(name)
    if not config_fresh or not config_fresh.siblings:
        return
    for sib_config, sib_record in await _get_sibling_pairs(name, config_fresh, store):
        if not sib_record.previous_image_digest:
            logger.warning(
                "rollback sibling '%s-%s': no prior digest — skipping",
                name,
                sib_config.service_key,
            )
            continue
        sib_name = f"{name}-{sib_config.service_key}"
        merged_env = await env_store.get_merged_env(sib_name, sib_config.env)
        effective_sib = ComponentConfig(
            id=sib_name,
            image=sib_config.image,
            container_name=sib_config.container_name,
            ports=sib_config.ports,
            mounts=sib_config.mounts,
            env=merged_env,
            health_check=sib_config.health_check,
            claude_mount=sib_config.claude_mount,
            host_docker_sock=sib_config.host_docker_sock,
            named_volumes=[m.host for m in sib_config.mounts],
            command=sib_config.command,
            entrypoint=sib_config.entrypoint,
        )
        try:
            sib_outcome = await backend.rollback(sib_record, effective_sib)
            old_dep_sib = sib_record.deployed_image_digest
            old_prev_sib = sib_record.previous_image_digest
            sib_record.state = sib_outcome.state
            sib_record.deployed_image_digest = old_prev_sib
            sib_record.previous_image_digest = old_dep_sib
            sib_record.image_revision = old_prev_sib
            await store.put(sib_record)
        except Exception:
            logger.warning(
                "rollback sibling '%s-%s' failed",
                name,
                sib_config.service_key,
            )


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


def _resolve_account_mode(
    current_raw: dict[str, Any] | None,
    target_account_index: int | None,
    config_assist_seeds: list[Any],
    template: dict[str, Any],
    existing: dict[str, Any],
    values: dict[str, Any],
    account_name: str | None,
    assist_command: str,
) -> tuple[str, int, dict[str, Any], str]:
    """Resolve account mode, target index, updated partial, and command.

    Returns (mode, target_idx, partial, updated_assist_command).
    Modifies *values* in-place for add_new relocation.
    """
    import re as _re  # noqa: PLC0415

    existing_accounts: list[dict[str, Any]] = (
        [
            a
            for a in current_raw.get("accounts", [])
            if isinstance(a, dict) and a.get("id")
        ]
        if current_raw is not None and isinstance(current_raw.get("accounts"), list)
        else []
    )
    req_idx = target_account_index

    if req_idx is not None and req_idx < len(existing_accounts):
        mode, target_idx = "update", req_idx
    elif existing_accounts:  # req_idx is None OR req_idx >= len
        mode, target_idx = "add_new", len(existing_accounts)
    else:
        mode, target_idx = "first_setup", 0

    # Rewrite accounts.0.* placeholders to the target index in the command.
    if target_idx != 0:
        assist_command = _re.sub(
            r"\{accounts\.0\.",
            f"{{accounts.{target_idx}.",
            assist_command,
        )

    # Sparse submission merge
    partial = _merge_config(template, existing, values, prefer_existing_for_unset=True)

    # For add_new: relocate seed values to the target slot, restore existing
    # accounts, re-merge, and validate.
    if mode == "add_new":
        _relocate_account_seed_values(values, config_assist_seeds, 0, target_idx)
        submitted_accts: list[dict[str, Any]] = values.setdefault("accounts", [])
        for i, ea in enumerate(existing_accounts):
            if i < len(submitted_accts):
                submitted_accts[i] = dict(ea)
            else:
                submitted_accts.append(dict(ea))
        partial = _merge_config(
            template, existing, values, prefer_existing_for_unset=True
        )

        new_id = _derive_account_id(config_assist_seeds, partial, target_idx)
        if account_name:
            _name_slug = _re.sub(r"[^a-z0-9]+", "-", account_name.lower()).strip("-")[
                :40
            ]
            if _name_slug:
                new_id = _name_slug
        acct_list: list[dict[str, Any]] = partial.setdefault("accounts", [])
        while len(acct_list) <= target_idx:
            acct_list.append({})
        acct_list[target_idx]["id"] = new_id
        _validate_account_ids(partial)  # fail fast: id must match ^[A-Za-z0-9._-]+$

    return mode, target_idx, partial, assist_command


def _postprocess_config_assist(
    merged: dict[str, Any], output: str
) -> tuple[dict[str, Any], str]:
    """Drop unconfigured accounts, fix default_account, detect Office365.

    Returns (merged, output) — both may be mutated.
    """
    accts_obj = merged.get("accounts")
    if not isinstance(accts_obj, list):
        return merged, output

    kept: list[Any] = []
    for a in accts_obj:
        if not isinstance(a, dict):
            continue
        auth = a.get("auth")
        imap = a.get("imap")
        user = auth.get("username") if isinstance(auth, dict) else None
        host = imap.get("host") if isinstance(imap, dict) else None
        if user or host:
            kept.append(a)
    merged["accounts"] = kept
    kept_ids = [a.get("id") for a in kept]
    if kept and merged.get("default_account") not in kept_ids:
        merged["default_account"] = kept[0].get("id", "")

    # Office365 accounts: ensure oauth2_provider is flagged and prompt operator
    _O365_SUFFIX = "office365.com"
    _o365_detected = False
    for _acct in kept:
        _imap = _acct.get("imap")
        _smtp = _acct.get("smtp")
        _imap_host = _imap.get("host", "") if isinstance(_imap, dict) else ""
        _smtp_host = _smtp.get("host", "") if isinstance(_smtp, dict) else ""
        if _imap_host.endswith(_O365_SUFFIX) or _smtp_host.endswith(_O365_SUFFIX):
            _acct_auth: Any = _acct.get("auth")
            if not isinstance(_acct_auth, dict):
                _acct_auth = {}
                _acct["auth"] = _acct_auth
            _acct_auth["oauth2_provider"] = "microsoft"
            _acct_auth.pop("password", None)
            _o365_detected = True
    if _o365_detected:
        _o365_msg = (
            "Microsoft/Office365 account detected — authorize it from the "
            "mail board (Authorize button) to connect."
        )
        output = f"{output}\n{_o365_msg}" if output.strip() else _o365_msg

    return merged, output


def _build_assist_command(
    assist_command: str,
    partial: dict[str, Any],
    mode: str,
) -> str:
    """Resolve placeholders in *assist_command* and strip --overwrite for add_new."""
    resolved = shlex.join(
        _resolve_placeholders(arg, partial) for arg in shlex.split(assist_command)
    )
    if mode == "add_new":
        resolved = shlex.join(
            arg for arg in shlex.split(resolved) if arg != "--overwrite"
        )
    return resolved


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
            item.stateful_volumes = config.stateful_volumes
            item.has_config_yaml = config.has_config_yaml
        items.append(item)
    return ServiceListResponse(services=items)


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
            action="start",
            previous_state=previous,
            current_state=ServiceState.RUNNING,
            detail="Service is already running",
        )
    if record.state == ServiceState.STARTING:
        return ActionResponse(
            name=name,
            action="start",
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
        for sib, sib_record in await _get_sibling_pairs(name, config, store):
            try:
                final = await backend.start(sib_record)
                sib_record.state = final
                await store.put(sib_record)
            except Exception:
                logger.warning("start sibling '%s-%s' failed", name, sib.service_key)

    return ActionResponse(
        name=name,
        action="start",
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
            action="stop",
            previous_state=previous,
            current_state=ServiceState.STOPPED,
            detail="Service is already stopped",
        )
    if record.state == ServiceState.STOPPING:
        return ActionResponse(
            name=name,
            action="stop",
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
        for sib, sib_record in await _get_sibling_pairs(name, config, store):
            try:
                final = await backend.stop(sib_record)
                sib_record.state = final
                await store.put(sib_record)
            except Exception:
                logger.warning("stop sibling '%s-%s' failed", name, sib.service_key)

    return ActionResponse(
        name=name,
        action="stop",
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
            action="restart",
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
        for sib, sib_record in await _get_sibling_pairs(name, config, store):
            try:
                final = await backend.restart(sib_record)
                sib_record.state = final
                await store.put(sib_record)
            except Exception:
                logger.warning("restart sibling '%s-%s' failed", name, sib.service_key)

    return ActionResponse(
        name=name,
        action="restart",
        previous_state=previous,
        current_state=record.state,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/deploy
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/deploy",
    response_model=DeployResponse,
    summary="Deploy a new image version for a service",
    responses={
        404: {
            "model": ErrorDetail,
            "description": "Service or component config not found",
        },
        503: {"model": ErrorDetail, "description": "Registry not loaded"},
    },
)
async def deploy_service(
    name: str,
    request: Request,
    body: DeployRequest | None = Body(default=None),  # type: ignore[assignment]
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    deploy_history_store: DeployHistoryStore = Depends(_get_deploy_history_store),
    _auth: None = Depends(verify_auth),
) -> DeployResponse:
    """Pull and deploy a new image version for a service.

    Optionally accepts a specific image reference; defaults to the component's
    configured image. Writes merged config.yaml to the config volume before
    starting when a config schema is present. Raises 404 if the service or
    component config is not found, 503 if the registry checker is not loaded,
    and 500 on backend failure. Sibling services are deployed on a best-effort
    basis. Persists the new and previous image digests to the store.
    """
    if body is None:
        body = DeployRequest()

    record = await _get_or_create_record(name, store)

    config = registry.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config for '{name}' — cannot deploy",
        )

    env_store: EnvStore = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(name, config.env)
    config = config.model_copy(update={"env": merged_env})

    image_ref = body.image or config.image

    # Write merged config.yaml into the config volume before starting the container.
    if config.has_config_yaml and config.config_volume:
        merged_cfg = await config_yaml_store.get_current(
            name
        ) or await config_yaml_store.get_template(name)
        if merged_cfg:
            try:
                await backend.write_config_to_volume(config.config_volume, merged_cfg)
            except Exception as exc:
                logger.warning(
                    "deploy %s: could not write config.yaml to volume %s: %s",
                    name,
                    config.config_volume,
                    exc,
                )
                # non-fatal: container may still start if config was written earlier

    try:
        outcome = await backend.deploy(record, config, image_ref)
    except Exception as exc:
        logger.exception("deploy %s failed", name)
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Deploy failed: {exc}",
        )

    record.state = outcome.state
    record.image = image_ref
    record.image_revision = outcome.deployed_digest
    record.deployed_image_digest = outcome.deployed_digest
    record.previous_image_digest = outcome.previous_digest
    record.last_error = ""
    await store.put(record)

    # Record deploy history (best-effort — never fail a successful deploy)
    try:
        await deploy_history_store.append(
            name,
            DeployHistoryEntry(
                digest=outcome.deployed_digest,
                image_ref=image_ref,
                timestamp=time.time(),
                source="manual",
                previous_digest=outcome.previous_digest,
            ),
        )
    except Exception:
        logger.warning(
            "deploy %s: failed to record history entry", repr(name), exc_info=True
        )

    # Deploy siblings
    await _fanout_deploy_siblings(
        name, store, backend, registry, env_store, deploy_history_store
    )

    # Auto-prune dangling images left behind by the update (opt-in setting);
    # rollback targets recorded in the store are protected. Entirely
    # best-effort: a settings-store or prune failure must never fail the
    # deploy that already succeeded.
    try:
        settings_store = getattr(request.app.state, "settings_store", None)
        settings = await settings_store.get() if settings_store is not None else None
        if settings is not None and settings.image_auto_prune:
            protected = await collect_protected_image_refs(store)
            reclaimed = await backend.prune_images(protected)
            if reclaimed:
                logger.info(
                    "deploy %s: image auto-prune reclaimed %d bytes", name, reclaimed
                )
    except Exception:
        logger.warning("deploy %s: image auto-prune failed", name, exc_info=True)

    return DeployResponse(
        name=name,
        deployed_digest=outcome.deployed_digest,
        previous_digest=outcome.previous_digest,
        current_state=record.state,
        warnings=outcome.warnings,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/rollback
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/rollback",
    response_model=RollbackResponse,
    summary="Rollback a service to a prior image digest",
    responses={
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail, "description": "No prior digest recorded"},
        503: {"model": ErrorDetail},
    },
)
async def rollback_service(
    name: str,
    request: Request,
    body: RollbackRequest | None = Body(default=None),  # type: ignore[assignment]
    store: ServiceStore = Depends(_get_store),
    backend: ExecutionBackend = Depends(_get_backend),
    registry: ComponentRegistry = Depends(_get_registry),
    deploy_history_store: DeployHistoryStore = Depends(_get_deploy_history_store),
    _auth: None = Depends(verify_auth),
) -> RollbackResponse:
    """Roll back a service to a previously recorded image digest.

    When *digest* is absent/None: swaps the deployed and previous digests
    (the original one-step rollback). When *digest* is present: validates it
    appears in the component's deploy history, deploys that digest via the
    backend, and records the rollback.

    Raises 404 if the service or component config is not found, 409 if no
    prior digest or the target digest is not in history, and 500 on backend
    failure. Sibling services are rolled back on a best-effort basis
    (one-step behaviour only).
    """
    record = await _get_or_create_record(name, store)

    config = registry.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No component config for '{name}' — cannot rollback",
        )

    env_store: EnvStore = await _get_env_store(request)
    merged_env = await env_store.get_merged_env(name, config.env)
    config = config.model_copy(update={"env": merged_env})

    if body is None:
        body = RollbackRequest()

    # -- Target-digest rollback (multi-entry history) -----------------------
    if body.digest is not None:
        target_digest = body.digest
        history = await deploy_history_store.list(name)
        recorded_digests = {e.digest for e in history}
        if target_digest not in recorded_digests:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="digest not in deploy history",
            )

        # Derive a digest-pinned repo ref from config.image.
        #   [registry[:port]/]name[:tag|@digest]
        # Strip @digest, then split on the last '/' so ports are never
        # mistaken for tags, then strip the :tag from the name portion.
        bare = config.image.split("@", 1)[0]
        if "/" in bare:
            prefix, name = bare.rsplit("/", 1)
            name = name.rsplit(":", 1)[0] if ":" in name else name
            repo = f"{prefix}/{name}"
        else:
            repo = bare.rsplit(":", 1)[0] if ":" in bare else bare
        image_ref = f"{repo}@{target_digest}"

        try:
            deploy_outcome = await backend.deploy(record, config, image_ref)
        except Exception as exc:
            logger.exception("rollback %s failed", repr(name))
            record.state = ServiceState.FAILED
            record.last_error = str(exc)
            await store.put(record)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Rollback failed: {exc}",
            )

        record.state = deploy_outcome.state
        record.deployed_image_digest = deploy_outcome.deployed_digest
        record.previous_image_digest = deploy_outcome.previous_digest
        record.image_revision = deploy_outcome.deployed_digest
        record.last_error = ""
        await store.put(record)

        try:
            await deploy_history_store.append(
                name,
                DeployHistoryEntry(
                    digest=deploy_outcome.deployed_digest,
                    image_ref=image_ref,
                    timestamp=time.time(),
                    source="rollback",
                    previous_digest=deploy_outcome.previous_digest,
                ),
            )
        except Exception:
            logger.warning(
                "rollback %s: failed to record history entry", repr(name), exc_info=True
            )

        # Fan out siblings (one-step; current behaviour)
        await _fanout_rollback_siblings(name, store, backend, registry, env_store)

        return RollbackResponse(
            name=name,
            rolled_back_to_digest=deploy_outcome.deployed_digest,
            current_state=record.state,
        )

    # -- Original one-step rollback -----------------------------------------
    if not record.previous_image_digest:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No prior image digest recorded for '{name}' — run a deploy first",
        )

    # Snapshot current digests before mutating
    old_deployed = record.deployed_image_digest
    old_previous = record.previous_image_digest

    try:
        outcome = await backend.rollback(record, config)
    except Exception as exc:
        logger.exception("rollback %s failed", name)
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        await store.put(record)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rollback failed: {exc}",
        )

    # Swap digests: rolled-back-to becomes deployed; what-we-had becomes previous
    record.state = outcome.state
    record.deployed_image_digest = old_previous
    record.previous_image_digest = old_deployed
    record.image_revision = old_previous
    record.last_error = ""
    await store.put(record)

    # Rollback siblings using each sibling's previous_image_digest
    await _fanout_rollback_siblings(name, store, backend, registry, env_store)

    return RollbackResponse(
        name=name,
        rolled_back_to_digest=old_previous,
        current_state=record.state,
    )


# ---------------------------------------------------------------------------
# GET /services/{name}/history
# ---------------------------------------------------------------------------


@router.get(
    "/services/{name}/history",
    response_model=DeployHistoryResponse,
    summary="Get deploy history for a service",
    responses={404: {"model": ErrorDetail, "description": "Service not found"}},
)
async def get_service_history(
    name: str,
    store: ServiceStore = Depends(_get_store),
    deploy_history_store: DeployHistoryStore = Depends(_get_deploy_history_store),
    _auth: None = Depends(verify_auth),
) -> DeployHistoryResponse:
    """Return the deploy history for a service, most-recent-first.

    Returns 200 with an empty ``entries`` list when no history is recorded.
    Raises 404 if the service is not found.
    """
    await _get_or_create_record(name, store)
    entries = await deploy_history_store.list(name)
    return DeployHistoryResponse(name=name, entries=entries)


# ---------------------------------------------------------------------------
# GET /services/{name}/env
# ---------------------------------------------------------------------------


@router.get(
    "/services/{name}/env",
    response_model=EnvResponse,
    summary="Get stored environment variables and secret keys for a service",
    responses={404: {"model": ErrorDetail, "description": "Service not found"}},
)
async def get_service_env(
    name: str,
    store: ServiceStore = Depends(_get_store),
    env_store: EnvStore = Depends(_get_env_store),
    _auth: None = Depends(verify_auth),
) -> EnvResponse:
    """Return stored environment variables and masked secret keys for a service.

    Secret values are never exposed — only the key names are returned, each
    masked as ``"***"``. Raises 404 if the service is not found.
    """
    await _get_or_create_record(name, store)
    config = await env_store.get(name)
    secrets_masked = {key: "***" for key in config.secret_tokens}
    return EnvResponse(env=config.env, secrets=secrets_masked)


# ---------------------------------------------------------------------------
# PUT /services/{name}/env
# ---------------------------------------------------------------------------


@router.put(
    "/services/{name}/env",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Upsert environment variables and secrets for a service",
    responses={404: {"model": ErrorDetail, "description": "Service not found"}},
)
async def put_service_env(
    name: str,
    body: EnvUpdate,
    store: ServiceStore = Depends(_get_store),
    env_store: EnvStore = Depends(_get_env_store),
    _auth: None = Depends(verify_auth),
) -> None:
    """Create or update environment variables and secrets for a service.

    Returns 204 No Content on success. Raises 404 if the service is not found.
    """
    await _get_or_create_record(name, store)
    await env_store.upsert(name, body.env, body.secrets)


# ---------------------------------------------------------------------------
# POST /services/{name}/env/sync-keys
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/env/sync-keys",
    response_model=EnvSyncResponse,
    summary="Add env keys newly declared by the repo's compose contract",
    responses={
        400: {"model": ErrorDetail, "description": "Component has no git_url"},
        404: {"model": ErrorDetail, "description": "Component not found"},
        422: {
            "model": ErrorDetail,
            "description": "Repo fetch or compose parse failed",
        },
    },
)
async def sync_env_keys(
    name: str,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    env_store: EnvStore = Depends(_get_env_store),
    _auth: None = Depends(verify_auth),
) -> EnvSyncResponse:
    """Re-read the repo's compose env contract and seed newly declared keys.

    The env key set belongs to the repo's contract, not the operator: keys
    with a default value are added as plain env entries, keys declared empty
    are added as secret slots — mirroring onboard seeding. Existing values
    are never modified, and keys the contract no longer declares are only
    reported, not deleted.
    """
    from robotsix_central_deploy.onboard.fetcher import (  # noqa: PLC0415
        FetchError,
        fetch_repo_files,
    )
    from robotsix_central_deploy.onboard.parser import (  # noqa: PLC0415
        ParseError,
        parse_compose,
    )

    comp_cfg = component_config_store.get(name)
    if comp_cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Component '{name}' not found",
        )
    if not comp_cfg.git_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Component '{name}' has no git_url — cannot fetch its repo",
        )

    loop = asyncio.get_running_loop()
    try:
        repo_files = await loop.run_in_executor(
            None, fetch_repo_files, comp_cfg.git_url
        )
    except FetchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        spec = parse_compose(repo_files.compose_bytes, name, comp_cfg.git_url)
    except ParseError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "compose validation failed", "violations": exc.violations},
        ) from exc

    declared: dict[str, str] = spec.env
    stored = await env_store.get(name)
    existing_keys = set(stored.env) | set(stored.secret_tokens)
    add_env = {k: v for k, v in declared.items() if v and k not in existing_keys}
    add_secrets = {
        k: "" for k, v in declared.items() if not v and k not in existing_keys
    }
    if add_env or add_secrets:
        await env_store.upsert(name, add_env, add_secrets)
    return EnvSyncResponse(
        added_env=sorted(add_env),
        added_secrets=sorted(add_secrets),
        undeclared=sorted(existing_keys - set(declared)),
    )


# ---------------------------------------------------------------------------
# DELETE /services/{name}/env/{key}
# ---------------------------------------------------------------------------


@router.delete(
    "/services/{name}/env/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an environment variable or secret key for a service",
    responses={
        404: {"model": ErrorDetail, "description": "Service or key not found"},
    },
)
async def delete_service_env_key(
    name: str,
    key: str,
    store: ServiceStore = Depends(_get_store),
    env_store: EnvStore = Depends(_get_env_store),
    _auth: None = Depends(verify_auth),
) -> None:
    """Delete a single environment-variable or secret key for a service.

    Returns 204 No Content on success. Raises 404 if the service or key
    is not found.
    """
    await _get_or_create_record(name, store)
    found = await env_store.delete_key(name, key)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Key '{key}' not found in env or secrets for '{name}'",
        )


# ---------------------------------------------------------------------------
# GET /services/{name}/config
# ---------------------------------------------------------------------------


@router.get(
    "/services/{name}/config",
    response_model=ConfigResponse,
    summary="Get config.yaml schema and current values for a service",
    responses={
        404: {"model": ErrorDetail, "description": "Service has no config schema"}
    },
)
async def get_service_config(
    name: str,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ConfigResponse:
    """Return the config.yaml schema and current masked values for a service.

    Raises 404 if the service has no config schema.
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

    return ConfigResponse(
        config_schema=template,
        current=current_masked,
        drift=drift,
        config_assist_command=comp_cfg.config_assist_command if comp_cfg else None,
        config_assist_seeds=comp_cfg.config_assist_seeds if comp_cfg else [],
    )


# ---------------------------------------------------------------------------
# PUT /services/{name}/config
# ---------------------------------------------------------------------------


@router.put(
    "/services/{name}/config",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Merge and save config.yaml values for a service",
    responses={
        404: {"model": ErrorDetail, "description": "Service has no config schema"}
    },
)
async def put_service_config(
    name: str,
    body: ConfigUpdate,
    request: Request,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> None:
    """Merge and save config.yaml values for a service, then write to the config volume.

    Restarts the running container (and any siblings sharing the config
    volume) so new values take effect immediately. Returns 204 No Content.
    Raises 404 if the service has no config schema.
    """
    await _get_or_create_record(name, store)
    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for component '{name}'",
        )
    existing = await config_yaml_store.get_current(name) or template

    # --- drift guard ---
    drifted = False
    live_dict_for_conflict: dict[str, Any] | None = None
    comp_cfg = component_config_store.get(name)
    if comp_cfg and comp_cfg.config_volume:
        stored_hash = await config_yaml_store.get_volume_hash(name)
        if stored_hash is not None:
            live_dict_for_conflict = await backend.read_config_from_volume(
                comp_cfg.config_volume
            )
            drifted = _canonical_hash(live_dict_for_conflict) != stored_hash
    if drifted and not body.force_overwrite:
        assert live_dict_for_conflict is not None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ConfigDriftConflict(
                live_config=_mask_secrets(template, live_dict_for_conflict),
                stored_config=_mask_secrets(template, existing),
            ).model_dump(),
        )
    # --- end drift guard ---

    try:
        merged = _merge_config(template, existing, body.values)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": str(exc)},
        )
    if "accounts" in merged:
        _validate_account_ids(merged)  # Bug 2: reject invalid id slugs
    merged = _prune_unset(merged, existing)  # Bug 3: prune resurrected empty fields
    _validate_config_or_422(template, merged)

    if comp_cfg and comp_cfg.config_volume:
        await backend.write_config_to_volume(comp_cfg.config_volume, merged)
        new_hash = _canonical_hash(merged)
        await config_yaml_store.update_current_and_hash(name, merged, new_hash)
        # Restart primary + siblings sharing the same config volume so the
        # running container(s) pick up the new values immediately.
        registry: ComponentRegistry = request.app.state.registry
        store2: ServiceStore = store  # local alias for clarity
        record = await store2.get(name)
        if record and record.state == ServiceState.RUNNING:
            try:
                await backend.restart(record)
            except Exception as exc:
                logger.warning("config saved for %s but restart failed: %s", name, exc)
        # Fan out to siblings that share the same config volume
        config = registry.get(name) if registry else None
        if config and config.siblings:
            for sib, sib_record in await _get_sibling_pairs(name, config, store2):
                if sib_record.state != ServiceState.RUNNING:
                    continue
                try:
                    await backend.restart(sib_record)
                except Exception as exc:
                    logger.warning(
                        "config saved for %s but sibling '%s' restart failed: %s",
                        name,
                        sib_record.name,
                        exc,
                    )
    else:
        await config_yaml_store.update_current(name, merged)
        logger.warning(
            "put_service_config: no config_volume for %s — config written to store only",
            name,
        )


# ---------------------------------------------------------------------------
# POST /services/{name}/config/import
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/config/import",
    response_model=ConfigImportResponse,
    summary="Import live volume content into the config store, clearing drift",
    responses={
        404: {
            "model": ErrorDetail,
            "description": "Service has no config schema or config volume",
        },
    },
)
async def import_service_config(
    name: str,
    store: ServiceStore = Depends(_get_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ConfigImportResponse:
    """Read the live volume file and store it as the new *current*, clearing drift.

    The imported dict is stored as-is (real secret values preserved, since the
    volume holds real values). The volume hash is updated to match, so subsequent
    drift checks see a clean state.
    """
    await _get_or_create_record(name, store)
    template = await config_yaml_store.get_template(name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config schema for '{name}'",
        )
    comp_cfg = component_config_store.get(name)
    if comp_cfg is None or not comp_cfg.config_volume:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No config volume for '{name}'",
        )
    live_dict = await backend.read_config_from_volume(comp_cfg.config_volume)
    new_hash = _canonical_hash(live_dict)
    await config_yaml_store.update_current_and_hash(name, live_dict, new_hash)
    return ConfigImportResponse(
        current=_mask_secrets(template, live_dict),
        volume_hash=new_hash,
    )


# ---------------------------------------------------------------------------
# POST /services/{name}/config/refresh-schema
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/config/refresh-schema",
    response_model=ConfigSchemaRefreshResponse,
    summary="Refetch config/config.schema.json from the repo and replace the stored template",
    responses={
        400: {"model": ErrorDetail, "description": "Component has no git_url"},
        404: {
            "model": ErrorDetail,
            "description": "Component not found or repo has no config/config.schema.json",
        },
        422: {
            "model": ErrorDetail,
            "description": "Repo fetch failed or schema is invalid JSON",
        },
    },
)
async def refresh_config_schema(
    name: str,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    _auth: None = Depends(verify_auth),
) -> ConfigSchemaRefreshResponse:
    """Replace the stored config template with the repo's committed schema.

    Components onboarded before the schema-driven config keep the legacy raw
    template captured at onboard time; this refetches ``config/config.schema.json``
    from the repo HEAD so the typed schema (field types, enums, descriptions)
    reaches the dashboard without re-onboarding. Stored *values* are untouched.
    """
    import json as _json  # noqa: PLC0415

    from robotsix_central_deploy.onboard.fetcher import (  # noqa: PLC0415
        FetchError,
        fetch_repo_files,
    )

    comp_cfg = component_config_store.get(name)
    if comp_cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Component '{name}' not found",
        )
    if not comp_cfg.git_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Component '{name}' has no git_url — cannot fetch its repo",
        )

    loop = asyncio.get_running_loop()
    try:
        repo_files = await loop.run_in_executor(
            None, fetch_repo_files, comp_cfg.git_url
        )
    except FetchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if repo_files.config_schema_json is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Repo of '{name}' has no config/config.schema.json — the "
                "component must commit a typed schema first"
            ),
        )
    try:
        schema = _json.loads(repo_files.config_schema_json)
    except _json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"config/config.schema.json is not valid JSON: {exc}",
        ) from exc

    await config_yaml_store.save_template(name, schema)
    logger.info("Refreshed config schema for %s from repo", name)
    return ConfigSchemaRefreshResponse(config_schema=schema)


# ---------------------------------------------------------------------------
# POST /services/{name}/config/assist
# ---------------------------------------------------------------------------


@router.post(
    "/services/{name}/config/assist",
    response_model=ConfigAssistResponse,
    summary="Run a repo-declared config-assist command in a one-shot container and return auto-filled config",
    responses={
        400: {
            "model": ErrorDetail,
            "description": "No config-assist command or config volume configured",
        },
        404: {"model": ErrorDetail, "description": "Component not found"},
        504: {"model": ErrorDetail, "description": "Assist command timed out"},
    },
)
async def run_config_assist(
    name: str,
    body: ConfigAssistRequest,
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    config_yaml_store: ConfigYamlStore = Depends(_get_config_yaml_store),
    env_store: EnvStore = Depends(_get_env_store),
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ConfigAssistResponse:
    """Run a repo-declared config-assist command in a one-shot container.

    Fetches fresh config-assist metadata from the component's git repo, runs
    the detect/assist command inside a temporary container with the config
    volume mounted, and merges detected fields into the user-submitted values.
    Raises 400 if no config-assist command or config volume is configured,
    404 if the component is not found, and 504 if the assist command times out.
    """
    comp_cfg = component_config_store.get(name)
    if comp_cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Component '{name}' not found",
        )

    # Re-read config-assist fields from repo HEAD. Write back if changed so
    # GET /config and future assist calls also see the fresh values.
    try:
        loop = asyncio.get_running_loop()
        from ..server import _fetch_fresh_config_assist  # noqa: PLC0415

        fresh_cmd, fresh_seeds = await loop.run_in_executor(
            None, _fetch_fresh_config_assist, comp_cfg.git_url, name
        )
        if (
            fresh_cmd != comp_cfg.config_assist_command
            or fresh_seeds != comp_cfg.config_assist_seeds
        ):
            comp_cfg = comp_cfg.model_copy(
                update={
                    "config_assist_command": fresh_cmd,
                    "config_assist_seeds": fresh_seeds,
                }
            )
            await component_config_store.put(comp_cfg)
            logger.info("Refreshed config-assist fields for %s from repo", name)
    except Exception as exc:
        logger.warning(
            "Could not refresh config-assist fields for %s from repo (%s); "
            "using stored values",
            name,
            exc,
        )

    if comp_cfg.config_assist_command is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No config-assist command configured for '{name}'",
        )
    if comp_cfg.config_volume is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No config volume for '{name}' — add robotsix.deploy.config-target label",
        )

    # Fetch config template + existing current
    template = await config_yaml_store.get_template(name) or {}
    current_raw = await config_yaml_store.get_current(name)
    existing = current_raw or template
    # --- Account-aware mode resolution ---
    mode, target_idx, partial, assist_command = _resolve_account_mode(
        current_raw,
        body.target_account_index,
        comp_cfg.config_assist_seeds,
        template,
        existing,
        body.values,
        body.account_name,
        comp_cfg.config_assist_command,
    )

    # Write sparse seed config into the volume (only submitted keys, no
    # template-default empty strings).  This lets the detect program fill
    # in absent/null fields correctly instead of treating pre-existing
    # empty strings as "already configured".
    if mode == "add_new":
        # Write existing accounts verbatim so detect does not re-validate them.
        # Write only the new account's seed fields (not template defaults).
        item_template = (template.get("accounts") or [{}])[0]
        submitted_accts = body.values.get("accounts", [])
        new_acct_vals = (
            submitted_accts[target_idx] if target_idx < len(submitted_accts) else {}
        )
        new_acct_seed = _seed_for_detect(item_template, {}, new_acct_vals)
        detect_seed: dict[str, Any] = {
            k: v for k, v in existing.items() if k != "accounts"
        }
        detect_seed["accounts"] = list(existing.get("accounts", [])) + [new_acct_seed]
        await backend.write_config_to_volume(comp_cfg.config_volume, detect_seed)
    else:
        await backend.write_config_to_volume(
            comp_cfg.config_volume,
            _seed_for_detect(template, existing, body.values),
        )

    # Resolve the container-side mount path for the config volume
    volume_mount_path = next(
        (m.container for m in comp_cfg.mounts if m.host == comp_cfg.config_volume),
        "/config",  # safe fallback (matches busybox writer convention)
    )

    # Fetch decrypted env+secrets
    merged_env = await env_store.get_merged_env(name, comp_cfg.env)

    # Build resolved command from template with placeholder substitution
    resolved_command = _build_assist_command(assist_command, partial, mode)

    # Run the one-shot container (60 s timeout)
    try:
        output = await backend.run_config_assist(
            image=comp_cfg.image,
            command_str=resolved_command,
            volume_name=comp_cfg.config_volume,
            volume_mount_path=volume_mount_path,
            env_dict=merged_env,
            timeout_seconds=60,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        )
    except RuntimeError as exc:
        output = str(exc)

    # Read back the updated config from the volume
    filled = await backend.read_config_from_volume(comp_cfg.config_volume)

    # Merge detected fields into the submitted config so the detected
    # output never clobbers other fields the user already entered.
    if mode == "add_new":
        # deep_merge replaces the accounts list wholesale. Guard: always take
        # existing accounts from storage (not from what the detect program may
        # have re-written), and only take the new account's slot from filled.
        filled_accts = filled.get("accounts", [])
        # Prefer the detected slot at target_idx; fall back to last entry.
        new_acct_from_filled = (
            filled_accts[target_idx]
            if target_idx < len(filled_accts)
            else (filled_accts[-1] if filled_accts else {})
        )
        new_acct_partial = (
            partial["accounts"][target_idx]
            if target_idx < len(partial.get("accounts", []))
            else {}
        )
        merged_new_acct = _deep_merge(dict(new_acct_partial), new_acct_from_filled)
        # Merge non-accounts keys normally.
        merged = _deep_merge(
            dict({k: v for k, v in partial.items() if k != "accounts"}),
            {k: v for k, v in filled.items() if k != "accounts"},
        )
        assert (
            current_raw is not None
        )  # add_new mode only reachable when current_raw is set
        merged["accounts"] = list(current_raw.get("accounts", [])) + [merged_new_acct]
    else:
        merged = _deep_merge(dict(partial), filled)

    # Post-process: drop unconfigured accounts and detect Office365
    merged, output = _postprocess_config_assist(merged, output)

    # Write the cleaned config back to the volume so the board reads the
    # de-stubbed config with a valid default_account (the detect output left
    # the empty template slot and/or default_account='main').
    await backend.write_config_to_volume(comp_cfg.config_volume, merged)
    # Persist detected config so GET /config shows it and Save is idempotent
    await config_yaml_store.update_current_and_hash(
        name, merged, _canonical_hash(merged)
    )

    return ConfigAssistResponse(config=merged, output=output)


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

    Deletes the service record, env/secrets, config.yaml, and component config.
    Optionally stops and removes the Docker container (``stop_container``) and
    deletes data volumes (``remove_volumes``, irreversible). Raises 404 if the
    component is not found.
    """
    # 1. Verify component exists in config store
    config = config_store.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Component '{name}' not found",
        )

    # 2. Resolve sibling pairs
    pairs = await _get_sibling_pairs(name, config, store)

    # 3. Get primary record (may be None if partially onboarded)
    record = await store.get(name)

    # 4. Best-effort container stop/remove
    if stop_container:
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

    # 4b. Best-effort volume removal (opt-in; IRREVERSIBLE).
    if remove_volumes:
        await _delete_component_volumes(name, config, pairs, backend)

    # 5. Delete sibling records and env
    for sib_cfg, sib_record in pairs:
        await store.delete(sib_record.name)
        await env_store.delete(f"{name}-{sib_cfg.service_key}")

    # 6. Delete primary record
    if record is not None:
        await store.delete(name)

    # 7. Delete primary env/secrets
    await env_store.delete(name)

    # 8. Delete primary config.yaml
    await config_yaml_store.delete(name)

    # 9. Delete from config store
    await config_store.delete(name)

    # 10. Remove from in-memory registry
    registry.unregister(name)
