"""Phase functions for the caretaker daily maintenance pass.

Each function is an async callable that returns ``list[CaretakerFinding]``.
They do NOT import the scheduler or mill client — they are pure logic.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Literal

from ..lifecycle.deploy_lock import release_deploy_lock, try_acquire_deploy_lock
from ..lifecycle.deploy_verify import (
    rollback_after_failed_deploy,
    verify_post_deploy_health,
)
from ..lifecycle.models import (
    CLAUDE_AUTH_VOLUME,
    DeployHistoryEntry,
    DeploySource,
    ServiceState,
)
from .models import CaretakerFinding, FindingKind

# Explicit re-exports: these phases live in their own modules but callers
# (the scheduler, tests) keep importing them from here.
from .phases_rollback import phase_auto_rollback as phase_auto_rollback  # noqa: PLC0414
from .phases_volumes import phase_volumes as phase_volumes  # noqa: PLC0414

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..lifecycle.backends import ExecutionBackend
    from ..lifecycle.models import ComponentInspect, ServiceRecord
    from ..lifecycle.store import ServiceStore
    from ..registry.config_store import ComponentConfigStore
    from ..registry.deploy_history_store import DeployHistoryStore
    from ..registry.env_store import EnvStore
    from ..registry.loader import ComponentRegistry
    from ..registry.models import ComponentConfig

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
    # Sibling records are never deployed on their own: they ride along with
    # their parent below. But a sibling can be stale while its parent is
    # current (mail-ingester ran a 4-day-old image behind an up-to-date
    # `mail` on 2026-09-09 because every caretaker deploy of `mail` since
    # 09-05 recreated the primary only), so a stale sibling makes its parent
    # eligible for a pass even when the parent has no update of its own.
    stale_sibling_parents = {
        r.component_id for r in records if r.component_id and r.update_available
    }

    for record in records:
        # Skip sibling records (handled via their parent, see above)
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

        siblings_stale = record.name in stale_sibling_parents
        if not record.update_available and not siblings_stale:
            continue

        # Don't re-pull a registry digest that already crash-looped and was
        # rolled back: retry only once the registry publishes a newer digest.
        # (hexarchy, 2026-10-06 — the caretaker re-deployed the same broken
        # :main image every pass.) A stale sibling still makes the parent
        # eligible for a sibling-only refresh below.
        update_is_known_bad = bool(
            record.failed_update_digest
            and record.latest_registry_digest == record.failed_update_digest
        )
        if update_is_known_bad and not siblings_stale:
            logger.info(
                "phase_update: skipping %s — registry digest %s already "
                "crash-looped and was rolled back; waiting for a newer image",
                record.name,
                record.failed_update_digest,
            )
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
            if record.update_available and not update_is_known_bad:
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
                # A fresh deploy clears any prior known-bad marker — if this
                # image also fails verification the failure block re-sets it.
                record.failed_update_digest = ""
                # The bounded-defer clock (mill busy-guard) is only meaningful while
                # an update waits; a completed deploy resets it so the next update
                # starts a fresh window.
                record.update_pending_since = None
                # A successful deploy supersedes whatever the previous attempt
                # left behind; a stale verifier reason otherwise sticks to a
                # healthy record for days (chat/cost-monitor still carried the
                # 2026-10-11 "unhealthy during the post-deploy window" text on
                # 2026-10-13 after two clean auto-deploys).
                record.last_error = ""
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
                    # Remember this registry digest as known-bad so we don't
                    # re-pull it every pass (requirement: retry only on a newer
                    # image). Recorded before the rollback swaps the digests.
                    record.failed_update_digest = record.latest_registry_digest
                    await store.put(record)
                    logger.error(
                        "phase_update: post-deploy verification failed for %s: %s",
                        record.name,
                        verification.reason,
                    )

                    # Roll the crash-looping container back to its last-good
                    # image instead of leaving it restarting indefinitely
                    # (hexarchy was down 6 days, 2026-10-06). Uses the same
                    # one-step swap POST /services/{name}/rollback performs and
                    # re-syncs the stored digest/health with the live container.
                    recovery = await rollback_after_failed_deploy(
                        backend,
                        record,
                        deploy_config,
                        store,
                        deploy_history_store,
                        source=DeploySource.ROLLBACK,
                        log_context="phase_update",
                    )

                    detail = (
                        f"Auto-update of {record.name} to {outcome.deployed_digest} "
                        f"landed a crash-looping container: {verification.reason}."
                    )
                    if recovery.rolled_back:
                        detail += (
                            f"\n\nRolled back to the previous image "
                            f"{recovery.rolled_back_to}; the broken "
                            f"{outcome.deployed_digest} will not be retried until "
                            f"the registry publishes a newer digest."
                        )
                    elif recovery.error:
                        detail += (
                            f"\n\nAutomatic rollback FAILED ({recovery.error}); the "
                            f"container is still crash-looping and needs manual "
                            f"intervention."
                        )
                    if verification.crash_log:
                        detail += f"\n\nLast container logs:\n{verification.crash_log}"

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
            # Siblings ride along with the parent's auto-update — and are
            # refreshed on their own when only they are stale. The manual and
            # chat deploy paths already fan out; the caretaker path did not
            # (mail-ingester, 2026-09-09). The helper clears each sibling's
            # update_available; a failing sibling is logged, never fatal.
            from ..lifecycle.routers._sibling_utils import (
                _fanout_siblings_deploy_best_effort,
            )

            deployed_sibs = await _fanout_siblings_deploy_best_effort(
                record.name, config, store, backend, "phase_update", env_store
            )
            if deployed_sibs:
                logger.info(
                    "phase_update: auto-deployed sibling(s) of %s: %s",
                    record.name,
                    ", ".join(deployed_sibs),
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


async def phase_claude_auth(
    backend: ExecutionBackend,
    refresh_state: dict[str, Any],
) -> list[CaretakerFinding]:
    """Flag a shared Claude credential that is unusable or cannot be renewed.

    Reads ``backend.check_claude_auth`` for the ``claude-auth`` volume and
    the background refresh loop's last outcome. Emits one ``CLAUDE_AUTH``
    finding when the credential is not ``authenticated``, carries no
    refresh token, or the last refresh grant was rejected.

    2026-10-16 → 10-20: the refresh token expired (``invalid_grant``), the
    access token died four hours later, and mill, chat and invest 401'd on
    every Claude call for four days. ``/claude-auth/status`` had the exact
    reason the whole time; nothing read it. This phase puts that reason
    where the fleet's own escalation reads.
    """
    try:
        status = await backend.check_claude_auth(CLAUDE_AUTH_VOLUME)
    except NotImplementedError:
        return []
    except Exception as exc:  # noqa: BLE001 — a probe failure is itself the finding
        return [
            CaretakerFinding(
                kind=FindingKind.CLAUDE_AUTH,
                title="Claude credential check failed",
                detail=f"check_claude_auth({CLAUDE_AUTH_VOLUME}) raised: {exc}",
                severity="warning",
            )
        ]

    state = str(status.get("status", "error"))
    detail_parts: list[str] = []
    severity: Literal["warning", "error"] = "warning"

    if state != "authenticated":
        severity = "error"
        detail_parts.append(f"status={state}: {status.get('detail') or 'no detail'}")
    elif status.get("refresh_capable") is False:
        detail_parts.append(
            "credential carries no refresh token, so it stops working when "
            "the access token expires: " + str(status.get("detail") or "")
        )

    last_error = refresh_state.get("last_error")
    if last_error:
        detail_parts.append(f"last refresh attempt failed: {last_error}")

    if not detail_parts:
        return []

    return [
        CaretakerFinding(
            kind=FindingKind.CLAUDE_AUTH,
            title=(
                "Claude credential unusable — every Claude-backed component is down"
                if severity == "error"
                else "Claude credential cannot be renewed automatically"
            ),
            detail="; ".join(detail_parts)
            + ". Log in again from the Claude Auth panel (POST /claude-auth/login).",
            severity=severity,
        )
    ]


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
