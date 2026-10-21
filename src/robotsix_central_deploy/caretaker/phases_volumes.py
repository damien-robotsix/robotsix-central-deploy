"""Caretaker volume phase: retention rules and volume-audit findings.

Split out of :mod:`.phases`, which re-exports :func:`phase_volumes`.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .models import CaretakerFinding, FindingKind

if TYPE_CHECKING:
    from ..lifecycle.backends import ExecutionBackend
    from ..lifecycle.config import LifecycleConfig
    from ..registry.config_store import ComponentConfigStore
    from ..registry.settings_store import SystemSettings
    from .volume_audit.scheduler import VolumeAuditScheduler

logger = logging.getLogger(__name__)


async def _apply_volume_retention(
    backend: ExecutionBackend,
    settings: SystemSettings,
) -> None:
    """Apply the operator's ``volume_retention_rules`` (age-based file
    pruning).  Invalid rules are skipped with a warning; a failing rule
    never aborts the pass.  See ``SystemSettings.volume_retention_rules``
    for the rule shape and the database-store caveat.
    """
    for rule in settings.volume_retention_rules:
        volume_name = str(rule.get("volume_name", "")).strip()
        rel_path = str(rule.get("path", "")).strip().strip("/")
        glob = str(rule.get("glob", "*")).strip() or "*"
        try:
            max_age_days = int(rule.get("max_age_days", 0))
        except (TypeError, ValueError):
            max_age_days = 0
        if not volume_name or max_age_days < 1 or ".." in rel_path.split("/"):
            logger.warning("volume retention: skipping invalid rule %r", rule)
            continue
        try:
            result = await backend.prune_volume_files(
                volume_name, rel_path, glob, max_age_days
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "volume retention: prune failed for %s/%s: %s",
                volume_name,
                rel_path,
                exc,
            )
            continue
        if result.get("removed", 0):
            logger.info(
                "volume retention: %s/%s glob=%s >%dd — removed %d file(s), %d bytes",
                volume_name,
                rel_path,
                glob,
                max_age_days,
                result["removed"],
                result["bytes"],
            )


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

    # 0. Age-based retention (operator-configured, default empty).  Runs
    #    before the growth scan so the scan measures the post-prune sizes.
    #    Results are logged, not turned into findings — routine pruning must
    #    not spawn mill tickets.
    await _apply_volume_retention(backend, settings)

    # 1. Growth scan (reuse VolumeAuditScheduler.run_once)
    try:
        await volume_audit_scheduler.run_once()
    except Exception as exc:  # noqa: BLE001
        logger.error("phase_volumes: run_once failed: %s", exc)
        # Continue with empty findings — the audit scan itself already
        # persisted its own findings to disk.
    audit_resp = volume_audit_scheduler.get_audit_response()

    # Build volume → component_id mapping
    vol_to_component: dict[str, str] = {}
    for comp_cfg in component_config_store.all():
        for vol_name in comp_cfg.named_volumes:
            vol_to_component[vol_name] = comp_cfg.id

    # recent_findings is the cumulative tail of the on-disk findings log, so
    # without an age cutoff every caretaker pass re-reported up to 5 historical
    # findings as if they were fresh (2026-09-02 04:27Z: five "chat-chat-data
    # grew" warnings in one pass, four of them from earlier scans) — a
    # duplicate-ticket source on the mill board.  Report only findings that
    # occurred within the current pass window.
    cutoff = datetime.now(tz=UTC) - timedelta(
        hours=max(1, settings.caretaker_interval_hours)
    )
    for af in audit_resp.recent_findings:
        if af.finding_at < cutoff:
            continue
        comp_id = vol_to_component.get(af.volume_name, "")
        repo_id = ""
        if comp_id:
            owner_cfg = component_config_store.get(comp_id)
            if owner_cfg is not None:
                repo_id = owner_cfg.repo_id
        if af.kind == "measurement_failed":
            findings.append(
                CaretakerFinding(
                    component_id=comp_id,
                    repo_id=repo_id,
                    kind=FindingKind.VOLUME_MEASUREMENT,
                    title=f"Volume {af.volume_name} size could not be measured",
                    detail=af.detail,
                    severity="warning",
                )
            )
            continue
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
            # A volume mounted by ANY container (``in_use`` = docker's
            # ``RefCount > 0``) is not an orphan, whoever owns the
            # container: central-deploy's own compose volumes
            # (central_deploy_data, traefik_letsencrypt, tinyauth_data),
            # sibling volumes and operator side stacks are not in
            # ``named_volumes`` and were flagged on every pass
            # (2026-09-09). Same rule as ``lifecycle.deps.volume``.
            if vol.name and not vol.in_use and vol.name not in declared:
                findings.append(
                    CaretakerFinding(
                        component_id="",
                        repo_id="",
                        kind=FindingKind.VOLUME_ORPHAN,
                        title=f"Orphan Docker volume: {vol.name}",
                        detail=(
                            f"Volume '{vol.name}' ({vol.size_bytes} bytes) "
                            f"is not declared by any component and is not "
                            f"attached to any container"
                        ),
                        severity="warning",
                    )
                )
    except Exception as exc:  # noqa: BLE001
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
    except Exception as exc:  # noqa: BLE001
        logger.error("phase_volumes: disk check failed: %s", exc)

    return findings
