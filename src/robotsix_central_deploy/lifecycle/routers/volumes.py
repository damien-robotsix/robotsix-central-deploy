"""Volume browsing, audit, and relocation endpoints for the lifecycle server."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ...caretaker.volume_audit.models import VolumeAuditResponse
from ...caretaker.volume_audit.scheduler import VolumeAuditScheduler
from ...registry.config_store import ComponentConfigStore
from ...registry.loader import ComponentRegistry
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
    _get_registry,
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
    registry: ComponentRegistry = Depends(_get_registry),  # noqa: B008
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

    # 3. Find the owning component.  The volume may belong to more than
    #    one component — pick the first and update its target_disk.
    component_id: str | None = None
    for cfg in component_config_store.all():
        if name in cfg.named_volumes:
            component_id = cfg.id
            break
    if component_id is None:
        raise HTTPException(
            status_code=404,
            detail=f"Volume '{name}' not owned by any registered component",
        )

    config = component_config_store.get(component_id)
    if config is None:
        raise HTTPException(
            status_code=404,
            detail=f"Component '{component_id}' not found",
        )

    # Record the previous disk for the response.
    source_disk = config.target_disk or "default"

    # 4. Stop the component (and siblings) if running.
    svc_record = await store.get(component_id)
    was_running = svc_record is not None and svc_record.state in (
        ServiceState.RUNNING,
        ServiceState.STARTING,
    )
    if was_running and svc_record is not None:
        try:
            await backend.stop(svc_record)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "relocate: failed to stop %s before migration: %s",
                _sanitize_log(component_id),
                exc,
            )
        if config.siblings:
            await _fanout_siblings_best_effort(
                component_id, config, store, backend, "stop"
            )

    # 5. Migrate the volume data.
    outcome = await backend.relocate_volume(name, target_disk_path)
    if outcome.get("status") != "ok":
        # Re-start the component on failure.
        if was_running and svc_record is not None:
            try:
                await backend.start(svc_record)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "relocate: failed to restart %s after failed migration: %s",
                    _sanitize_log(component_id),
                    exc,
                )
        raise HTTPException(
            status_code=500,
            detail=f"Volume relocation failed: {outcome.get('detail', 'unknown error')}",
        )

    # 6. Persist the new target_disk on the owning component.
    config.target_disk = target_disk_path
    await component_config_store.put(config)

    # 7. Restart the component.
    if was_running and svc_record is not None:
        try:
            await backend.start(svc_record)
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

    return RelocateVolumeResponse(
        status="ok",
        detail=outcome.get("detail", ""),
        volume_name=name,
        component_id=component_id,
        source_disk=source_disk,
        target_disk=target_disk_path,
    )
