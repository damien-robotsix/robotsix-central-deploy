"""Periodic volume-audit scheduler: measures, records, and reports on managed volumes."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_board_agent.client import BoardClient

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
        except Exception as exc:
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
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Scan logic
    # ------------------------------------------------------------------

    async def run_once(self) -> list[VolumeGrowthRecord]:
        """Perform one full scan pass: measure all managed named volumes,
        compute growth vs previous snapshot, emit findings.

        Returns the list of VolumeGrowthRecord produced this pass.
        """
        # 1. Collect all (component_id, volume_name) pairs
        all_configs = self._component_config_store.all()  # synchronous
        volume_owners: list[tuple[str, str]] = []
        for comp_cfg in all_configs:
            for vol_name in comp_cfg.named_volumes:
                volume_owners.append((comp_cfg.id, vol_name))

        if not volume_owners:
            logger.debug("VolumeAudit: no named volumes registered, skipping scan")
            return []

        # 2. Measure each volume
        now = datetime.now(tz=timezone.utc)
        current: dict[str, VolumeSizeSnapshot] = {}
        for component_id, vol_name in volume_owners:
            size = await self._backend.measure_volume_bytes(vol_name)
            current[vol_name] = VolumeSizeSnapshot(
                volume_name=vol_name,
                component_id=component_id,
                measured_at=now,
                size_bytes=size,
            )

        # 3. Load previous and compute growth
        previous = self._load_snapshots()
        records, findings = compute_growth_records(
            current,
            previous,
            self._config.volume_audit_growth_threshold_pct,
            self._config.volume_audit_min_delta_bytes,
        )

        # 4. Create a single board client for this scan pass (reused across
        #    all findings), then emit findings through the report seam.
        board_client = await self._maybe_create_board_client()
        try:
            for finding in findings:
                try:
                    await report_finding(finding, self._findings_path, board_client)
                except Exception as exc:
                    logger.error(
                        "report_finding failed for %s: %s",
                        finding.volume_name,
                        exc,
                    )
        finally:
            if board_client is not None:
                try:
                    await board_client.close()
                except Exception as exc:
                    logger.error("Failed to close board client: %s", exc)

        # 5. Persist new snapshot and update in-memory state
        self._save_snapshots(current)
        self._last_records = records
        self._last_scan_at = now

        logger.info(
            "VolumeAudit: scanned %d volume(s), %d finding(s)",
            len(records),
            len(findings),
        )
        return records

    async def _maybe_create_board_client(self) -> BoardClient | None:
        """Return a BoardClient if board integration is configured, else None."""
        cfg = self._config
        if cfg.board_api_url and cfg.board_api_token and cfg.board_repo_id:
            try:
                from robotsix_board_agent.client import BoardClient
                from robotsix_board_agent.config import BoardAgentSettings

                settings = BoardAgentSettings(
                    board_api_url=cfg.board_api_url,
                    board_api_token=cfg.board_api_token,
                    board_repo_id=cfg.board_repo_id,
                )
                return BoardClient(settings)
            except Exception as exc:
                logger.error("Failed to create board client: %s", exc)
                return None
        return None

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
                except Exception as exc:
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
