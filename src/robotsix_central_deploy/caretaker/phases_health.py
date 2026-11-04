"""Health-check and restart-watchdog (crash-loop) caretaker phases.

Part of the caretaker phase set re-exported by :mod:`.phases`. Every
function here is pure logic: no scheduler or mill-client imports.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..lifecycle.models import (
    ServiceState,
)
from .models import CaretakerFinding, FindingKind

if TYPE_CHECKING:
    from ..lifecycle.backends import ExecutionBackend
    from ..lifecycle.models import ComponentInspect, ServiceRecord
    from ..lifecycle.store import ServiceStore
    from ..registry.config_store import ComponentConfigStore
    from ..registry.loader import ComponentRegistry

logger = logging.getLogger(__name__)


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
    """Flag any managed container (primary **or** sibling) that shows a
    crash-loop signal since the last scrape.

    This is the always-on crash-loop safety net: a component can start
    restarting repeatedly outside any deploy window (config drift, a
    dependency outage), and nothing else notices (2026-09-05 incident — a
    hexarchy crash loop ran ~1.5h on host ``bequiet`` unseen; 2026-10-07 — a
    ``memory-hindsight`` sibling crash-looped ~10,920 times over 5.5 days
    unseen). ``store.list_all()`` returns every managed record including
    siblings, so each pass covers them with no component_id filter.

    A container is treated as crash-looping when **any** of these hold — the
    three signals the original sibling-monitoring ticket asked for, so a
    point-in-time scrape that happens to catch the container momentarily
    ``running`` between restarts is not the only thing that can flag it:

    * **RestartCount grew** since the previous pass (``current > previous``) —
      the classic Docker restart-policy loop.
    * **State is ``restarting``** — the container is in Docker's restart
      backoff right now, which a pure RestartCount delta misses when the
      count was reset by a container recreate.
    * **State is ``exited`` with a non-zero exit code** — the container
      crashed and is down.

    A ``CRASH_LOOP`` finding is emitted naming the record and carrying the
    same last-crash-log excerpt a HEALTH finding carries, for the escalation
    consumer.

    ``previous_restart_counts`` is the scheduler-owned baseline, keyed by
    record name and mutated in place across passes:

    * First observation of a container records a baseline and only the
      count-growth signal is suppressed — so a stable component is never
      flagged on the growth signal across a normal scrape window (the
      ``restarting`` / ``exited``-non-zero signals still apply immediately,
      being point-in-time rather than delta-based).
    * Count growth emits a finding; the baseline is then advanced to
      ``current`` so a container that keeps crash-looping is re-flagged each
      interval it keeps restarting.
    * Containers that have gone away are dropped from the baseline, so a
      later redeploy re-baselines instead of comparing against a stale count.

    Runtime state is read straight from ``get_container_diagnostics``
    (``restart_count``, ``state``, ``exit_code``), which the multi-host
    backend routes to the owning host — so remote components (host
    ``bequiet``) are covered without gating on any cosmetic ``/diagnose``
    routing/label verdict field.
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

        state = str(diag.get("state", "") or "").lower()
        exit_code = int(diag.get("exit_code", 0) or 0)

        reasons = _crash_loop_reasons(
            previous=previous,
            current=current,
            state=state,
            exit_code=exit_code,
        )
        if not reasons:
            # First observation (baseline only), a stable/reset count, and no
            # restarting / exited-non-zero signal — not crash-looping.
            continue

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
                detail=await _crash_loop_finding_detail(backend, record, reasons),
                severity="error",
            )
        )

    # Forget baselines for containers no longer present, so a fresh redeploy
    # starts from a clean baseline rather than comparing against a stale count.
    for stale in set(previous_restart_counts) - seen:
        del previous_restart_counts[stale]

    return findings


def _crash_loop_reasons(
    *,
    previous: int | None,
    current: int,
    state: str,
    exit_code: int,
) -> list[str]:
    """Return the human-readable crash-loop signals that fired this pass.

    An empty list means the container is healthy. ``state`` is expected
    already lower-cased. The count-growth signal is delta-based (needs a
    prior baseline), while the ``restarting`` and ``exited``-non-zero signals
    are point-in-time and apply even on the first observation.
    """
    reasons: list[str] = []
    if previous is not None and current > previous:
        reasons.append(
            f"RestartCount grew from {previous} to {current} across one "
            f"caretaker interval"
        )
    if state == "restarting":
        reasons.append(
            "the container is in Docker's restart backoff (state=restarting)"
        )
    if state == "exited" and exit_code != 0:
        reasons.append(f"the container exited with a non-zero code ({exit_code})")
    return reasons


async def _crash_loop_finding_detail(
    backend: ExecutionBackend,
    record: ServiceRecord,
    reasons: list[str],
) -> str:
    """Build a CRASH_LOOP finding body, including recent container logs.

    Mirrors ``_health_finding_detail``: the crash-loop ``reasons`` name the
    symptom and the log tail carries the crash cause (a traceback or a
    repeated startup error), so the escalation consumer gets an actionable
    signal rather than a bare "restarting" verdict. Log capture is
    best-effort — a finding without logs is still worth emitting.
    """
    header = (
        "Crash-loop signal(s) detected — "
        + "; ".join(reasons)
        + " — the container is crash-looping."
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
