"""Volume browsing, audit, and relocation endpoints for the lifecycle server."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ...caretaker.volume_audit.models import VolumeAuditResponse
from ...caretaker.volume_audit.scheduler import VolumeAuditScheduler
from ...registry.config_store import ComponentConfigStore
from .._config_utils import _sanitize_log
from .._disk_utils import resolve_target_disk
from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..config import LifecycleConfig
from ..deps import (
    VOLUME_CAT_MAX_BYTES,
    _assert_volume_browsable,
    _compute_orphan_volumes,
    _get_backend,
    _get_component_config_store,
    _get_config,
    _get_store,
    _validate_volume_path,
)
from ..models import ServiceState
from ..schemas import (
    OrphanVolume,
    OrphanVolumesResponse,
    PruneVolumesRequest,
    PruneVolumesResponse,
    RelocateVolumeRequest,
    RelocateVolumeResponse,
    VolumeEntry,
    VolumeFileResponse,
    VolumeListResponse,
)
from ..store import ServiceStore
from ._sibling_utils import _fanout_siblings_best_effort

logger = logging.getLogger(__name__)

router = APIRouter(tags=["volumes"])


@router.get("/volumes/audit", response_model=VolumeAuditResponse)
async def get_volume_audit(
    request: Request,
    _auth: None = Depends(verify_auth),
    config: LifecycleConfig = Depends(_get_config),  # noqa: B008
) -> VolumeAuditResponse:
    """Current volume audit state (sizes and growth). Returns enabled=false when subsystem is off."""
    if not config.volume_audit_enabled:
        return VolumeAuditResponse(enabled=False)
    scheduler: VolumeAuditScheduler = request.app.state.volume_audit_scheduler
    return scheduler.get_audit_response()


@router.get(
    "/volumes/orphans",
    response_model=OrphanVolumesResponse,
    summary="List Docker volumes owned by no component and not in use",
)
async def list_orphan_volumes(
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> OrphanVolumesResponse:
    """Return the prune-safe orphan volumes and their total size.

    Orphans are Docker volumes declared by no registered component and not
    attached to any container — leftovers from removed/re-onboarded components.
    """
    orphans = await _compute_orphan_volumes(backend, component_config_store)
    return OrphanVolumesResponse(
        volumes=[OrphanVolume(name=v.name, size_bytes=v.size_bytes) for v in orphans],
        total_bytes=sum(v.size_bytes for v in orphans),
    )


@router.post(
    "/volumes/prune",
    response_model=PruneVolumesResponse,
    summary="Remove orphan Docker volumes (owned by no component, not in use)",
)
async def prune_orphan_volumes(
    body: PruneVolumesRequest | None = Body(default=None),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> PruneVolumesResponse:
    """Delete orphan volumes (IRREVERSIBLE).

    The eligible set is recomputed server-side on every call — a component's
    own volumes and in-use volumes are never touched even if their names are
    passed in ``names``.  With no ``names`` (or ``names=null``) every orphan is
    pruned; with an explicit list, only names that are still genuine orphans
    are removed and the rest are reported under ``skipped``.
    """
    orphans = {
        v.name: v
        for v in await _compute_orphan_volumes(backend, component_config_store)
    }
    if body is not None and body.names is not None:
        to_remove = [n for n in body.names if n in orphans]
        skipped = [n for n in body.names if n not in orphans]
    else:
        to_remove = list(orphans)
        skipped = []

    for name in to_remove:
        # Best-effort and non-raising (see ExecutionBackend.remove_volume).
        await backend.remove_volume(name)

    # Re-query to report only volumes that actually disappeared — remove_volume
    # swallows errors, so success cannot be assumed from the call returning.
    after = await _compute_orphan_volumes(backend, component_config_store)
    still_present = {v.name for v in after}
    removed = [n for n in to_remove if n not in still_present]
    failed = [n for n in to_remove if n in still_present]
    reclaimed = sum(orphans[n].size_bytes for n in removed)
    return PruneVolumesResponse(
        removed=removed,
        skipped=skipped,
        failed=failed,
        space_reclaimed_bytes=reclaimed,
    )


@router.get(
    "/volumes/{name}/ls",
    response_model=VolumeListResponse,
    summary="List files in a data volume",
)
async def list_volume_files(
    name: str,
    path: str = "",
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> VolumeListResponse:
    """Return immediate children of a directory within a named volume.

    Only volumes declared in at least one component's ``named_volumes`` are
    browsable.  ``path`` defaults to the volume root.
    """
    _assert_volume_browsable(name, component_config_store)
    rel = _validate_volume_path(path)
    try:
        entries_raw = await backend.list_volume_dir(name, rel)
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Volume browsing not supported by this backend",
        )
    except NotADirectoryError:
        raise HTTPException(
            status_code=404,
            detail=f"'{rel or '/'}' is not a directory in volume '{name}'",
        )
    return VolumeListResponse(entries=[VolumeEntry(**e) for e in entries_raw])


@router.get(
    "/volumes/{name}/cat",
    response_model=VolumeFileResponse,
    summary="Read a text file from a data volume",
)
async def cat_volume_file(
    name: str,
    path: str = "",
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> VolumeFileResponse:
    """Return the text content of a file within a named volume.

    Files larger than ``VOLUME_CAT_MAX_BYTES`` are truncated (``truncated=True``).
    Binary files (NUL byte or non-UTF-8) return ``binary=True`` and ``content=null``.
    """
    _assert_volume_browsable(name, component_config_store)
    rel = _validate_volume_path(path)
    try:
        result = await backend.read_volume_file(name, rel, VOLUME_CAT_MAX_BYTES)
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Volume browsing not supported by this backend",
        )
    except IsADirectoryError:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{rel or '/'}' is a directory — list it with "
                f"GET /volumes/{name}/ls?path={rel}"
            ),
        )
    return VolumeFileResponse(**result)


@router.post(
    "/volumes/{name}/relocate",
    response_model=RelocateVolumeResponse,
    summary="Relocate a named data volume to a different physical disk",
)
async def relocate_volume(
    name: str,
    body: RelocateVolumeRequest = Body(...),  # noqa: B008
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),  # noqa: B008
    store: ServiceStore = Depends(_get_store),  # noqa: B008
    backend: ExecutionBackend = Depends(_get_backend),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> RelocateVolumeResponse:
    """Relocate *name*'s data to another physical disk.

    The volume's contents are copied to the target disk, verified, and the
    owning component's ``target_disk`` is updated so the new location
    persists across redeploys.  The component is stopped during the
    migration and restarted afterwards.  On failure the original volume
    and mount are left intact.
    """
    # 1. Resolve the target disk identifier to a canonical mount point.
    try:
        target_disk_path = resolve_target_disk(body.target_disk)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 2. Ensure the volume is browsable (owned by a registered component).
    _assert_volume_browsable(name, component_config_store)

    # 3. Find all owning components.  The volume may belong to more than
    #    one component — update the target_disk on every one of them so
    #    no owner is left with a stale disk reference.
    owning_ids: list[str] = [
        cfg.id for cfg in component_config_store.all() if name in cfg.named_volumes
    ]
    if not owning_ids:
        raise HTTPException(
            status_code=404,
            detail=f"Volume '{name}' not owned by any registered component",
        )

    # Use the first owner for lifecycle operations (stop/start).
    component_id = owning_ids[0]
    config = component_config_store.get(component_id)
    if config is None:
        raise HTTPException(
            status_code=404,
            detail=f"Component '{component_id}' not found",
        )

    # Record the previous disk for the response.
    source_disk = config.target_disk or "default"

    # Guard: skip the migration if the volume is already on the requested
    # disk — avoids a wasteful (and potentially dangerous) copy-into-self.
    # Compare realpaths so a symlinked mount-point spelling still short-circuits.
    if config.target_disk and os.path.realpath(target_disk_path) == os.path.realpath(
        config.target_disk
    ):
        return RelocateVolumeResponse(
            status="ok",
            detail="Volume is already on the requested disk",
            volume_name=name,
            component_id=component_id,
            source_disk=source_disk,
            target_disk=target_disk_path,
        )

    # 4. Stop every owning component (and their siblings) if running.
    #    All owners that share this volume must be stopped so no one writes
    #    during the copy or keeps an orphaned mount after the volume swap.
    #    We do NOT gate this on the first owner's state — a co-owner or sibling
    #    may be RUNNING even when the primary is STOPPED (review #436-1).
    svc_record = await store.get(component_id)
    was_running = svc_record is not None and svc_record.state in (
        ServiceState.RUNNING,
        ServiceState.STARTING,
    )
    # Stop the first owner (the one we selected for lifecycle ops).
    if svc_record is not None and svc_record.state in (
        ServiceState.RUNNING,
        ServiceState.STARTING,
    ):
        try:
            stop_result = await backend.stop(svc_record)
            svc_record.state = stop_result
            await store.put(svc_record)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "relocate: failed to stop %s before migration: %s",
                _sanitize_log(component_id),
                exc,
            )
    if config.siblings:
        await _fanout_siblings_best_effort(component_id, config, store, backend, "stop")
    # Also stop every OTHER owning component so they cannot write to
    # the volume during migration (review issue #434-3).
    for oid in owning_ids:
        if oid == component_id:
            continue
        ocfg = component_config_store.get(oid)
        if ocfg is None:
            continue
        o_svc = await store.get(oid)
        if o_svc is not None and o_svc.state in (
            ServiceState.RUNNING,
            ServiceState.STARTING,
        ):
            try:
                stop_result = await backend.stop(o_svc)
                o_svc.state = stop_result
                await store.put(o_svc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "relocate: failed to stop co-owner %s before migration: %s",
                    _sanitize_log(oid),
                    exc,
                )
            if ocfg.siblings:
                await _fanout_siblings_best_effort(oid, ocfg, store, backend, "stop")

    # 5. Persist the new target_disk on every owning component BEFORE
    #    migration.  If the config write fails we never touch the volume;
    #    if the migration fails we roll the config back.  This avoids the
    #    data-loss scenario where the volume is physically relocated but
    #    the config write raises — the component would restart pointing
    #    at the old (now empty) location.
    previous_target_disks: dict[str, str] = {}
    for oid in owning_ids:
        ocfg = component_config_store.get(oid)
        if ocfg is not None:
            previous_target_disks[oid] = ocfg.target_disk
            ocfg.target_disk = target_disk_path
            await component_config_store.put(ocfg)

    # 6. Migrate the volume data.  Pass the owning component's ``user``
    #    override so the relocated volume root is chowned to the uid/gid the
    #    component actually runs as (defaults to the server's uid/gid inside
    #    the backend when the component has no override).
    #    The call MUST be wrapped in try/except: an unexpected backend
    #    exception (transport error, NotImplementedError, etc.) must still
    #    trigger config rollback and component restart (review #436-2).
    try:
        outcome = await backend.relocate_volume(name, target_disk_path, config.user)
    except NotImplementedError:
        # The DockerBackend (docker-cli) does not support relocation.
        outcome = {"status": "failed", "detail": "Not supported by this backend"}
    except Exception as exc:  # noqa: BLE001
        outcome = {"status": "failed", "detail": f"Backend error: {exc}"}

    if outcome.get("status") != "ok":
        # Rollback the config change on every owner.
        for oid, prev in previous_target_disks.items():
            ocfg = component_config_store.get(oid)
            if ocfg is not None:
                ocfg.target_disk = prev
                try:
                    await component_config_store.put(ocfg)
                except Exception as rollback_exc:  # noqa: BLE001
                    logger.warning(
                        "relocate: failed to rollback target_disk for %s: %s",
                        _sanitize_log(oid),
                        rollback_exc,
                    )
        # Re-start ALL owning components (and their siblings) on failure —
        # all were stopped before the migration attempt (review issue #434-3).
        # Check each component's state individually rather than relying on
        # the primary owner's was_running flag (review #436-1).
        # Primary owner.
        if svc_record is not None:
            try:
                start_result = await backend.start(svc_record)
                svc_record.state = start_result
                await store.put(svc_record)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "relocate: failed to restart %s after failed migration: %s",
                    _sanitize_log(component_id),
                    exc,
                )
        if config.siblings:
            await _fanout_siblings_best_effort(
                component_id, config, store, backend, "start"
            )
        # Co-owners.
        for oid in owning_ids:
            if oid == component_id:
                continue
            ocfg = component_config_store.get(oid)
            if ocfg is None:
                continue
            o_svc = await store.get(oid)
            if o_svc is not None:
                try:
                    start_result = await backend.start(o_svc)
                    o_svc.state = start_result
                    await store.put(o_svc)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "relocate: failed to restart co-owner %s after failed migration: %s",
                        _sanitize_log(oid),
                        exc,
                    )
                if ocfg.siblings:
                    await _fanout_siblings_best_effort(
                        oid, ocfg, store, backend, "start"
                    )

        status_code = (
            501 if outcome.get("detail") == "Not supported by this backend" else 500
        )
        raise HTTPException(
            status_code=status_code,
            detail=f"Volume relocation failed: {outcome.get('detail', 'unknown error')}",
        )

    # 7. Restart all owning components (and their siblings).  We use the
    #    *pre-migration* running state so components that were STOPPED before
    #    the migration stay stopped after it.
    if was_running and svc_record is not None:
        try:
            start_result = await backend.start(svc_record)
            svc_record.state = start_result
            await store.put(svc_record)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "relocate: failed to restart %s after migration: %s",
                _sanitize_log(component_id),
                exc,
            )
        if config.siblings:
            await _fanout_siblings_best_effort(
                component_id, config, store, backend, "start"
            )
        for oid in owning_ids:
            if oid == component_id:
                continue
            ocfg = component_config_store.get(oid)
            if ocfg is None:
                continue
            o_svc = await store.get(oid)
            if o_svc is not None:
                try:
                    start_result = await backend.start(o_svc)
                    o_svc.state = start_result
                    await store.put(o_svc)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "relocate: failed to restart co-owner %s after migration: %s",
                        _sanitize_log(oid),
                        exc,
                    )
                if ocfg.siblings:
                    await _fanout_siblings_best_effort(
                        oid, ocfg, store, backend, "start"
                    )

    return RelocateVolumeResponse(
        status="ok",
        detail=outcome.get("detail", ""),
        volume_name=name,
        component_id=component_id,
        source_disk=source_disk,
        target_disk=target_disk_path,
    )
