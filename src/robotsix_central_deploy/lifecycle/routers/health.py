"""Health and disk management endpoints for the lifecycle server."""

from __future__ import annotations

import os
import shutil

from fastapi import APIRouter, Depends

from .._disk_utils import discover_mounted_disks
from ..auth import verify_auth
from ..backends import ExecutionBackend, collect_protected_image_refs
from ..config import LifecycleConfig
from ..deps import _get_backend, _get_config, _get_store
from ..models import DiskUsageResponse, PerDiskUsage, ReclaimResponse
from ..store import ServiceStore

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
    """Host disk usage and Docker storage breakdown.

    Reports usage for every mounted data disk individually, plus an
    aggregate total across all discovered disks.
    """
    docker_df = await backend.disk_df()

    disks = discover_mounted_disks()
    per_disk: list[PerDiskUsage] = [
        PerDiskUsage(
            device=d.device,
            mount_point=d.mount_point,
            fs_type=d.fs_type,
            total_bytes=d.total_bytes,
            used_bytes=d.used_bytes,
            free_bytes=d.free_bytes,
        )
        for d in disks
    ]

    # Aggregate across all discovered disks.
    total_bytes = sum(d.total_bytes for d in disks)
    used_bytes = sum(d.used_bytes for d in disks)
    free_bytes = sum(d.free_bytes for d in disks)

    # Fallback to config.disk_path when no disks are discovered (e.g.
    # inside a container where findmnt / /proc/mounts are unavailable).
    if not disks:
        disk_path = os.path.realpath(str(config.disk_path))
        if not disk_path.startswith("/"):
            raise ValueError(f"disk_path must be absolute: {disk_path!r}")
        usage = shutil.disk_usage(disk_path)
        total_bytes = usage.total
        used_bytes = usage.used
        free_bytes = usage.free

    return DiskUsageResponse(
        total_bytes=total_bytes,
        used_bytes=used_bytes,
        free_bytes=free_bytes,
        warn_threshold_pct=config.disk_warn_pct,
        docker=docker_df,
        disks=per_disk,
    )


@router.post("/disk/reclaim", response_model=ReclaimResponse)
async def reclaim_build_cache(
    _auth: None = Depends(verify_auth),
    backend: ExecutionBackend = Depends(_get_backend),
    store: ServiceStore = Depends(_get_store),
) -> ReclaimResponse:
    """Prune Docker build cache and dangling images, return bytes freed.

    Dangling images that are rollback targets (deployed or previous digests
    recorded in the service store) are protected from removal.
    """
    space_reclaimed = await backend.prune_builds()
    protected = await collect_protected_image_refs(store)
    space_reclaimed += (await backend.prune_images(protected)).space_reclaimed_bytes
    return ReclaimResponse(space_reclaimed_bytes=space_reclaimed)
