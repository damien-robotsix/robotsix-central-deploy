"""Tests for the execution backends."""

import pytest

from robotsix_central_deploy.lifecycle.backend import NoopBackend
from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState


class TestNoopBackend:
    @pytest.fixture
    def backend(self) -> NoopBackend:
        return NoopBackend()

    async def test_start_returns_running(self, backend: NoopBackend):
        rec = ServiceRecord(name="test", state=ServiceState.STOPPED)
        result = await backend.start(rec)
        assert result == ServiceState.RUNNING

    async def test_stop_returns_stopped(self, backend: NoopBackend):
        rec = ServiceRecord(name="test", state=ServiceState.RUNNING)
        result = await backend.stop(rec)
        assert result == ServiceState.STOPPED

    async def test_restart_returns_running(self, backend: NoopBackend):
        rec = ServiceRecord(name="test", state=ServiceState.RUNNING)
        result = await backend.restart(rec)
        assert result == ServiceState.RUNNING

    async def test_status_reflects_current(self, backend: NoopBackend):
        rec = ServiceRecord(name="test", state=ServiceState.STOPPED)
        assert await backend.status(rec) == ServiceState.STOPPED
        rec.state = ServiceState.RUNNING
        assert await backend.status(rec) == ServiceState.RUNNING
