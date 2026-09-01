"""Caretaker scheduler — orchestrates the periodic maintenance pass."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robotsix_http import RetryClient

from .models import CaretakerFinding, CaretakerReport
from .phases import phase_health, phase_update, phase_volumes

if TYPE_CHECKING:
    from ..lifecycle.backends import ExecutionBackend
    from ..lifecycle.config import LifecycleConfig
    from ..lifecycle.store import ServiceStore
    from ..registry.config_store import ComponentConfigStore
    from ..registry.deploy_history_store import DeployHistoryStore
    from ..registry.env_store import EnvStore
    from ..registry.loader import ComponentRegistry
    from ..registry.settings_store import SystemSettingsStore
    from .volume_audit.scheduler import VolumeAuditScheduler

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
        http_client: RetryClient,
        deploy_history_store: DeployHistoryStore,
        env_store: EnvStore,
    ) -> None:
        """Initialise the caretaker scheduler.

        Stores all injected dependencies as private attributes and
        resolves the local findings file path from the config.

        Args:
            config: Full lifecycle configuration.
            backend: Execution backend for container actions.
            registry: Component registry for service lookups.
            service_store: Persistent service state store.
            component_config_store: Component configuration store.
            volume_audit_scheduler: Optional volume audit background scanner.
            settings_store: System settings (caretaker interval, etc.).
            http_client: Shared async HTTP client.
            deploy_history_store: Deployment history store.
            env_store: Environment variable store.
        """
        self._config = config
        self._backend = backend
        self._registry = registry
        self._store = service_store
        self._component_config_store = component_config_store
        self._volume_audit_scheduler = volume_audit_scheduler
        self._settings_store = settings_store
        self._http_client = http_client
        self._deploy_history_store = deploy_history_store
        self._env_store = env_store

        self._findings_path = self._resolve_findings_path(config)

        self._last_report: CaretakerReport | None = None

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
        started_at = datetime.now(tz=UTC)
        errors: list[str] = []
        findings: list[CaretakerFinding] = []
        phases_run: list[str] = []

        settings = await self._settings_store.get()

        # 1. Phase: UPDATE
        # Identify our own container so phase_update never tries to auto-deploy
        # (and thereby kill) the process running this pass — see phase_update.
        self_container_name = ""
        # Distinguishes "this backend has no self container to protect" from
        # "it has one but we could not name it". Only the latter is dangerous:
        # phase_update fails closed on it rather than risk deploying over the
        # management plane.
        self_identity_known = True
        try:
            self_info = await self._backend.inspect_self()
            if self_info is not None:
                self_container_name = self_info.container_name
            else:
                # The backend supports self-inspection but found nothing —
                # a transient API error, or not running in a container.
                self_identity_known = False
        except NotImplementedError:
            # Backend cannot self-inspect at all (noop/non-containerised):
            # there is no own container to accidentally deploy over.
            pass
        except Exception:
            logger.warning("caretaker: inspect_self failed", exc_info=True)
            self_identity_known = False

        try:
            update_findings = await phase_update(
                self._registry,
                self._store,
                self._backend,
                self._component_config_store,
                self._deploy_history_store,
                self._env_store,
                self_container_name,
                self_identity_known,
            )
            findings.extend(update_findings)
            phases_run.append("update")

            # Auto-prune dangling images (opt-in setting); rollback targets
            # in the store are protected. Runs every cycle, not only after
            # applied updates: images also pile up from pulls that bypass the
            # deploy path (self-updates, out-of-band container recreations).
            if settings.image_auto_prune:
                try:
                    # Imported lazily: the lifecycle package's __init__ chain
                    # imports this module, so a top-level import is circular.
                    from ..lifecycle.backends import (
                        collect_protected_image_refs,
                    )

                    protected = await collect_protected_image_refs(self._store)
                    result = await self._backend.prune_images(protected)
                    reclaimed = result.space_reclaimed_bytes
                    if reclaimed:
                        logger.info(
                            "caretaker: image auto-prune reclaimed %d bytes",
                            reclaimed,
                        )
                except Exception:
                    logger.warning("caretaker: image auto-prune failed", exc_info=True)
        except Exception as exc:
            logger.exception("phase_update crashed")
            errors.append(f"phase_update: {exc}")

        # 2. Phase: HEALTH
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

        # 3. Phase: VOLUMES
        if self._volume_audit_scheduler is not None:
            try:
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

        # 4. Record findings locally. The caretaker never files tickets
        # (operator decision, 2026-09-01: it "just updates the containers") —
        # findings land in caretaker_findings.jsonl and the log, where the
        # operator, the fleet monitor, and the chat agent can read them.
        for f in findings:
            logger.warning(
                "caretaker finding [%s] %s%s: %s",
                f.kind.value,
                f.component_id or "host",
                f" ({f.repo_id})" if f.repo_id else "",
                f.title,
            )
            self._append_local(f)

        finished_at = datetime.now(tz=UTC)
        report = CaretakerReport(
            started_at=started_at,
            finished_at=finished_at,
            findings=findings,
            phases_run=phases_run,
            errors=errors,
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
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to write local finding: %s", exc)

    async def get_status(self) -> dict[str, Any]:
        """Return {enabled, last_run_at, last_report}."""
        settings = await self._settings_store.get()
        return {
            "enabled": settings.caretaker_enabled,
            "last_run_at": (
                self._last_report.finished_at.isoformat()
                if self._last_report is not None
                else None
            ),
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
                except Exception as exc:  # noqa: BLE001
                    logger.error("CaretakerScheduler: failed to read settings: %s", exc)
                    await asyncio.sleep(60)
                    continue

                if settings.caretaker_enabled:
                    try:
                        await self.run_once()
                    except Exception as exc:
                        logger.exception(
                            "CaretakerScheduler: run_once crashed: %s",
                            exc,  # noqa: TRY401
                        )

                interval = max(settings.caretaker_interval_hours, 1) * 3600
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("CaretakerScheduler: background loop cancelled")
            raise
