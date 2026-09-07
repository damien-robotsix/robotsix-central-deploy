"""Unit tests for post-deploy health verification."""

from __future__ import annotations

from typing import Any

import pytest

from robotsix_central_deploy.lifecycle.deploy_verify import (
    verify_post_deploy_health,
)
from robotsix_central_deploy.lifecycle.models import ServiceRecord

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

    async def get_container_diagnostics(
        self, service: ServiceRecord
    ) -> dict[str, Any]:
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
        [{"exists": True, "state": "running", "restart_count": 0, "health": "unhealthy"}]
    )
    result = await verify_post_deploy_health(backend, _record(), **_FAST)  # type: ignore[arg-type]

    assert result.ok is False
    assert "unhealthy" in result.reason


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
