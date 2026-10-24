"""Unit tests for post-deploy health verification."""

from __future__ import annotations

from typing import Any

import pytest

from robotsix_central_deploy.lifecycle.deploy_verify import (
    rollback_after_failed_deploy,
    verify_post_deploy_health,
)
from robotsix_central_deploy.lifecycle.models import (
    ComponentInspect,
    DeploySource,
    RollbackOutcome,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.models import ComponentConfig

# Fast poll settings so the tests don't actually sleep for a real window.
_FAST = {"window_seconds": 0.05, "poll_interval": 0.01}


class _FakeBackend:
    """Backend stub exposing only the two methods the verifier calls.

    ``diags`` is a list of diagnostics dicts returned one per poll (the last
    is repeated once exhausted), so a test can model a RestartCount that grows
    across the window.
    """

    def __init__(self, diags: list[dict[str, Any]], logs: str = "boom\n") -> None:
        self._diags = diags
        self._i = 0
        self._logs = logs
        self.logs_requested = False

    async def get_container_diagnostics(self, service: ServiceRecord) -> dict[str, Any]:
        diag = self._diags[min(self._i, len(self._diags) - 1)]
        self._i += 1
        return diag

    async def get_container_logs(self, service: ServiceRecord, tail: int = 200) -> str:
        self.logs_requested = True
        return self._logs


def _record() -> ServiceRecord:
    return ServiceRecord(name="hexarchy", container_name="hexarchy")


@pytest.mark.asyncio
async def test_healthy_deploy_passes_cleanly() -> None:
    """A running container with a steady RestartCount is not a failure."""
    backend = _FakeBackend(
        [{"exists": True, "state": "running", "restart_count": 3, "health": "healthy"}]
    )
    result = await verify_post_deploy_health(backend, _record(), **_FAST)  # type: ignore[arg-type]

    assert result.ok is True
    assert result.verified is True
    assert result.crash_log == ""
    assert backend.logs_requested is False


@pytest.mark.asyncio
async def test_restarting_container_is_failed_with_crash_log() -> None:
    """A container caught 'restarting' is a crash loop → FAILED + log excerpt."""
    backend = _FakeBackend(
        [{"exists": True, "state": "restarting", "restart_count": 7}],
        logs="pydantic.ValidationError: extra_forbidden bonus_influence\n",
    )
    result = await verify_post_deploy_health(backend, _record(), **_FAST)  # type: ignore[arg-type]

    assert result.ok is False
    assert "crash loop" in result.reason
    assert "extra_forbidden" in result.crash_log
    assert backend.logs_requested is True


@pytest.mark.asyncio
async def test_growing_restart_count_is_failed() -> None:
    """RestartCount climbing during the window is the 110-restart signature."""
    backend = _FakeBackend(
        [
            {"exists": True, "state": "running", "restart_count": 0},
            {"exists": True, "state": "running", "restart_count": 1},
            {"exists": True, "state": "running", "restart_count": 2},
        ],
        logs="crash\n",
    )
    result = await verify_post_deploy_health(backend, _record(), **_FAST)  # type: ignore[arg-type]

    assert result.ok is False
    assert "RestartCount grew" in result.reason
    assert result.crash_log == "crash\n"


@pytest.mark.asyncio
async def test_exited_container_is_failed() -> None:
    """A container that exited (not running) is a failed deploy."""
    backend = _FakeBackend([{"exists": True, "state": "exited", "restart_count": 1}])
    result = await verify_post_deploy_health(backend, _record(), **_FAST)  # type: ignore[arg-type]

    assert result.ok is False
    assert "exited" in result.reason


@pytest.mark.asyncio
async def test_unhealthy_healthcheck_is_failed() -> None:
    """A settled 'unhealthy' healthcheck fails verification."""
    backend = _FakeBackend(
        [
            {
                "exists": True,
                "state": "running",
                "restart_count": 0,
                "health": "unhealthy",
            }
        ]
    )
    result = await verify_post_deploy_health(backend, _record(), **_FAST)  # type: ignore[arg-type]

    assert result.ok is False
    assert "unhealthy" in result.reason
    assert backend.logs_requested


@pytest.mark.asyncio
async def test_transient_unhealthy_that_recovers_passes() -> None:
    """'unhealthy' on an early poll, healthy by the last poll → not a failure.

    chat 2026-10-11 (twice): a healthcheck probe timed out while the freshly
    booted service was busy, the caretaker judged the deploy a crash loop and
    left the record ``failed`` although the container stayed up for 30 h with
    ``RestartCount=0``.
    """
    unhealthy = {
        "exists": True,
        "state": "running",
        "restart_count": 0,
        "health": "unhealthy",
    }
    healthy = {**unhealthy, "health": "healthy"}
    backend = _FakeBackend([healthy, unhealthy, unhealthy, healthy])
    result = await verify_post_deploy_health(backend, _record(), **_FAST)  # type: ignore[arg-type]

    assert result.ok is True
    assert result.health == "healthy"
    assert not backend.logs_requested


@pytest.mark.asyncio
async def test_no_container_is_unverified_not_failed() -> None:
    """A backend with no container to observe must not invent a failure."""
    backend = _FakeBackend([{"exists": False}])
    result = await verify_post_deploy_health(backend, _record(), **_FAST)  # type: ignore[arg-type]

    assert result.ok is True
    assert result.verified is False


@pytest.mark.asyncio
async def test_diagnostics_error_never_raises() -> None:
    """A diagnostics exception is swallowed; absence of evidence is not failure."""

    class _Boom:
        async def get_container_diagnostics(self, service: ServiceRecord) -> Any:
            raise RuntimeError("docker unreachable")

        async def get_container_logs(
            self, service: ServiceRecord, tail: int = 200
        ) -> str:
            return ""

    result = await verify_post_deploy_health(_Boom(), _record(), **_FAST)  # type: ignore[arg-type]

    assert result.ok is True


# ---------------------------------------------------------------------------
# rollback_after_failed_deploy
# ---------------------------------------------------------------------------


class _RollbackBackend:
    """Backend stub exposing the methods rollback_after_failed_deploy calls."""

    def __init__(
        self,
        outcome: RollbackOutcome | None = None,
        rollback_error: Exception | None = None,
        inspect: ComponentInspect | None = None,
    ) -> None:
        self._outcome = outcome
        self._rollback_error = rollback_error
        self._inspect = inspect
        self.rollback_called = False

    async def rollback(self, service: ServiceRecord, config: Any) -> RollbackOutcome:
        self.rollback_called = True
        if self._rollback_error is not None:
            raise self._rollback_error
        assert self._outcome is not None
        return self._outcome

    async def status(self, service: ServiceRecord) -> ComponentInspect:
        if self._inspect is None:
            raise RuntimeError("no status")
        return self._inspect


class _FakeStore:
    def __init__(self) -> None:
        self.put_calls = 0

    async def put(self, record: ServiceRecord) -> None:
        self.put_calls += 1


class _FakeHistory:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    async def append(self, name: str, entry: Any) -> None:
        self.entries.append(entry)


def _rb_record() -> ServiceRecord:
    return ServiceRecord(
        name="hexarchy",
        image="ghcr.io/org/hexarchy:main",
        deployed_image_digest="sha256:bad",
        previous_image_digest="sha256:good",
    )


def _rb_config() -> ComponentConfig:
    return ComponentConfig(
        id="hexarchy", image="ghcr.io/org/hexarchy:main", container_name="hexarchy"
    )


@pytest.mark.asyncio
async def test_rollback_restores_prior_image_and_syncs_state() -> None:
    backend = _RollbackBackend(
        outcome=RollbackOutcome(
            deployed_digest="sha256:good", state=ServiceState.RUNNING
        ),
        inspect=ComponentInspect(state=ServiceState.RUNNING, health="healthy"),
    )
    store = _FakeStore()
    history = _FakeHistory()
    record = _rb_record()

    recovery = await rollback_after_failed_deploy(
        backend,
        record,
        _rb_config(),
        store,
        history,  # type: ignore[arg-type]
    )

    assert recovery.rolled_back is True
    assert recovery.rolled_back_to == "sha256:good"
    assert recovery.failed_digest == "sha256:bad"
    # Digests swapped and state/health synced with the live container.
    assert record.deployed_image_digest == "sha256:good"
    assert record.previous_image_digest == "sha256:bad"
    assert record.state == ServiceState.RUNNING
    assert record.health == "healthy"
    assert store.put_calls == 1
    # A ROLLBACK history entry was recorded.
    assert len(history.entries) == 1
    assert history.entries[0].source == DeploySource.ROLLBACK
    assert history.entries[0].digest == "sha256:good"


@pytest.mark.asyncio
async def test_rollback_without_prior_digest_reports_error() -> None:
    backend = _RollbackBackend()
    store = _FakeStore()
    history = _FakeHistory()
    record = _rb_record()
    record.previous_image_digest = ""

    recovery = await rollback_after_failed_deploy(
        backend,
        record,
        _rb_config(),
        store,
        history,  # type: ignore[arg-type]
    )

    assert recovery.rolled_back is False
    assert "no prior image digest" in recovery.error
    assert backend.rollback_called is False


@pytest.mark.asyncio
async def test_rollback_backend_failure_reports_error() -> None:
    backend = _RollbackBackend(rollback_error=RuntimeError("boom"))
    store = _FakeStore()
    history = _FakeHistory()
    record = _rb_record()

    recovery = await rollback_after_failed_deploy(
        backend,
        record,
        _rb_config(),
        store,
        history,  # type: ignore[arg-type]
    )

    assert recovery.rolled_back is False
    assert "boom" in recovery.error
    # The original failed digest is still the deployed one — not corrupted.
    assert record.deployed_image_digest == "sha256:bad"
