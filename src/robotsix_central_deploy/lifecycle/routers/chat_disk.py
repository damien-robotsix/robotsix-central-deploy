"""Disk reclaim endpoint for the chat agent.

Exposes:
- ``POST /chat/disk/reclaim`` — prune dangling images and/or build cache
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import verify_auth
from ..backends import (
    ExecutionBackend,
    PruneImagesResult,
    collect_protected_image_refs,
)
from ..config import LifecycleConfig
from ..deps import (
    _get_backend,
    _get_chat_agent_audit_store,
    _get_component_config_store,
    _get_config,
    _get_store,
)
from ..models import DiskUsageResponse
from ..schemas import ChatAgentDiskReclaimRequest, ChatAgentDiskReclaimResponse
from ..store import ServiceStore
from ...registry.chat_agent_audit_store import ChatAgentAuditEntry, ChatAgentAuditStore
from ...registry.config_store import ComponentConfigStore

from ._chat_common import (
    _check_rate_limit,
    _require_allowed_service,
)

import os
import shutil

router = APIRouter(tags=["chat"])


# ---------------------------------------------------------------------------
# POST /chat/disk/reclaim
# ---------------------------------------------------------------------------


@router.post(
    "/chat/disk/reclaim",
    response_model=ChatAgentDiskReclaimResponse,
    summary="Prune dangling images and/or build cache (chat-agent allowlisted)",
    responses={
        403: {"description": "Service not allowlisted"},
        429: {"description": "Rate limited"},
    },
)
async def chat_disk_reclaim(
    body: ChatAgentDiskReclaimRequest,
    request: Request,
    backend: ExecutionBackend = Depends(_get_backend),
    store: ServiceStore = Depends(_get_store),
    config: LifecycleConfig = Depends(_get_config),
    component_config_store: ComponentConfigStore = Depends(_get_component_config_store),
    audit_store: ChatAgentAuditStore = Depends(_get_chat_agent_audit_store),
    _auth: None = Depends(verify_auth),
) -> ChatAgentDiskReclaimResponse:
    """Prune safe Docker disk targets: dangling images and/or build cache.

    Only ``dangling_images`` and ``build_cache`` are accepted — this
    endpoint never touches tagged images, running-container images, or
    named volumes.
    """
    await _require_allowed_service("central-deploy", component_config_store)
    _check_rate_limit(request.app.state, "central-deploy", "disk_reclaim")

    space_reclaimed: int = 0
    operations: list[str] = []
    prune_result = PruneImagesResult()

    if body.build_cache:
        freed = await backend.prune_builds()
        space_reclaimed += freed
        operations.append(f"build_cache={freed}")

    if body.dangling_images:
        protected = await collect_protected_image_refs(store)
        prune_result = await backend.prune_images(protected, force=body.force)
        space_reclaimed += prune_result.space_reclaimed_bytes
        parts: list[str] = [f"dangling_images={prune_result.space_reclaimed_bytes}"]
        if prune_result.removed_count:
            parts.append(f"removed={prune_result.removed_count}")
        if prune_result.stopped_containers_removed:
            parts.append(
                f"stopped_containers_removed={prune_result.stopped_containers_removed}"
            )
        skipped_bits: list[str] = []
        if prune_result.skipped_protected:
            skipped_bits.append(f"protected={prune_result.skipped_protected}")
        if prune_result.skipped_in_use:
            skipped_bits.append(f"in_use={prune_result.skipped_in_use}")
        if prune_result.skipped_intermediate:
            skipped_bits.append(f"intermediate={prune_result.skipped_intermediate}")
        if prune_result.skipped_error:
            skipped_bits.append(f"error={prune_result.skipped_error}")
        if skipped_bits:
            parts.append("skipped(" + ", ".join(skipped_bits) + ")")
        if prune_result.error_summary:
            parts.append(f"errors={prune_result.error_summary!r}")
        operations.append(" ".join(parts))

    # Take a post-reclaim disk snapshot.
    disk_path = os.path.realpath(str(config.disk_path))
    if not disk_path.startswith("/"):
        raise ValueError(f"disk_path must be absolute: {disk_path!r}")
    usage = shutil.disk_usage(disk_path)
    docker_df = await backend.disk_df()
    disk_snapshot = DiskUsageResponse(
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        warn_threshold_pct=config.disk_warn_pct,
        docker=docker_df,
    )

    detail = (
        f"Reclaimed {space_reclaimed} bytes "
        f"({', '.join(operations) if operations else 'nothing requested'})."
    )

    await audit_store.append(
        ChatAgentAuditEntry(
            component="central-deploy",
            action="disk-reclaim",
            detail=detail,
        )
    )

    return ChatAgentDiskReclaimResponse(
        space_reclaimed_bytes=space_reclaimed,
        detail=detail,
        disk_snapshot=disk_snapshot,
        images_removed=prune_result.removed_count,
        images_skipped_protected=prune_result.skipped_protected,
        images_skipped_in_use=prune_result.skipped_in_use,
        images_skipped_intermediate=prune_result.skipped_intermediate,
        images_skipped_error=prune_result.skipped_error,
        images_error_summary=prune_result.error_summary,
        stopped_containers_removed=prune_result.stopped_containers_removed,
    )
