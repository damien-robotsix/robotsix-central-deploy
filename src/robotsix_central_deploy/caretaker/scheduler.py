"""Caretaker scheduler — orchestrates the periodic maintenance pass."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robotsix_http import RetryClient

from .mill_client import MillClient
from .models import CaretakerFinding, CaretakerReport, FindingKind
from .phases import (
    component_auto_update_enabled,
    phase_health,
    phase_update,
    phase_volumes,
)

if TYPE_CHECKING:
    from ..lifecycle.backends import ExecutionBackend
    from ..lifecycle.config import LifecycleConfig
    from ..lifecycle.models import SelfInspect
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
    """Orchestrates the four-phase caretaker pass on a configurable interval.

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

        #: Digest of the last detached self-update attempt this boot. Guards
        #: the end-of-pass self-update step: if ``update_available`` fails to
        #: clear (updater lost the race, flag stuck), the same pending digest
        #: is not re-triggered — no repeated self-restarts within one boot.
        self._last_self_update_digest: str | None = None

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
        """Execute a full four-phase caretaker pass."""
        started_at = datetime.now(tz=UTC)
        errors: list[str] = []
        findings: list[CaretakerFinding] = []
        phases_run: list[str] = []

        settings = await self._settings_store.get()

        # 1. Phase: UPDATE
        # Identify our own container so phase_update never tries to auto-deploy
        # (and thereby kill) the process running this pass — see phase_update.
        self_container_name = ""
        # Kept beyond the block below: the end-of-pass self-update step hands
        # it to the detached updater (POST /system/update path).
        self_info: SelfInspect | None = None
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

        # Probe the mill for in-flight stages: recreating its container
        # mid-implement aborts hour-scale agent runs, so a busy mill keeps
        # its pending update for the next pass. Fails open — an unreachable
        # mill is not treated as busy (deploying is then the likely remedy).
        busy_components: dict[str, str] = {}
        mill_id = settings.mill_component_id
        if mill_id:
            mill_url = MillClient.derive_url_from_registry(
                self._registry, self._component_config_store, mill_id
            )
            if mill_url is not None:
                mill_client = MillClient(mill_url, self._http_client)
                busy_reason = await mill_client.active_stage_summary()
                if busy_reason is not None:
                    busy_components[mill_id] = busy_reason

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
                busy_components=busy_components,
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

        # 4. Phase: SELF-UPDATE — detached self-updater for the plane itself.
        # At the end of the pass, if our own image has a pending update, launch
        # the same detached watchtower updater POST /system/update uses
        # (backend.trigger_self_update), so central-deploy updates itself
        # within one caretaker interval with zero human involvement. phase_update
        # must never deploy our own container in-process (2026-07-21 loop); this
        # detached path survives the swap and runs last, so the updater replaces
        # us only after every other phase has finished.
        try:
            self_update_finding = await self._maybe_trigger_self_update(
                self_info=self_info,
            )
            # Record the phase whenever it ran, matching health/volumes which
            # append even when they produce zero findings.
            phases_run.append("self-update")
            if self_update_finding is not None:
                findings.append(self_update_finding)
        except Exception as exc:
            logger.exception("self-update step crashed")
            errors.append(f"self-update: {exc}")

        # 5. Record findings locally. The caretaker never files tickets
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

    async def _maybe_trigger_self_update(
        self,
        self_info: SelfInspect | None,
    ) -> CaretakerFinding | None:
        """Launch the detached self-updater when the plane's own image has an update.

        Mirrors ``POST /system/update``: ``backend.trigger_self_update``
        spawns a one-shot watchtower container that survives the swap,
        unlike an in-process ``backend.deploy`` (which phase_update must
        never run on our own container — the 2026-07-21 self-restart loop).

        Guards, all of which must pass:

        * The backend can identify our own container (containerised).
        * A self record exists with ``update_available`` true.
        * The central-deploy component's unified per-component auto-update
          flag is on (default on) — evaluated through
          ``phases.component_auto_update_enabled``, the SAME predicate every
          other component's auto-update goes through, so central-deploy is
          just one more component with no caretaker special case.
        * The pending digest differs from the running digest AND from the
          digest of the last self-update attempt this boot — a stale
          ``update_available`` flag (updater lost the race, or the flag
          failed to clear) can then not restart the plane more than once
          per boot.
        """
        if self_info is None:
            # Not containerised / backend cannot self-inspect (noop): there
            # is no own container to replace, nothing to update.
            return None

        records = await self._store.list_all()
        self_record = next(
            (
                r
                for r in records
                if not r.component_id and r.container_name == self_info.container_name
            ),
            None,
        )
        if self_record is None:
            logger.debug("caretaker: no self record, skipping self-update")
            return None
        if not self_record.update_available:
            return None

        # Gate the plane's own self-update on the SAME unified per-component
        # auto-update flag every other component uses, resolved through the
        # shared predicate in phases.py — central-deploy is just one more
        # component. A missing config falls through to the default-on
        # behaviour (matching ComponentConfig.caretaker_auto_update's default).
        config = self._component_config_store.get(self_record.name)
        if config is not None and not component_auto_update_enabled(config):
            logger.debug(
                "caretaker: self-update disabled for %s (auto-update off)",
                self_record.name,
            )
            return None

        new_digest = self_record.latest_registry_digest
        # Prefer the LIVE running digest from inspect_self: the detached
        # watchtower updater swaps the container out-of-band and never persists
        # a new deployed_image_digest, so the store can go stale after a
        # successful self-update (its only writer, refresh_record_status, is a
        # status-poll path the caretaker does not invoke). Comparing against
        # the actual running digest — falling back to the store's
        # deployed_image_digest when the live one is unresolvable — means a
        # completed update (pending == running) is never re-triggered,
        # regardless of store freshness.
        running_digest = self_info.running_digest or self_record.deployed_image_digest
        if not new_digest or new_digest == running_digest:
            logger.debug(
                "caretaker: self-update pending but no distinct digest "
                "(running=%s latest=%s), skipping",
                running_digest,
                new_digest,
            )
            return None

        if new_digest == self._last_self_update_digest:
            logger.warning(
                "caretaker: self-update to %s already attempted this boot — "
                "update_available did not clear, skipping to avoid a "
                "self-restart loop",
                new_digest,
            )
            return None

        try:
            updater_id = await self._backend.trigger_self_update(
                self_info,
                self._config.self_update_watchtower_image,
                self._config.docker_socket_url,
                self._config.self_update_docker_api_version,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("caretaker: self-update launch failed: %s", exc)
            return CaretakerFinding(
                component_id=self_record.name,
                kind=FindingKind.UPDATE_FAILED,
                title="Self-update launch failed",
                detail=f"Detached updater could not be started: {exc}",
                severity="error",
            )

        self._last_self_update_digest = new_digest
        logger.warning(
            "caretaker: triggered detached self-update of %s → %s "
            "(updater container %s)",
            self_record.name,
            new_digest,
            updater_id,
        )
        return CaretakerFinding(
            component_id=self_record.name,
            kind=FindingKind.SELF_UPDATE_TRIGGERED,
            title=f"Self-update triggered for {self_record.name}",
            detail=(
                f"Pending digest {new_digest} differs from running "
                f"{running_digest}; launched detached updater {updater_id}. "
                "The server will restart itself shortly."
            ),
        )

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
