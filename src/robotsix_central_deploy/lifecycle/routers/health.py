"""Health and disk management endpoints for the lifecycle server."""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends

from ..auth import verify_auth
from ..backend import ExecutionBackend
from ..config import LifecycleConfig
from ..deps import _get_backend, _get_config
from ..models import DiskUsageResponse, ReclaimResponse

router = APIRouter(tags=["health"])


@router.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/disk", response_model=DiskUsageResponse)
async def get_disk_usage(
    _auth: None = Depends(verify_auth),
    backend: ExecutionBackend = Depends(_get_backend),
    config: LifecycleConfig = Depends(_get_config),
) -> DiskUsageResponse:
    """Host disk usage and Docker storage breakdown."""
    usage = shutil.disk_usage(config.disk_path)
    docker_df = await backend.disk_df()
    return DiskUsageResponse(
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        warn_threshold_pct=config.disk_warn_pct,
        docker=docker_df,
    )


@router.post("/disk/reclaim", response_model=ReclaimResponse)
async def reclaim_build_cache(
    _auth: None = Depends(verify_auth),
    backend: ExecutionBackend = Depends(_get_backend),
) -> ReclaimResponse:
    """Prune Docker build cache and return bytes freed."""
    space_reclaimed = await backend.prune_builds()
    return ReclaimResponse(space_reclaimed_bytes=space_reclaimed)
