"""Tests for the execution backends."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from robotsix_central_deploy.lifecycle.backend import DockerSdkBackend, NoopBackend
from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState
from robotsix_central_deploy.registry.models import ComponentConfig, PortMapping, ServiceConfig


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


# ---------------------------------------------------------------------------
# Docker SDK backend — running_digest (image RepoDigests)
# ---------------------------------------------------------------------------


class TestDockerSdkBackendRunningDigest:
    @pytest.fixture
    def client_mock(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def backend(self, client_mock: MagicMock):
        docker_mock = MagicMock()
        docker_mock.DockerClient = MagicMock(return_value=client_mock)
        docker_mock.errors.NotFound = type("NotFound", (Exception,), {})
        docker_mock.errors.APIError = type("APIError", (Exception,), {})
        docker_mock.errors.ImageNotFound = type("ImageNotFound", (Exception,), {})
        with patch.dict(sys.modules, {"docker": docker_mock}):
            b = DockerSdkBackend()
            yield b, client_mock

    def _make_mock_container(self, image_id, repo_digests):
        container = MagicMock()
        container.attrs = {
            "State": {"Status": "running"},
            "Image": image_id,
        }
        container.image = MagicMock()
        container.image.labels = {}
        image_mock = MagicMock()
        image_mock.attrs = {"RepoDigests": repo_digests}
        return container, image_mock

    async def test_running_digest_from_repo_digests(self, backend):
        b, client = backend
        image_id = "sha256:deadbeef"
        repo_digests = ["ghcr.io/owner/img@sha256:e9f02675cf8a7c09"]
        container, image_mock = self._make_mock_container(image_id, repo_digests)
        client.containers.get.return_value = container
        client.images.get.return_value = image_mock

        record = ServiceRecord(name="cost-monitor", image="ghcr.io/owner/img:main")
        result = await b.status(record)
        assert result.running_digest == "sha256:e9f02675cf8a7c09"

    async def test_running_digest_empty_when_image_get_raises(self, backend):
        import docker

        b, client = backend
        image_id = "sha256:deadbeef"
        container, _ = self._make_mock_container(image_id, [])
        client.containers.get.return_value = container
        client.images.get.side_effect = docker.errors.ImageNotFound("no img")

        record = ServiceRecord(name="cost-monitor", image="ghcr.io/owner/img:main")
        result = await b.status(record)
        assert result.running_digest == ""

    async def test_running_digest_fallback_when_no_matching_prefix(self, backend):
        b, client = backend
        image_id = "sha256:deadbeef"
        repo_digests = ["ghcr.io/other/img@sha256:abc123"]
        container, image_mock = self._make_mock_container(image_id, repo_digests)
        client.containers.get.return_value = container
        client.images.get.return_value = image_mock

        record = ServiceRecord(name="cost-monitor", image="ghcr.io/owner/img:main")
        result = await b.status(record)
        assert result.running_digest == "sha256:abc123"


# ---------------------------------------------------------------------------
# Docker SDK backend — host-port publishing (ports={})
# ---------------------------------------------------------------------------


class TestDockerSdkBackendNoHostPorts:
    """_create_container must never publish host ports — the gateway routes
    via the Docker bridge network."""

    @pytest.fixture
    def client_mock(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def backend(self, client_mock: MagicMock):
        docker_mock = MagicMock()
        docker_mock.DockerClient = MagicMock(return_value=client_mock)
        docker_mock.errors.NotFound = type("NotFound", (Exception,), {})
        docker_mock.errors.APIError = type("APIError", (Exception,), {})
        with patch.dict(sys.modules, {"docker": docker_mock}):
            b = DockerSdkBackend()
            yield b, client_mock

    def test_create_container_does_not_publish_host_ports(self, backend):
        """_create_container passes ports={} regardless of config.ports."""
        b, client = backend
        config = ComponentConfig(
            id="test-svc",
            image="test:latest",
            container_name="test-svc",
            ports=[PortMapping(host=8080, container=8080)],
        )
        b._create_container(config, "test:latest")
        _, kwargs = client.containers.create.call_args
        assert kwargs["ports"] == {}

    def test_sibling_deploy_passes_empty_ports(self, backend):
        """Sibling-shaped ComponentConfig also gets ports={}."""
        b, client = backend
        config = ComponentConfig(
            id="test-svc",
            image="test:latest",
            container_name="test-svc",
            ports=[PortMapping(host=9000, container=9000)],
            siblings=[
                ServiceConfig(
                    service_key="worker",
                    container_name="mail-worker",
                    image="x:latest",
                    ports=[PortMapping(host=9000, container=9000)],
                )
            ],
        )
        b._create_container(config, "test:latest")
        _, kwargs = client.containers.create.call_args
        assert kwargs["ports"] == {}

    async def test_deploy_succeeds_when_host_port_already_bound(self, backend):
        """deploy() succeeds because _create_container passes ports={},
        so Docker never attempts a host-port bind that could conflict."""
        import docker

        b, client = backend
        client.containers.get.side_effect = docker.errors.NotFound("gone")
        mock_image = MagicMock()
        mock_image.attrs = {"RepoDigests": ["ghcr.io/o/img@sha256:abc"]}
        client.images.pull.return_value = mock_image
        mock_container = MagicMock()
        client.containers.create.return_value = mock_container

        config = ComponentConfig(
            id="test-svc",
            image="test:latest",
            container_name="test-svc",
            ports=[PortMapping(host=8080, container=8080)],
        )
        record = ServiceRecord(name="test-svc", container_name="test-svc", state=ServiceState.STOPPED)

        outcome = await b.deploy(record, config, "test:latest")
        assert outcome.state == ServiceState.RUNNING
        client.containers.create.assert_called_once()
        _, kwargs = client.containers.create.call_args
        assert kwargs.get("ports") == {} or "ports" not in kwargs
