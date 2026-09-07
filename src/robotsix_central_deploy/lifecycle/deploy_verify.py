"""Post-deploy health verification.

A container can boot, pass its startup healthcheck, and *then* fall into a
crash loop: Docker's restart policy recreates it again and again while nothing
in the deploy path ever looks back. On 2026-09-05 the caretaker auto-deployed a
new hexarchy image whose config schema had changed; the container hit a
pydantic ``extra_forbidden`` error and restarted 110 times over ~1.5h. The
deploy reported success and no fleet component noticed.

Health-at-a-single-instant is not enough. :func:`verify_post_deploy_health`
polls the container's runtime state and ``RestartCount`` for a window after the
deploy completes and calls the deploy **FAILED** when the container is
restarting, has exited/died, or its ``RestartCount`` grows during the window —
each the signature of a crash loop. On failure it captures the tail of the
container logs so the escalation path (a companion ticket) can carry the crash
excerpt.

It reads only :meth:`ExecutionBackend.get_container_diagnostics` — container
state, ``restart_count`` and ``health``. It deliberately never consults the
``/diagnose`` report's routing fields (``proxy_network_attached`` and the like):
those legitimately read false for ``host:`` remote components routed via the
Traefik file provider, so gating health on them would fail every remote deploy.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backends import ExecutionBackend
    from .models import ServiceRecord

logger = logging.getLogger(__name__)

#: Default post-deploy observation window. Long enough for Docker's restart
#: policy to recreate a crash-looping container at least once (the default
#: backoff starts at 100ms and grows), short enough not to stall a deploy job.
POST_DEPLOY_WINDOW_SECONDS = 120.0

#: Spacing between diagnostics polls within the window.
POST_DEPLOY_POLL_INTERVAL_SECONDS = 10.0

#: Lines of container log to attach to a failed-deploy signal — enough for a
#: traceback or a repeated startup error, short enough to stay in a ticket body.
CRASH_LOG_TAIL_LINES = 40

#: Docker container states that mean the container is not healthily running.
#: ``restarting`` is a crash loop in progress; ``exited``/``dead`` are a
#: container that gave up.
_FAILED_STATES = frozenset({"restarting", "exited", "dead"})


@dataclass
class DeployVerification:
    """Outcome of a post-deploy health poll.

    ``ok`` is the headline verdict. ``verified`` is False when the backend
    could not observe the container at all (e.g. the noop backend, or no
    Docker access) — such a deploy is *not* failed on absence of evidence, but
    callers can tell a clean pass from an unobserved one.
    """

    ok: bool
    verified: bool = True
    state: str = ""
    health: str = ""
    baseline_restart_count: int = 0
    restart_count: int = 0
    reason: str = ""
    crash_log: str = ""


async def verify_post_deploy_health(
    backend: ExecutionBackend,
    record: ServiceRecord,
    *,
    window_seconds: float = POST_DEPLOY_WINDOW_SECONDS,
    poll_interval: float = POST_DEPLOY_POLL_INTERVAL_SECONDS,
    log_tail_lines: int = CRASH_LOG_TAIL_LINES,
) -> DeployVerification:
    """Poll *record*'s container for *window_seconds* and judge the deploy.

    The deploy is FAILED as soon as any poll observes the container
    ``restarting``/``exited``/``dead``, a ``RestartCount`` above the value seen
    on the first poll, or a Docker healthcheck reporting ``unhealthy``. When no
    poll trips within the window the deploy passes.

    Never raises: a diagnostics or log-capture error is logged and treated as
    "could not verify" rather than a deploy failure — this check exists to add
    a failure signal, not to invent one.
    """
    # At least two samples so a growing RestartCount can actually be observed.
    polls = max(2, int(window_seconds / poll_interval) + 1)
    baseline: int | None = None
    last_state = ""
    last_health = ""
    last_restart = 0

    for i in range(polls):
        if i:
            await asyncio.sleep(poll_interval)
        try:
            diag = await backend.get_container_diagnostics(record)
        except Exception:  # noqa: BLE001 — verification must never raise
            logger.warning(
                "post-deploy verify %s: diagnostics poll failed",
                record.name,
                exc_info=True,
            )
            continue

        if not diag.get("exists"):
            # No container to observe (noop backend, or the backend has no
            # Docker access). Absence of evidence is not a failed deploy.
            return DeployVerification(
                ok=True,
                verified=False,
                reason="container diagnostics unavailable",
            )

        state = str(diag.get("state", ""))
        health = str(diag.get("health", ""))
        try:
            restart_count = int(diag.get("restart_count", 0) or 0)
        except (TypeError, ValueError):
            restart_count = 0

        if baseline is None:
            baseline = restart_count
        last_state, last_health, last_restart = state, health, restart_count

        reason = ""
        if state in _FAILED_STATES:
            reason = (
                f"container is '{state}' during the post-deploy window "
                f"(RestartCount={restart_count}) — crash loop"
            )
        elif restart_count > baseline:
            reason = (
                f"RestartCount grew {baseline}→{restart_count} during the "
                f"{int(window_seconds)}s post-deploy window — crash loop"
            )
        elif health == "unhealthy":
            reason = (
                f"container healthcheck reports 'unhealthy' during the "
                f"post-deploy window (RestartCount={restart_count})"
            )

        if reason:
            crash_log = ""
            try:
                crash_log = await backend.get_container_logs(
                    record, tail=log_tail_lines
                )
            except Exception:  # noqa: BLE001 — best-effort log capture
                logger.warning(
                    "post-deploy verify %s: could not capture crash logs",
                    record.name,
                    exc_info=True,
                )
            return DeployVerification(
                ok=False,
                verified=True,
                state=state,
                health=health,
                baseline_restart_count=baseline,
                restart_count=restart_count,
                reason=reason,
                crash_log=crash_log,
            )

    return DeployVerification(
        ok=True,
        verified=True,
        state=last_state,
        health=last_health,
        baseline_restart_count=baseline or 0,
        restart_count=last_restart,
    )
