"""Volume browsing and audit endpoints for the lifecycle server."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import verify_auth
from ..backend import ExecutionBackend
from ..config import LifecycleConfig
from ..deps import (
    VOLUME_CAT_MAX_BYTES,
    _assert_volume_browsable,
    _get_backend,
    _get_component_config_store,
    _get_config,
    _validate_volume_path,
)
from ...registry.config_store import ComponentConfigStore
from ..schemas import VolumeEntry, VolumeFileResponse, VolumeListResponse
from ...volume_audit.models import VolumeAuditResponse
from ...volume_audit.scheduler import VolumeAuditScheduler

router = APIRouter(tags=["volumes"])


@router.get("/volumes/audit", response_model=VolumeAuditResponse)
async def get_volume_audit(
    request: Request,
    _auth: None = Depends(verify_auth),
    config: LifecycleConfig = Depends(_get_config),
) -> VolumeAuditResponse:
    """Current volume audit state (sizes and growth). Returns enabled=false when subsystem is off."""
    if not config.volume_audit_enabled:
        return VolumeAuditResponse(enabled=False)
    scheduler: VolumeAuditScheduler = request.app.state.volume_audit_scheduler
    return scheduler.get_audit_response()


@router.get(
    "/volumes/{name}/ls",
    response_model=VolumeListResponse,
    summary="List files in a data volume",
)
async def list_volume_files(
    name: str,
    path: str = "",
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    backend: ExecutionBackend = Depends(_get_backend),
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
    return VolumeListResponse(entries=[VolumeEntry(**e) for e in entries_raw])


@router.get(
    "/volumes/{name}/cat",
    response_model=VolumeFileResponse,
    summary="Read a text file from a data volume",
)
async def cat_volume_file(
    name: str,
    path: str = "",
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    backend: ExecutionBackend = Depends(_get_backend),
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
    return VolumeFileResponse(**result)
