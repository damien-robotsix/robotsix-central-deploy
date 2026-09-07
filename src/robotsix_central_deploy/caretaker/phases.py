"""Phase functions for the caretaker daily maintenance pass.

Each function is an async callable that returns ``list[CaretakerFinding]``.
They do NOT import the scheduler or mill client — they are pure logic.
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..lifecycle.deploy_lock import release_deploy_lock, try_acquire_deploy_lock
from ..lifecycle.deploy_verify import verify_post_deploy_health
from ..lifecycle.models import DeployHistoryEntry, DeploySource, ServiceState
from .models import CaretakerFinding, FindingKind

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..lifecycle.backends import ExecutionBackend
    from ..lifecycle.config import LifecycleConfig
    from ..lifecycle.models import ComponentInspect, ServiceRecord
    from ..lifecycle.store import ServiceStore
    from ..registry.config_store import ComponentConfigStore
    from ..registry.deploy_history_store import DeployHistoryStore
    from ..registry.env_store import EnvStore
    from ..registry.loader import ComponentRegistry
    from ..registry.models import ComponentConfig
    from ..registry.settings_store import SystemSettings
    from .volume_audit.scheduler import VolumeAuditScheduler

logger = logging.getLogger(__name__)


def component_auto_update_enabled(config: ComponentConfig) -> bool:
    """Single source of truth for the per-component auto-update decision.

    Every caretaker path that asks "may I auto-update component X?" — both
    ``phase_update`` for managed components and the plane's own end-of-pass
    self-update in the scheduler — routes through this one predicate, so
    central-deploy is governed exactly like any other component (no special
    caretaker case).
    """
    return config.auto_update_enabled


async def phase_update(
    registry: ComponentRegistry,
    store: ServiceStore,
    backend: ExecutionBackend,
    component_config_store: ComponentConfigStore,
    deploy_history_store: DeployHistoryStore,
    env_store: EnvStore,
    self_container_name: str = "",
    self_identity_known: bool = True,
    busy_components: Mapping[str, str] | None = None,
) -> list[CaretakerFinding]:
    """Deploy updated images for opted-in primary components.

    Only processes ``ServiceRecord``\\s where ``component_id == ""``
    (primary), ``update_available == True``, and the component config does
    NOT have ``auto_update_enabled == False``.  Sibling records are
    excluded — they are managed by the main deploy path.

    The record matching ``self_container_name`` (the container this caretaker
    runs inside) is also skipped: an in-process ``backend.deploy`` of our own
    container stops+recreates us mid-pass, so ``update_available=False`` is
    never persisted and the replacement self-updates forever. Self-update is
    handled only via the detached watchtower updater that survives the swap —
    ``POST /system/update`` and the caretaker's end-of-pass self-update step
    both use that path.

    When ``self_identity_known`` is False the caller could not determine which
    container it runs inside, so no record can be ruled out as self and the
    whole phase is skipped — see the fail-closed note below.

    ``busy_components`` maps component names to a human-readable reason why
    their deploy must wait (the mill with heavy stages in flight); those
    records keep ``update_available`` and are retried next pass.
    """
    findings: list[CaretakerFinding] = []

    # Fail closed on unknown self-identity. The self-skip below can only
    # exclude a record it can name; with no name every record is potentially
    # this very container, and deploying it kills the management plane with no
    # detached recreator to bring it back (2026-07-31 outage: a mid-scan
    # NotFound made inspect_self return None, the guard fell through, and
    # phase_update deployed central-deploy on top of itself). Skipping a pass
    # only defers updates to the next one; guessing wrong takes the fleet down.
    if not self_identity_known:
        logger.warning(
            "phase_update: self-identity unknown, skipping the update phase "
            "(cannot rule out deploying our own container)"
        )
        return [
            CaretakerFinding(
                kind=FindingKind.UPDATE_FAILED,
                title="Update phase skipped: self-identity unknown",
                detail=(
                    "The caretaker could not determine which container it runs "
                    "inside, so it cannot guarantee an auto-deploy would not "
                    "replace the management plane itself. No component was "
                    "updated this pass; the next pass retries. Self-update "
                    "remains available via POST /system/update."
                ),
                severity="warning",
            )
        ]

    records = await store.list_all()

    for record in records:
        # Skip sibling records
        if record.component_id:
            continue

        # Never auto-deploy the container this caretaker runs inside. The
        # deploy would replace our own container before this pass can persist
        # ``update_available=False``, so every replacement boots, still sees
        # the update as pending, and self-updates again — an unbreakable loop
        # that took down deploy.robotsix.net on 2026-07-21. Self-update is
        # instead driven by the detached updater: POST /system/update or the
        # caretaker's end-of-pass self-update step (same path).
        if self_container_name and record.container_name == self_container_name:
            logger.debug(
                "phase_update: skipping self-component %s "
                "(self-update handled by the detached updater)",
                record.name,
            )
            continue

        if not record.update_available:
            continue

        # A component the caller marked busy (the mill with implement/ci_fix
        # stages in flight — see the scheduler's /active probe) keeps its
        # pending update for the next pass: recreating it mid-stage aborts
        # hour-scale agent runs whose work is then redone from scratch.
        if busy_components and record.name in busy_components:
            logger.info(
                "phase_update: deferring %s — %s",
                record.name,
                busy_components[record.name],
            )
            continue

        config = component_config_store.get(record.name)
        if config is None:
            logger.warning(
                "phase_update: no config for component %s, skipping", record.name
            )
            continue

        if not component_auto_update_enabled(config):
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
        if not await try_acquire_deploy_lock(record.name, source="caretaker"):
            logger.info(
                "phase_update: deploy already in progress for %s, skipping",
                record.name,
            )
            continue
        try:
            # Recreating the container with only the static registry env would
            # silently drop EnvStore-provisioned variables (API keys, secrets),
            # so merge them exactly like the manual deploy path does.
            merged_env = await env_store.get_merged_env(record.name, config.env)
            # Resolve credentials shared by other components via scope tags.
            if config.consumed_scopes:
                cred_env = await env_store.resolve_consumed_credentials(
                    record.name, config.consumed_scopes
                )
                if cred_env:
                    merged_env = {**cred_env, **merged_env}
            deploy_config = config.model_copy(update={"env": merged_env})
            outcome = await backend.deploy(record, deploy_config, image_ref)
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
                        source=DeploySource.CARETAKER,
                        previous_digest=outcome.previous_digest,
                    ),
                )
            except Exception:
                logger.warning(
                    "phase_update: failed to record history for %s",
                    record.name,
                    exc_info=True,
                )

            logger.info(
                "phase_update: auto-deployed %s → %s",
                record.name,
                outcome.deployed_digest,
            )

            # Verify the auto-deployed image actually stays up. An image whose
            # config schema changed can boot, pass its startup healthcheck,
            # then crash-loop under Docker's restart policy — the 2026-09-05
            # hexarchy incident restarted 110 times with nothing noticing.
            # Poll runtime state + RestartCount for a window; a restarting
            # container or a growing RestartCount is a FAILED deploy that must
            # produce a signal carrying the crash-log excerpt.
            verification = await verify_post_deploy_health(backend, record)
            if not verification.ok:
                record.state = ServiceState.FAILED
                record.last_error = verification.reason
                await store.put(record)
                detail = (
                    f"Auto-update of {record.name} to {outcome.deployed_digest} "
                    f"landed a crash-looping container: {verification.reason}.\n\n"
                    f"Last container logs:\n{verification.crash_log}"
                    if verification.crash_log
                    else (
                        f"Auto-update of {record.name} to "
                        f"{outcome.deployed_digest} landed a crash-looping "
                        f"container: {verification.reason}."
                    )
                )
                logger.error(
                    "phase_update: post-deploy verification failed for %s: %s",
                    record.name,
                    verification.reason,
                )
                findings.append(
                    CaretakerFinding(
                        component_id=record.name,
                        repo_id=config.repo_id,
                        kind=FindingKind.UPDATE_FAILED,
                        title=f"Auto-update crash loop: {record.name}",
                        detail=detail,
                        severity="error",
                    )
                )
        except Exception as exc:  # noqa: BLE001
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
                detail=await _health_finding_detail(backend, record, inspect),
                severity="error",
            )
        )

    return findings


# Lines of container log to attach to a health finding. Enough to carry a
# traceback or a repeated startup error; short enough to stay readable in a
# ticket body and well inside any downstream ingest limit.
_HEALTH_LOG_TAIL_LINES = 40


async def _health_finding_detail(
    backend: ExecutionBackend,
    record: ServiceRecord,
    inspect: ComponentInspect,
) -> str:
    """Build the body of a HEALTH finding, including recent container logs.

    The detail used to be just ``"State: …, Health: …"``. That is a true
    statement and a useless ticket: it names no symptom, so the mill's refine
    stage cannot turn it into a spec and the ticket blocks with nothing
    anyone can act on (observed 2026-07-31 for ``mail-ingester``, whose real
    cause — the container printing CLI usage and exiting because it had no
    command — was sitting in the first line of its logs the whole time).

    Log capture is best-effort: a finding without logs is still worth
    emitting, so any failure degrades to a note rather than propagating.
    """
    header = (
        f"State: {inspect.state.value}, Health: {inspect.health or 'no healthcheck'}"
    )
    if record.image:
        header += f"\nImage: {record.image}"

    try:
        logs = await backend.get_container_logs(record, tail=_HEALTH_LOG_TAIL_LINES)
    except Exception:
        logger.warning(
            "phase_health: could not read logs for %s", record.name, exc_info=True
        )
        logs = ""

    if not logs.strip():
        return f"{header}\n\nNo container logs available."

    return (
        f"{header}\n\n"
        f"Last {_HEALTH_LOG_TAIL_LINES} log lines:\n\n"
        f"```\n{logs.strip()}\n```"
    )


async def phase_restart_watchdog(
    store: ServiceStore,
    backend: ExecutionBackend,
    previous_restart_counts: dict[str, int],
) -> list[CaretakerFinding]:
    """Flag any managed container whose ``RestartCount`` grew since last scrape.

    This is the always-on crash-loop safety net: a component can start
    restarting repeatedly outside any deploy window (config drift, a
    dependency outage), and nothing else notices (2026-09-05 incident — a
    hexarchy crash loop ran ~1.5h on host ``bequiet`` unseen). Each pass reads
    every managed container's ``RestartCount`` and compares it to the value
    recorded on the previous pass; a growing count means the container is
    crash-looping and a ``CRASH_LOOP`` finding is emitted carrying the same
    last-crash-log excerpt a HEALTH finding carries, for the escalation
    consumer.

    ``previous_restart_counts`` is the scheduler-owned baseline, keyed by
    record name and mutated in place across passes:

    * First observation of a container only records a baseline — never a
      finding — so a stable component is never flagged (no false positive
      across a normal scrape window).
    * Growth (``current > previous``) emits a finding; the baseline is then
      advanced to ``current`` so a container that keeps crash-looping is
      re-flagged each interval it keeps restarting.
    * Containers that have gone away are dropped from the baseline, so a
      later redeploy re-baselines instead of comparing against a stale count.

    Runtime state is read straight from ``get_container_diagnostics``
    (``RestartCount``), which the multi-host backend routes to the owning
    host — so remote components (host ``bequiet``) are covered without gating
    on any cosmetic ``/diagnose`` routing/label verdict field.
    """
    findings: list[CaretakerFinding] = []
    records = await store.list_all()
    seen: set[str] = set()

    for record in records:
        try:
            diag = await backend.get_container_diagnostics(record)
        except Exception:
            logger.warning(
                "phase_restart_watchdog: diagnostics failed for %s",
                record.name,
                exc_info=True,
            )
            continue

        if not diag.get("exists"):
            # No container yet (or it was removed): don't baseline, and let
            # the stale-key prune below forget any prior count.
            continue

        current = int(diag.get("restart_count", 0) or 0)
        seen.add(record.name)
        previous = previous_restart_counts.get(record.name)
        previous_restart_counts[record.name] = current

        if previous is None or current <= previous:
            # First observation (baseline only) or a stable/reset count —
            # not crash-looping.
            continue

        # RestartCount grew across the interval → crash-looping.
        repo_id = record.repo_id
        if not repo_id and record.component_id:
            parent = await store.get(record.component_id)
            if parent is not None:
                repo_id = parent.repo_id

        findings.append(
            CaretakerFinding(
                component_id=record.name,
                repo_id=repo_id,
                kind=FindingKind.CRASH_LOOP,
                title=f"Container {record.name} is crash-looping",
                detail=await _crash_loop_finding_detail(
                    backend, record, previous, current
                ),
                severity="error",
            )
        )

    # Forget baselines for containers no longer present, so a fresh redeploy
    # starts from a clean baseline rather than comparing against a stale count.
    for stale in set(previous_restart_counts) - seen:
        del previous_restart_counts[stale]

    return findings


async def _crash_loop_finding_detail(
    backend: ExecutionBackend,
    record: ServiceRecord,
    previous_count: int,
    current_count: int,
) -> str:
    """Build a CRASH_LOOP finding body, including recent container logs.

    Mirrors ``_health_finding_detail``: the RestartCount delta names the
    symptom and the log tail carries the crash cause (a traceback or a
    repeated startup error), so the escalation consumer gets an actionable
    signal rather than a bare "restarting" verdict. Log capture is
    best-effort — a finding without logs is still worth emitting.
    """
    header = (
        f"RestartCount grew from {previous_count} to {current_count} across one "
        f"caretaker interval — the container is crash-looping."
    )
    if record.image:
        header += f"\nImage: {record.image}"

    try:
        logs = await backend.get_container_logs(record, tail=_HEALTH_LOG_TAIL_LINES)
    except Exception:
        logger.warning(
            "phase_restart_watchdog: could not read logs for %s",
            record.name,
            exc_info=True,
        )
        logs = ""

    if not logs.strip():
        return f"{header}\n\nNo container logs available."

    return (
        f"{header}\n\n"
        f"Last {_HEALTH_LOG_TAIL_LINES} log lines:\n\n"
        f"```\n{logs.strip()}\n```"
    )


async def phase_auto_rollback(
    crash_loop_findings: list[CaretakerFinding],
    store: ServiceStore,
    backend: ExecutionBackend,
    component_config_store: ComponentConfigStore,
    deploy_history_store: DeployHistoryStore,
    env_store: EnvStore,
    self_container_name: str = "",
) -> list[CaretakerFinding]:
    """Roll a verified failed deploy back to its previous image (opt-in).

    A ``CRASH_LOOP`` finding from ``phase_restart_watchdog`` is the
    verified-failed-deploy signal (RestartCount grew across a scrape
    interval). When the operator has enabled
    ``caretaker_auto_rollback_enabled`` — the scheduler only calls this
    phase then — each such component is recreated from its
    ``previous_image_digest`` via ``backend.rollback``, restoring the last
    image that was running before the bad deploy (the 2026-09-05 hexarchy
    crash loop would have self-healed this way).

    This is **destructive** (it recreates a running container with a prior
    image), so the behaviour ships behind an OFF-by-default flag and every
    rollback is recorded twice over: a ``ROLLBACK_APPLIED`` finding naming
    the from→to digests, and a ``DeploySource.ROLLBACK`` deploy-history
    entry. A backend failure yields a ``ROLLBACK_FAILED`` finding and never
    aborts the pass.

    Only primary components with a recorded ``previous_image_digest`` are
    rolled back:

    * The self-container (the management plane) is skipped — it cannot
      safely replace itself in-process (mirrors ``phase_update`` and the
      ``POST /services/{name}/rollback`` guard); self-recovery is the
      detached updater's job.
    * A sibling record (or any name without a component config) is skipped
      — ``component_config_store.get`` returns ``None`` for it.
    * A component with no prior digest has nothing to roll back to.
    """
    findings: list[CaretakerFinding] = []

    for finding in crash_loop_findings:
        if finding.kind is not FindingKind.CRASH_LOOP:
            continue
        name = finding.component_id
        if not name:
            continue

        record = await store.get(name)
        if record is None:
            continue

        # Never roll back the container this caretaker runs inside: an
        # in-process recreate would replace the management plane mid-pass
        # (same hazard phase_update guards against). Self-recovery uses the
        # detached updater, not this path.
        if self_container_name and record.container_name == self_container_name:
            logger.debug("phase_auto_rollback: skipping self-component %s", record.name)
            continue

        config = component_config_store.get(record.name)
        if config is None:
            # Siblings and unregistered names have no primary config here.
            logger.debug("phase_auto_rollback: no config for %s, skipping", record.name)
            continue

        if not record.previous_image_digest:
            logger.info(
                "phase_auto_rollback: %s has no prior image digest — nothing "
                "to roll back to",
                record.name,
            )
            findings.append(
                CaretakerFinding(
                    component_id=record.name,
                    repo_id=config.repo_id,
                    kind=FindingKind.ROLLBACK_FAILED,
                    title=f"Auto-rollback skipped for {record.name}",
                    detail=(
                        "The component is crash-looping but has no previous "
                        "image digest recorded, so there is no prior image to "
                        "roll back to. Manual intervention is required."
                    ),
                    severity="error",
                )
            )
            continue

        # Serialise against operator/caretaker deploys of the same component.
        if not await try_acquire_deploy_lock(record.name, source="caretaker"):
            logger.info(
                "phase_auto_rollback: deploy already in progress for %s, skipping",
                record.name,
            )
            continue
        try:
            # Recreate the prior container with its full merged env, exactly
            # like phase_update — the static registry env alone would drop
            # EnvStore-provisioned secrets.
            merged_env = await env_store.get_merged_env(record.name, config.env)
            if config.consumed_scopes:
                cred_env = await env_store.resolve_consumed_credentials(
                    record.name, config.consumed_scopes
                )
                if cred_env:
                    merged_env = {**cred_env, **merged_env}
            rollback_config = config.model_copy(update={"env": merged_env})

            old_deployed = record.deployed_image_digest
            old_previous = record.previous_image_digest
            outcome = await backend.rollback(record, rollback_config)

            # Swap digests: rolled-back-to becomes deployed; what we had
            # becomes previous (mirrors the router's one-step rollback).
            record.state = outcome.state
            record.deployed_image_digest = old_previous
            record.previous_image_digest = old_deployed
            record.image_revision = old_previous
            record.last_error = ""
            await store.put(record)

            try:
                await deploy_history_store.append(
                    record.name,
                    DeployHistoryEntry(
                        digest=outcome.deployed_digest,
                        image_ref=record.image,
                        timestamp=time.time(),
                        source=DeploySource.ROLLBACK,
                        previous_digest=old_deployed,
                    ),
                )
            except Exception:
                logger.warning(
                    "phase_auto_rollback: failed to record history for %s",
                    record.name,
                    exc_info=True,
                )

            logger.warning(
                "phase_auto_rollback: rolled %s back %s → %s (crash loop)",
                record.name,
                old_deployed or "unknown",
                outcome.deployed_digest,
            )
            findings.append(
                CaretakerFinding(
                    component_id=record.name,
                    repo_id=config.repo_id,
                    kind=FindingKind.ROLLBACK_APPLIED,
                    title=f"Auto-rolled back {record.name} after crash loop",
                    detail=(
                        f"The deploy of {record.name} was verified failed "
                        f"(crash-looping), so the caretaker rolled it back to "
                        f"its previous image.\n"
                        f"From digest: {old_deployed or 'unknown'}\n"
                        f"To digest:   {outcome.deployed_digest}"
                    ),
                    severity="warning",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "phase_auto_rollback: rollback failed for %s: %s", record.name, exc
            )
            findings.append(
                CaretakerFinding(
                    component_id=record.name,
                    repo_id=config.repo_id,
                    kind=FindingKind.ROLLBACK_FAILED,
                    title=f"Auto-rollback failed for {record.name}",
                    detail=str(exc),
                    severity="error",
                )
            )
        finally:
            release_deploy_lock(record.name)

    return findings


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
