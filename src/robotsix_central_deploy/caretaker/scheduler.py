"""Caretaker scheduler — orchestrates the periodic maintenance pass."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .mill_client import MillClient
from .models import CaretakerFinding, CaretakerReport
from .phases import phase_health, phase_update, phase_volumes

if TYPE_CHECKING:
    from ..lifecycle.backend import ExecutionBackend
    from ..lifecycle.config import LifecycleConfig
    from ..lifecycle.store import ServiceStore
    from ..registry.config_store import ComponentConfigStore
    from ..registry.loader import ComponentRegistry
    from ..registry.settings_store import SystemSettingsStore
    from ..lifecycle.volume_audit.scheduler import VolumeAuditScheduler

logger = logging.getLogger(__name__)

_MAX_LOCAL_FINDINGS = 200


class CaretakerScheduler:
    """Orchestrates the three-phase caretaker pass on a configurable interval.

    Created once in the FastAPI lifespan and always running — when the
    caretaker is disabled the loop simply sleeps without executing phases.
    """

    def __init__(
        self,
        config: LifecycleConfig,
        backend: ExecutionBackend,
        registry: ComponentRegistry,
        service_store: ServiceStore,
        component_config_store: ComponentConfigStore,
        volume_audit_scheduler: VolumeAuditScheduler | None,
        settings_store: SystemSettingsStore,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._config = config
        self._backend = backend
        self._registry = registry
        self._store = service_store
        self._component_config_store = component_config_store
        self._volume_audit_scheduler = volume_audit_scheduler
        self._settings_store = settings_store
        self._http_client = http_client

        self._findings_path = self._resolve_findings_path(config)

        self._last_report: CaretakerReport | None = None
        self._mill_reachable: bool = True

    @staticmethod
    def _resolve_findings_path(config: LifecycleConfig) -> Path:
        """Derive the findings path from the config's data directory convention."""
        # The system_settings_path sits under data/ — use its parent.
        settings_path = Path(config.system_settings_path)
        if settings_path.parent.name:
            return settings_path.parent / "caretaker_findings.jsonl"
        return Path("data") / "caretaker_findings.jsonl"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_once(self) -> CaretakerReport:
        """Execute a full three-phase caretaker pass."""
        started_at = datetime.now(tz=timezone.utc)
        errors: list[str] = []
        findings: list[CaretakerFinding] = []
        phases_run: list[str] = []
        mill_reported = 0
        local_only = 0

        # 1. Discover mill URL
        mill_url = MillClient.derive_url_from_registry(
            self._registry, self._component_config_store
        ) or os.environ.get("MILL_INGEST_URL")
        mill_client = MillClient(mill_url, self._http_client) if mill_url else None

        # 2. Phase: UPDATE
        try:
            update_findings = await phase_update(
                self._registry,
                self._store,
                self._backend,
                self._component_config_store,
            )
            findings.extend(update_findings)
            phases_run.append("update")
        except Exception as exc:
            logger.exception("phase_update crashed")
            errors.append(f"phase_update: {exc}")

        # 3. Phase: HEALTH
        try:
            health_findings = await phase_health(
                self._registry,
                self._store,
                self._backend,
                self._component_config_store,
            )
            findings.extend(health_findings)
            phases_run.append("health")
        except Exception as exc:
            logger.exception("phase_health crashed")
            errors.append(f"phase_health: {exc}")

        # 4. Phase: VOLUMES
        if self._volume_audit_scheduler is not None:
            try:
                settings = await self._settings_store.get()
                volume_findings = await phase_volumes(
                    self._volume_audit_scheduler,
                    self._backend,
                    self._component_config_store,
                    self._config,
                    settings,
                )
                findings.extend(volume_findings)
                phases_run.append("volumes")
            except Exception as exc:
                logger.exception("phase_volumes crashed")
                errors.append(f"phase_volumes: {exc}")
        else:
            logger.debug("phase_volumes skipped: no VolumeAuditScheduler")

        # 5. Report findings: mill ingest or local fallback
        ingest_attempted = 0
        ingest_succeeded = 0
        for f in findings:
            if f.repo_id and mill_client is not None:
                ingest_attempted += 1
                ok = await mill_client.ingest_finding(f)
                if ok:
                    mill_reported += 1
                    ingest_succeeded += 1
                else:
                    self._append_local(f)
                    local_only += 1
            else:
                self._append_local(f)
                local_only += 1

        # mill_reachable: True when no ingest was attempted (no trackable
        # findings) OR at least one ingest succeeded. False only when every
        # attempted ingest call failed.
        mill_reachable = (
            ingest_attempted == 0 or ingest_succeeded > 0
        ) and mill_client is not None
        self._mill_reachable = mill_reachable

        finished_at = datetime.now(tz=timezone.utc)
        report = CaretakerReport(
            started_at=started_at,
            finished_at=finished_at,
            findings=findings,
            phases_run=phases_run,
            mill_reported=mill_reported,
            local_only=local_only,
            errors=errors,
            mill_reachable=mill_reachable,
        )
        self._last_report = report
        return report

    def _append_local(self, finding: CaretakerFinding) -> None:
        """Append a JSON line to the local findings file; trim to last 200."""
        try:
            self._findings_path.parent.mkdir(parents=True, exist_ok=True)
            lines: list[str] = []
            if self._findings_path.exists():
                raw = self._findings_path.read_text(encoding="utf-8")
                lines = [ln for ln in raw.splitlines() if ln.strip()]
            lines.append(finding.model_dump_json())
            # Keep last N entries
            if len(lines) > _MAX_LOCAL_FINDINGS:
                lines = lines[-_MAX_LOCAL_FINDINGS:]
            self._findings_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:
            logger.error("Failed to write local finding: %s", exc)

    async def get_status(self) -> dict[str, Any]:
        """Return {enabled, last_run_at, mill_reachable, last_report}."""
        settings = await self._settings_store.get()
        return {
            "enabled": settings.caretaker_enabled,
            "last_run_at": (
                self._last_report.finished_at.isoformat()
                if self._last_report is not None
                else None
            ),
            "mill_reachable": self._mill_reachable,
            "last_report": (
                self._last_report.model_dump(mode="json")
                if self._last_report is not None
                else None
            ),
        }

    async def loop(self) -> None:
        """Run the periodic caretaker loop.

        Reads settings each iteration so hot-applied changes take effect
        without a restart.
        """
        logger.info("CaretakerScheduler: background loop starting")
        try:
            while True:
                try:
                    settings = await self._settings_store.get()
                except Exception as exc:
                    logger.error("CaretakerScheduler: failed to read settings: %s", exc)
                    await asyncio.sleep(60)
                    continue

                if settings.caretaker_enabled:
                    try:
                        await self.run_once()
                    except Exception as exc:
                        logger.exception(
                            "CaretakerScheduler: run_once crashed: %s", exc
                        )

                interval = max(settings.caretaker_interval_hours, 1) * 3600
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("CaretakerScheduler: background loop cancelled")
            raise
