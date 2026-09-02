"""Periodic volume-audit scheduler: measures, records, and reports on managed volumes."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...lifecycle.backends import ExecutionBackend
    from ...lifecycle.config import LifecycleConfig

from ...registry.config_store import ComponentConfigStore
from .growth import compute_growth_records
from .models import (
    AuditFinding,
    VolumeAuditResponse,
    VolumeGrowthRecord,
    VolumeSizeSnapshot,
)
from .reporter import report_finding

logger = logging.getLogger(__name__)


class VolumeAuditScheduler:
    """Periodic background scanner that tracks Docker volume growth."""

    def __init__(
        self,
        config: LifecycleConfig,
        backend: ExecutionBackend,
        component_config_store: ComponentConfigStore,
    ) -> None:
        self._config = config
        self._backend = backend
        self._component_config_store = component_config_store
        self._snapshot_path = Path(config.volume_audit_snapshot_path)
        self._findings_path = Path(config.volume_audit_findings_path)
        self._last_records: list[VolumeGrowthRecord] = []
        self._last_scan_at: datetime | None = None
        # Serialize scan passes: the background loop and the caretaker's
        # phase_volumes both call run_once at startup, which put two
        # concurrent du helpers on the same large volume (2026-09-02,
        # mill-mill-data) — each slowing the other toward its timeout.
        self._scan_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Snapshot persistence
    # ------------------------------------------------------------------

    def _load_snapshots(self) -> dict[str, VolumeSizeSnapshot]:
        """Load previous snapshots from disk; return empty dict on missing/corrupt file."""
        if not self._snapshot_path.exists():
            return {}
        try:
            raw = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
            return {k: VolumeSizeSnapshot.model_validate(v) for k, v in raw.items()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load volume snapshots: %s", exc)
            return {}

    def _save_snapshots(self, snaps: dict[str, VolumeSizeSnapshot]) -> None:
        self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.model_dump(mode="json") for k, v in snaps.items()}
        self._snapshot_path.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

    def _load_recent_findings(self) -> list[AuditFinding]:
        if not self._findings_path.exists():
            return []
        try:
            raw: list[dict[str, Any]] = json.loads(
                self._findings_path.read_text(encoding="utf-8")
            )
            return [AuditFinding.model_validate(f) for f in raw[-5:]]
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------
    # Scan logic
    # ------------------------------------------------------------------

    async def run_once(self) -> list[VolumeGrowthRecord]:
        """Perform one full scan pass: measure all managed named volumes,
        compute growth vs previous snapshot, emit findings.

        Returns the list of VolumeGrowthRecord produced this pass.

        Passes are serialized: a call that arrives while another scan is in
        flight waits for it rather than measuring the same volumes twice.
        """
        async with self._scan_lock:
            return await self._run_once_locked()

    async def _run_once_locked(self) -> list[VolumeGrowthRecord]:
        # 1. Collect all (component_id, volume_name) pairs
        all_configs = self._component_config_store.all()  # synchronous
        volume_owners: list[tuple[str, str]] = []
        for comp_cfg in all_configs:
            for vol_name in comp_cfg.named_volumes:
                volume_owners.append((comp_cfg.id, vol_name))

        if not volume_owners:
            logger.debug("VolumeAudit: no named volumes registered, skipping scan")
            return []

        # 2. Load previous snapshots up front so a per-volume measure
        #    failure can preserve that volume's baseline for the next scan.
        previous = self._load_snapshots()

        # 3. Measure each volume. A single volume's measurement failure is
        #    surfaced as an AuditFinding and the scan continues with the
        #    remaining volumes, rather than aborting the whole pass.
        now = datetime.now(tz=UTC)
        current: dict[str, VolumeSizeSnapshot] = {}
        preserved: dict[str, VolumeSizeSnapshot] = {}
        measure_findings: list[AuditFinding] = []
        for component_id, vol_name in volume_owners:
            try:
                size = await self._backend.measure_volume_bytes(vol_name)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "VolumeAudit: failed to measure volume %s: %s",
                    vol_name,
                    exc,
                )
                measure_findings.append(
                    AuditFinding(
                        volume_name=vol_name,
                        component_id=component_id,
                        finding_at=now,
                        size_bytes=0,
                        delta_bytes=0,
                        growth_pct=0.0,
                        detail=f"Failed to measure volume {vol_name!r}: {exc}",
                    )
                )
                # Preserve the prior baseline (kept out of this pass's growth
                # records) so tracking resumes once the volume is measurable.
                prev_snap = previous.get(vol_name)
                if prev_snap is not None:
                    preserved[vol_name] = prev_snap
                continue
            current[vol_name] = VolumeSizeSnapshot(
                volume_name=vol_name,
                component_id=component_id,
                measured_at=now,
                size_bytes=size,
            )

        # 4. Compute growth vs previous snapshot; prepend measure failures.
        records, findings = compute_growth_records(
            current,
            previous,
            self._config.volume_audit_growth_threshold_pct,
            self._config.volume_audit_min_delta_bytes,
        )
        findings = measure_findings + findings

        # 5. Record findings locally (log + findings JSON). No tickets —
        # the caretaker just updates containers (operator decision,
        # 2026-09-01).
        for finding in findings:
            try:
                await report_finding(finding, self._findings_path)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "report_finding failed for %s: %s",
                    finding.volume_name,
                    exc,
                )

        # 6. Persist new snapshot and update in-memory state. Baselines for
        #    volumes that failed to measure are carried forward unchanged.
        self._save_snapshots({**preserved, **current})
        self._last_records = records
        self._last_scan_at = now

        logger.info(
            "VolumeAudit: scanned %d volume(s), %d finding(s)",
            len(records),
            len(findings),
        )
        return records

    async def loop(self, interval_seconds: int) -> None:
        """Run run_once() repeatedly with *interval_seconds* sleep between passes.
        Designed to be run as a background asyncio Task (cancelled on shutdown).
        """
        logger.info(
            "VolumeAudit: starting background loop (interval=%ds)", interval_seconds
        )
        try:
            while True:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.error("VolumeAudit scan failed: %s", exc)
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("VolumeAudit: background loop cancelled")
            raise

    # ------------------------------------------------------------------
    # Read path (for GET /volumes/audit)
    # ------------------------------------------------------------------

    def get_audit_response(self) -> VolumeAuditResponse:
        return VolumeAuditResponse(
            enabled=True,
            last_scan_at=self._last_scan_at,
            volumes=self._last_records,
            recent_findings=self._load_recent_findings(),
        )
