"""Tests for the execution backends."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from robotsix_central_deploy.lifecycle.backend import DockerSdkBackend, NoopBackend
from robotsix_central_deploy.lifecycle.models import ComponentInspect, ServiceRecord, ServiceState


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
        result = await backend.status(rec)
        assert result.state == ServiceState.STOPPED
        rec.state = ServiceState.RUNNING
        result = await backend.status(rec)
        assert result.state == ServiceState.RUNNING


# ---------------------------------------------------------------------------
# Docker SDK backend tests (fully mocked — no live daemon required)
# ---------------------------------------------------------------------------


class TestDockerSdkBackend:
    @staticmethod
    def _make_container(
        status: str = "running",
        revision: str = "abc123",
        health_status: str = "healthy",
    ) -> MagicMock:
        container = MagicMock()
        container.attrs = {
            "State": {
                "Status": status,
                "Health": {"Status": health_status},
            }
        }
        container.image.labels = {
            "org.opencontainers.image.revision": revision,
        }
        return container

    @pytest.fixture
    def client_mock(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def backend(self, client_mock: MagicMock):
        # Mock the docker module in sys.modules before constructing
        # DockerSdkBackend, so its lazy ``import docker`` resolves.
        docker_mock = MagicMock()
        docker_mock.DockerClient = MagicMock(return_value=client_mock)
        docker_mock.errors.NotFound = type("NotFound", (Exception,), {})
        docker_mock.errors.APIError = type("APIError", (Exception,), {})
        with patch.dict(sys.modules, {"docker": docker_mock}):
            b = DockerSdkBackend()
            yield b, client_mock

    async def test_status_running_with_revision_and_health(self, backend):
        b, client = backend
        client.containers.get.return_value = self._make_container()
        record = ServiceRecord(name="cost-monitor", container_name="cost-monitor")
        result = await b.status(record)
        assert result.state == ServiceState.RUNNING
        assert result.image_revision == "abc123"
        assert result.health == "healthy"

    async def test_status_not_found_returns_unknown(self, backend):
        import docker

        b, client = backend
        client.containers.get.side_effect = docker.errors.NotFound("nope")
        record = ServiceRecord(name="missing", container_name="missing")
        result = await b.status(record)
        assert result.state == ServiceState.UNKNOWN

    async def test_status_no_health_check(self, backend):
        b, client = backend
        container = self._make_container()
        del container.attrs["State"]["Health"]  # simulate no health check
        client.containers.get.return_value = container
        record = ServiceRecord(name="x", container_name="x")
        result = await b.status(record)
        assert result.health == ""

    async def test_start_success(self, backend):
        b, client = backend
        container = MagicMock()
        client.containers.get.return_value = container
        record = ServiceRecord(name="cost-monitor", container_name="cost-monitor")
        state = await b.start(record)
        container.start.assert_called_once()
        assert state == ServiceState.RUNNING

    async def test_start_not_found_returns_failed(self, backend):
        import docker

        b, client = backend
        client.containers.get.side_effect = docker.errors.NotFound("gone")
        record = ServiceRecord(name="x", container_name="x")
        assert await b.start(record) == ServiceState.FAILED

    async def test_stop_success(self, backend):
        b, client = backend
        container = MagicMock()
        client.containers.get.return_value = container
        record = ServiceRecord(name="x", container_name="x")
        state = await b.stop(record)
        container.stop.assert_called_once()
        assert state == ServiceState.STOPPED

    async def test_stop_not_found_treated_as_stopped(self, backend):
        import docker

        b, client = backend
        client.containers.get.side_effect = docker.errors.NotFound("gone")
        record = ServiceRecord(name="x", container_name="x")
        assert await b.stop(record) == ServiceState.STOPPED

    async def test_restart_success(self, backend):
        b, client = backend
        container = MagicMock()
        client.containers.get.return_value = container
        record = ServiceRecord(name="x", container_name="x")
        state = await b.restart(record)
        container.restart.assert_called_once()
        assert state == ServiceState.RUNNING

    @pytest.mark.parametrize(
        "docker_status,expected",
        [
            ("running", ServiceState.RUNNING),
            ("exited", ServiceState.STOPPED),
            ("created", ServiceState.STOPPED),
            ("restarting", ServiceState.RESTARTING),
            ("dead", ServiceState.FAILED),
            ("removing", ServiceState.STOPPING),
            ("paused", ServiceState.RUNNING),
        ],
    )
    async def test_status_state_mapping(self, backend, docker_status, expected):
        b, client = backend
        container = self._make_container(status=docker_status)
        client.containers.get.return_value = container
        record = ServiceRecord(name="x", container_name="x")
        result = await b.status(record)
        assert result.state == expected
