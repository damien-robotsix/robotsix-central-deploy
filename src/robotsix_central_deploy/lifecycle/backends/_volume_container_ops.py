"""One-shot busybox helper-container mechanics for the Docker SDK backend.

Mixed into :class:`~robotsix_central_deploy.lifecycle.backends._volume_ops.VolumeOps`;
these methods run the detached poll-wait / forced-removal lifecycle shared by
every volume helper container and sweep helpers orphaned by a dead
predecessor.  They rely on ``self._client`` (the shared Docker client) provided
by the composing ``VolumeOps`` instance.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class ContainerHelperOpsMixin:
    """One-shot helper-container mechanics for :class:`VolumeOps`.

    Uses ``self._client`` (the Docker client shared with the owning
    ``DockerSdkBackend``) supplied by the composing ``VolumeOps`` instance.
    """

    _client: Any

    # -- volume inspection helpers ------------------------------------------

    #: Hard deadline for a one-shot du helper.  A du that runs longer is
    #: killed — better a missing size than a stray container grinding IO.
    _ONE_SHOT_TIMEOUT_S = 600

    #: Dedicated, longer deadline for the whole-volume measure.  The generic
    #: one-shot deadline (600s) is too tight for the largest managed volume
    #: (tens of thousands of files): the du helper was dying/timing out on it
    #: every hourly scan ("Response ended prematurely", 2026-09-02) and the
    #: scan silently recorded 0 bytes for that volume.
    _MEASURE_TIMEOUT_S = 1800

    #: Retries for the whole-volume measure — Docker API errors ONLY.  A
    #: Docker API stream cut under host IO pressure ("Response ended
    #: prematurely") is transient — retry rather than surfacing a failed
    #: measurement on the first hiccup.  A deadline :class:`TimeoutError` is
    #: never retried: it is deterministic, not transient — a du that did not
    #: finish in ``_MEASURE_TIMEOUT_S`` will not finish on an immediate
    #: re-run of the same volume on the same loaded host.  Retrying it
    #: tripled the IO grind on 2026-09-07 (mill-mill-data: attempts 1/3 and
    #: 2/3 each timed out at 1800s, 14:23Z/15:23Z/15:53Z/19:49Z), competing
    #: with mill's implement sandboxes for the host's disks.
    _MEASURE_ATTEMPTS = 3
    _MEASURE_RETRY_DELAY_S = 5

    #: Label stamped on every one-shot du helper so a fresh server process can
    #: sweep helpers orphaned by a dead predecessor.  The finally-remove in
    #: ``_run_one_shot_sync`` only protects the process that spawned the
    #: helper: a self-update replaces the container mid-wait, and the helper
    #: keeps running du with nobody left to remove it (2026-09-02: the old
    #: server's hourly audit du of mill-mill-data outlived it by 10+ minutes).
    HELPER_LABEL = "robotsix-central-deploy.one-shot-helper"

    #: Poll cadence for waiting on helper containers.  A single blocking
    #: ``/containers/{id}/wait`` call stays idle for the helper's whole
    #: runtime, and the docker socket-proxy (haproxy, ``timeout client/server
    #: 10m``) cuts any idle stream at 600s — every mill-mill-data measure
    #: attempt died at ~600s with "Response ended prematurely" despite the
    #: 1800s deadline (2026-09-04).  Waiting by short inspect polls keeps
    #: every request sub-second, so no idle timeout can trigger.
    _WAIT_POLL_START_S = 0.25
    _WAIT_POLL_MAX_S = 5.0
    _WAIT_POLL_MAX_CONSECUTIVE_ERRORS = 5

    def _wait_container_exit_sync(self, container: Any, timeout_s: float) -> int:
        """Wait for *container* to exit; return its exit code.

        Polls short ``inspect`` calls instead of one long-idle ``wait()``
        so the socket-proxy's 10-minute idle timeout can never cut the
        wait.  Tolerates a few consecutive transient poll errors (daemon
        under IO pressure).  Raises :class:`TimeoutError` when *timeout_s*
        elapses first; the container is NOT removed here — callers own
        cleanup on every path.

        Runs synchronously — callers must wrap in an executor.
        """
        deadline = time.monotonic() + timeout_s
        delay = self._WAIT_POLL_START_S
        errors = 0
        while True:
            try:
                container.reload()
                errors = 0
                if container.status in ("exited", "dead"):
                    state = container.attrs.get("State") or {}
                    return int(state.get("ExitCode") or 0)
            except Exception as exc:
                errors += 1
                if errors >= self._WAIT_POLL_MAX_CONSECUTIVE_ERRORS:
                    raise
                logger.warning(
                    "helper wait poll failed (%d/%d), retrying: %s",
                    errors,
                    self._WAIT_POLL_MAX_CONSECUTIVE_ERRORS,
                    exc,
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"helper container still running after {timeout_s:.0f}s"
                )
            time.sleep(delay)
            delay = min(delay * 2, self._WAIT_POLL_MAX_S)

    async def remove_stale_helpers(self) -> int:
        """Force-remove every labelled one-shot helper container.

        Called at server startup: any helper alive then belongs to a dead
        (or dying, mid-self-update) predecessor process.  Returns the number
        of containers removed; never raises.
        """
        loop = asyncio.get_running_loop()

        def _sweep() -> int:
            removed = 0
            try:
                stale = self._client.containers.list(
                    all=True, filters={"label": self.HELPER_LABEL}
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("stale-helper sweep: listing failed: %s", exc)
                return 0
            for container in stale:
                try:
                    container.remove(force=True)
                    removed += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "stale-helper sweep: failed to remove %s: %s",
                        getattr(container, "name", "?"),
                        exc,
                    )
            if removed:
                logger.info(
                    "stale-helper sweep: removed %d orphaned helper(s)", removed
                )
            return removed

        return await loop.run_in_executor(None, _sweep)

    def _run_one_shot_sync(
        self,
        command: list[str],
        volumes: dict[str, dict[str, str]],
        timeout_s: int | None = None,
    ) -> bytes:
        """Run a one-shot busybox helper detached and ALWAYS remove it.

        docker-py's non-detached ``run(remove=True)`` performs the removal
        client-side after the attach stream ends — when that stream breaks
        (daemon under IO pressure, "Response ended prematurely") the call
        raises and the helper container is orphaned, still running its du.
        Two such orphans ground mill-mill-data for 40+ minutes on
        2026-09-02.  Detach + poll-wait + finally-remove(force) kills the
        helper on every exit path.

        Runs synchronously — callers must wrap in an executor.
        """
        container = self._client.containers.run(
            "busybox",
            command=command,
            volumes=volumes,
            detach=True,
            labels={self.HELPER_LABEL: "1"},
        )
        try:
            self._wait_container_exit_sync(
                container, timeout_s or self._ONE_SHOT_TIMEOUT_S
            )
            raw = container.logs(stdout=True, stderr=False)
            return raw if isinstance(raw, bytes) else str(raw).encode()
        finally:
            try:
                container.remove(force=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "one-shot helper cleanup failed (container may leak): %s", exc
                )
