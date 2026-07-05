"""Phase functions for the caretaker daily maintenance pass.

Each function is an async callable that returns ``list[CaretakerFinding]``.
They do NOT import the scheduler or mill client — they are pure logic.
"""

from __future__ import annotations

import logging
import shutil
import time
from typing import TYPE_CHECKING

from ..deploy_lock import release_deploy_lock, try_acquire_deploy_lock
from ..lifecycle.models import DeployHistoryEntry, ServiceState
from .models import CaretakerFinding, FindingKind

if TYPE_CHECKING:
    from ..lifecycle.backends import ExecutionBackend
    from ..lifecycle.config import LifecycleConfig
    from ..lifecycle.store import ServiceStore
    from ..registry.config_store import ComponentConfigStore
    from ..registry.deploy_history_store import DeployHistoryStore
    from ..registry.loader import ComponentRegistry
    from ..registry.settings_store import SystemSettings
    from ..volume_audit.scheduler import VolumeAuditScheduler

logger = logging.getLogger(__name__)


async def phase_update(
    registry: ComponentRegistry,
    store: ServiceStore,
    backend: ExecutionBackend,
    component_config_store: ComponentConfigStore,
    deploy_history_store: DeployHistoryStore,
) -> list[CaretakerFinding]:
    """Deploy updated images for opted-in primary components.

    Only processes ``ServiceRecord``\s where ``component_id == ""``
    (primary), ``update_available == True``, and the component config does
    NOT have ``caretaker_auto_update == False``.  Sibling records are
    excluded — they are managed by the main deploy path.
    """
    findings: list[CaretakerFinding] = []
    records = await store.list_all()

    for record in records:
        # Skip sibling records
        if record.component_id:
            continue

        if not record.update_available:
            continue

        config = component_config_store.get(record.name)
        if config is None:
            logger.warning(
                "phase_update: no config for component %s, skipping", record.name
            )
            continue

        if not config.caretaker_auto_update:
            logger.debug(
                "phase_update: component %s opted out of auto-update", record.name
            )
            continue

        # Pull by repo@digest: a bare "sha256:…" digest is not a valid image
        # reference (docker resolves it as repository "sha256"), so anchor it
        # to the record's repository. Falls back to the plain tag when no
        # digest is recorded.
        repo = (record.image or config.image).rsplit(":", 1)[0]
        if record.latest_registry_digest:
            image_ref = f"{repo}@{record.latest_registry_digest}"
        else:
            image_ref = record.image or config.image

        # Serialise concurrent deploys of the same component (operator + caretaker).
        if not await try_acquire_deploy_lock(record.name):
            logger.info(
                "phase_update: deploy already in progress for %s, skipping",
                record.name,
            )
            continue
        try:
            outcome = await backend.deploy(record, config, image_ref)
            record.state = outcome.state
            record.image_revision = outcome.deployed_digest
            record.deployed_image_digest = outcome.deployed_digest
            record.previous_image_digest = outcome.previous_digest
            record.update_available = False
            await store.put(record)

            try:
                await deploy_history_store.append(
                    record.name,
                    DeployHistoryEntry(
                        digest=outcome.deployed_digest,
                        image_ref=record.latest_registry_digest,
                        timestamp=time.time(),
                        source="caretaker",
                        previous_digest=outcome.previous_digest,
                    ),
                )
            except Exception:
                logger.warning(
                    "phase_update: failed to record history for %s",
                    record.name,
                    exc_info=True,
                )

            findings.append(
                CaretakerFinding(
                    component_id=record.name,
                    repo_id=config.repo_id,
                    kind=FindingKind.UPDATE_APPLIED,
                    title=f"Auto-updated component {record.name}",
                    detail=(
                        f"Deployed {outcome.deployed_digest} "
                        f"(previous: {outcome.previous_digest or 'none'})"
                    ),
                    severity="warning",
                )
            )
            logger.info(
                "phase_update: auto-deployed %s → %s",
                record.name,
                outcome.deployed_digest,
            )
        except Exception as exc:
            logger.error("phase_update: deploy failed for %s: %s", record.name, exc)
            findings.append(
                CaretakerFinding(
                    component_id=record.name,
                    repo_id=config.repo_id,
                    kind=FindingKind.UPDATE_FAILED,
                    title=f"Auto-update failed for {record.name}",
                    detail=str(exc),
                    severity="error",
                )
            )
        finally:
            release_deploy_lock(record.name)

    return findings


async def phase_health(
    registry: ComponentRegistry,
    store: ServiceStore,
    backend: ExecutionBackend,
    component_config_store: ComponentConfigStore,
) -> list[CaretakerFinding]:
    """Check health of ALL managed containers (primary + sibling).

    Emits a ``HEALTH`` finding for any container not in {RUNNING, STARTING}
    or whose Docker healthcheck reports ``"unhealthy"``.
    """
    findings: list[CaretakerFinding] = []
    records = await store.list_all()

    for record in records:
        inspect = await backend.status(record)
        is_unhealthy = (
            inspect.state not in {ServiceState.RUNNING, ServiceState.STARTING}
            or inspect.health == "unhealthy"
        )
        if not is_unhealthy:
            continue

        # Resolve repo_id: for primaries use record.repo_id; for siblings
        # look up the parent's repo_id.
        repo_id = record.repo_id
        if not repo_id and record.component_id:
            parent = await store.get(record.component_id)
            if parent is not None:
                repo_id = parent.repo_id

        findings.append(
            CaretakerFinding(
                component_id=record.name,
                repo_id=repo_id,
                kind=FindingKind.HEALTH,
                title=f"Container {record.name} is unhealthy",
                detail=(
                    f"State: {inspect.state.value}, "
                    f"Health: {inspect.health or 'no healthcheck'}"
                ),
                severity="error",
            )
        )

    return findings


async def phase_volumes(
    volume_audit_scheduler: VolumeAuditScheduler,
    backend: ExecutionBackend,
    component_config_store: ComponentConfigStore,
    config: LifecycleConfig,
    settings: SystemSettings,
) -> list[CaretakerFinding]:
    """Run the volume-audit growth scan plus orphan-volume and disk checks.

    Returns findings for volume growth, orphan volumes, and disk pressure.
    """
    findings: list[CaretakerFinding] = []

    # 1. Growth scan (reuse VolumeAuditScheduler.run_once)
    try:
        await volume_audit_scheduler.run_once()
    except Exception as exc:
        logger.error("phase_volumes: run_once failed: %s", exc)
        # Continue with empty findings — the audit scan itself already
        # persisted its own findings to disk.
    audit_resp = volume_audit_scheduler.get_audit_response()

    # Build volume → component_id mapping
    vol_to_component: dict[str, str] = {}
    for comp_cfg in component_config_store.all():
        for vol_name in comp_cfg.named_volumes:
            vol_to_component[vol_name] = comp_cfg.id

    for af in audit_resp.recent_findings:
        comp_id = vol_to_component.get(af.volume_name, "")
        repo_id = ""
        if comp_id:
            owner_cfg = component_config_store.get(comp_id)
            if owner_cfg is not None:
                repo_id = owner_cfg.repo_id
        findings.append(
            CaretakerFinding(
                component_id=comp_id,
                repo_id=repo_id,
                kind=FindingKind.VOLUME_GROWTH,
                title=f"Volume {af.volume_name} grew by {af.growth_pct:.1f}%",
                detail=af.detail,
                severity="warning",
            )
        )

    # 2. Orphan volumes
    try:
        declared: set[str] = set()
        for comp_cfg in component_config_store.all():
            declared.update(comp_cfg.named_volumes)

        df = await backend.disk_df()
        for vol in df.volumes:
            if vol.name and vol.name not in declared:
                findings.append(
                    CaretakerFinding(
                        component_id="",
                        repo_id="",
                        kind=FindingKind.VOLUME_ORPHAN,
                        title=f"Orphan Docker volume: {vol.name}",
                        detail=(
                            f"Volume '{vol.name}' ({vol.size_bytes} bytes) "
                            f"is not declared by any component"
                        ),
                        severity="warning",
                    )
                )
    except Exception as exc:
        logger.error("phase_volumes: orphan detection failed: %s", exc)

    # 3. Disk usage
    try:
        usage = shutil.disk_usage(config.disk_path)
        pct_free = (usage.free / usage.total) * 100
        if pct_free < settings.disk_warn_pct:
            pct_used = (usage.used / usage.total) * 100
            findings.append(
                CaretakerFinding(
                    component_id="",
                    repo_id="",
                    kind=FindingKind.DISK,
                    title=f"Disk usage at {pct_used:.1f}%",
                    detail=(
                        f"Host disk is {pct_used:.1f}% full "
                        f"({usage.used // (1024**3)} GiB / "
                        f"{usage.total // (1024**3)} GiB); "
                        f"warn threshold is {settings.disk_warn_pct}% free"
                    ),
                    severity="error" if pct_free < 5 else "warning",
                )
            )
    except Exception as exc:
        logger.error("phase_volumes: disk check failed: %s", exc)

    return findings
