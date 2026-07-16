"""Dedicated tests for lifecycle/backends/docker_sdk.py — DockerSdkBackend.

All tests mock the ``docker`` module so no live Docker daemon is required.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from robotsix_central_deploy.lifecycle.backends.base import ExecutionBackend
from robotsix_central_deploy.lifecycle.backends.docker_sdk import DockerSdkBackend
from robotsix_central_deploy.lifecycle.models import (
    DeployOutcome,
    DockerDfStats,
    RollbackOutcome,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    HealthCheck,
)


# ---------------------------------------------------------------------------
# Shared helper — create a mock backend with a patched docker module
# ---------------------------------------------------------------------------


def _make_docker_mock():
    """Return a mock ``docker`` module suitable for ``sys.modules`` patching."""
    dm = MagicMock()
    dm.DockerClient = MagicMock()

    # Create fake exception types with attributes matching real docker.errors.
    class _FakeAPIError(Exception):
        def __init__(self, message: str, status_code: int = 500):
            super().__init__(message)
            self.status_code = status_code
            self.explanation = message

    _NotFound = type("NotFound", (Exception,), {})
    _ContainerError = type("ContainerError", (Exception,), {})
    _ImageNotFound = type("ImageNotFound", (Exception,), {})
    _DockerException = type("DockerException", (Exception,), {})

    dm.errors.NotFound = _NotFound
    dm.errors.APIError = _FakeAPIError
    dm.errors.ContainerError = _ContainerError
    dm.errors.ImageNotFound = _ImageNotFound
    dm.errors.DockerException = _DockerException
    return dm


def _make_api_error(message: str, status_code: int = 500) -> Exception:
    """Build a realistic fake ``docker.errors.APIError`` instance."""
    dm = _make_docker_mock()
    return dm.errors.APIError(message, status_code=status_code)


# ---------------------------------------------------------------------------
# Lifecycle edge cases — APIError during _get_container and container ops
# ---------------------------------------------------------------------------


class TestDockerSdkBackendLifecycleEdgeCases:
    """start/stop/restart error paths not covered by test_backend.py."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_start_daemon_unreachable_returns_failed(self, backend):
        """When _get_container raises APIError, start returns FAILED."""
        b, client, dm = backend
        client.containers.get.side_effect = dm.errors.APIError("daemon down")
        record = ServiceRecord(name="x", container_name="x")
        assert await b.start(record) == ServiceState.FAILED

    async def test_start_container_start_raises_api_error_returns_failed(self, backend):
        """When container.start() raises APIError, start returns FAILED."""
        b, client, dm = backend
        container = MagicMock()
        container.start.side_effect = dm.errors.APIError("port conflict")
        client.containers.get.return_value = container
        record = ServiceRecord(name="x", container_name="x")
        assert await b.start(record) == ServiceState.FAILED

    async def test_stop_daemon_unreachable_returns_failed(self, backend):
        """When _get_container raises APIError, stop returns FAILED."""
        b, client, dm = backend
        client.containers.get.side_effect = dm.errors.APIError("daemon down")
        record = ServiceRecord(name="x", container_name="x")
        assert await b.stop(record) == ServiceState.FAILED

    async def test_stop_container_stop_raises_api_error_returns_failed(self, backend):
        """When container.stop() raises APIError, stop returns FAILED."""
        b, client, dm = backend
        container = MagicMock()
        container.stop.side_effect = dm.errors.APIError("permission denied")
        client.containers.get.return_value = container
        record = ServiceRecord(name="x", container_name="x")
        assert await b.stop(record) == ServiceState.FAILED

    async def test_restart_daemon_unreachable_returns_failed(self, backend):
        """When _get_container raises APIError, restart returns FAILED."""
        b, client, dm = backend
        client.containers.get.side_effect = dm.errors.APIError("daemon down")
        record = ServiceRecord(name="x", container_name="x")
        assert await b.restart(record) == ServiceState.FAILED

    async def test_restart_container_restart_raises_api_error_returns_failed(
        self, backend
    ):
        """When container.restart() raises APIError, restart returns FAILED."""
        b, client, dm = backend
        container = MagicMock()
        container.restart.side_effect = dm.errors.APIError("restart failed")
        client.containers.get.return_value = container
        record = ServiceRecord(name="x", container_name="x")
        assert await b.restart(record) == ServiceState.FAILED

    async def test_status_daemon_unreachable_returns_unknown(self, backend):
        """When _get_container raises APIError, status returns UNKNOWN."""
        b, client, dm = backend
        client.containers.get.side_effect = dm.errors.APIError("daemon down")
        record = ServiceRecord(name="x", container_name="x")
        result = await b.status(record)
        assert result.state == ServiceState.UNKNOWN

    async def test_status_container_none_returns_unknown(self, backend):
        """When _get_container returns None, status returns UNKNOWN."""
        b, client, dm = backend
        client.containers.get.side_effect = dm.errors.NotFound("gone")
        record = ServiceRecord(name="x", container_name="x")
        result = await b.status(record)
        assert result.state == ServiceState.UNKNOWN

    async def test_start_container_none_returns_failed(self, backend):
        """When _get_container returns None (NotFound), start returns FAILED."""
        b, client, dm = backend
        client.containers.get.side_effect = dm.errors.NotFound("gone")
        record = ServiceRecord(name="x", container_name="x")
        assert await b.start(record) == ServiceState.FAILED

    async def test_stop_container_none_returns_stopped(self, backend):
        """When _get_container returns None, stop returns STOPPED (idempotent)."""
        b, client, dm = backend
        client.containers.get.side_effect = dm.errors.NotFound("gone")
        record = ServiceRecord(name="x", container_name="x")
        assert await b.stop(record) == ServiceState.STOPPED

    async def test_restart_container_none_returns_failed(self, backend):
        """When _get_container returns None, restart returns FAILED."""
        b, client, dm = backend
        client.containers.get.side_effect = dm.errors.NotFound("gone")
        record = ServiceRecord(name="x", container_name="x")
        assert await b.restart(record) == ServiceState.FAILED


# ---------------------------------------------------------------------------
# _get_container helper
# ---------------------------------------------------------------------------


class TestDockerSdkBackendGetContainer:
    """Tests for the _get_container helper."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_get_container_returns_container_on_success(self, backend):
        b, client, dm = backend
        container = MagicMock()
        client.containers.get.return_value = container
        result = await b._get_container("test-svc")
        assert result is container

    async def test_get_container_returns_none_on_not_found(self, backend):
        b, client, dm = backend
        client.containers.get.side_effect = dm.errors.NotFound("nope")
        result = await b._get_container("missing")
        assert result is None

    async def test_get_container_reraises_api_error(self, backend):
        b, client, dm = backend
        client.containers.get.side_effect = dm.errors.APIError("boom")
        with pytest.raises(dm.errors.APIError):
            await b._get_container("broken")


# ---------------------------------------------------------------------------
# _container_name helper
# ---------------------------------------------------------------------------


class TestDockerSdkBackendContainerName:
    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        dm.DockerClient.return_value = MagicMock()
        with patch.dict(sys.modules, {"docker": dm}):
            return DockerSdkBackend()

    def test_container_name_prefers_container_name(self, backend):
        record = ServiceRecord(name="svc", container_name="custom")
        assert backend._container_name(record) == "custom"

    def test_container_name_falls_back_to_name(self, backend):
        record = ServiceRecord(name="svc", container_name="")
        assert backend._container_name(record) == "svc"


# ---------------------------------------------------------------------------
# _state_from_docker helper
# ---------------------------------------------------------------------------


class TestDockerSdkBackendStateFromDocker:
    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        dm.DockerClient.return_value = MagicMock()
        with patch.dict(sys.modules, {"docker": dm}):
            return DockerSdkBackend()

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("running", ServiceState.RUNNING),
            ("paused", ServiceState.RUNNING),
            ("restarting", ServiceState.RESTARTING),
            ("created", ServiceState.STOPPED),
            ("exited", ServiceState.STOPPED),
            ("dead", ServiceState.FAILED),
            ("removing", ServiceState.STOPPING),
            ("bogus", ServiceState.UNKNOWN),
        ],
    )
    def test_state_from_docker_mapping(self, backend, status, expected):
        assert backend._state_from_docker(status) == expected


# ---------------------------------------------------------------------------
# Deploy flow — full deploy with pull, volume prep, container create/start
# ---------------------------------------------------------------------------


class TestDockerSdkBackendDeploy:
    """Full deploy flow tests."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    @staticmethod
    def _make_config(**overrides) -> ComponentConfig:
        defaults: dict = {
            "id": "test-svc",
            "image": "ghcr.io/o/img:main",
            "container_name": "test-svc",
        }
        defaults.update(overrides)
        return ComponentConfig(**defaults)

    @staticmethod
    def _make_image_mock(
        repo_digests: list[str] | None = None, image_id: str = "sha256:newid"
    ):
        img = MagicMock()
        img.attrs = {"RepoDigests": repo_digests or []}
        img.id = image_id
        return img

    async def test_deploy_happy_path_no_health_check(self, backend):
        """Deploy pulls image, removes old container, creates + starts new."""
        b, client, dm = backend
        config = self._make_config()
        record = ServiceRecord(
            name="test-svc", container_name="test-svc", state=ServiceState.STOPPED
        )

        # Image pull succeeds
        image = self._make_image_mock(
            ["ghcr.io/o/img@sha256:e9f02675cf8a7c09"], "sha256:newid"
        )
        client.images.pull.return_value = image

        # Old container present
        old_container = MagicMock()
        old_container.image.id = "sha256:oldid"
        client.containers.get.return_value = old_container

        # New container create + start
        new_container = MagicMock()
        client.containers.create.return_value = new_container

        outcome = await b.deploy(record, config, "ghcr.io/o/img:main")

        assert isinstance(outcome, DeployOutcome)
        assert outcome.state == ServiceState.RUNNING
        assert outcome.deployed_digest == "sha256:e9f02675cf8a7c09"
        assert outcome.previous_digest == "sha256:oldid"

        # Old container was stopped + removed
        old_container.stop.assert_called_once()
        old_container.remove.assert_called_once_with(force=True)

        # New container created with correct image_ref
        client.containers.create.assert_called_once()
        _, kwargs = client.containers.create.call_args
        assert kwargs["image"] == "ghcr.io/o/img:main"

        # New container started
        new_container.start.assert_called_once()

    async def test_deploy_no_previous_container(self, backend):
        """Deploy when no old container exists (first deploy)."""
        b, client, dm = backend
        config = self._make_config()
        record = ServiceRecord(
            name="test-svc", container_name="test-svc", state=ServiceState.UNKNOWN
        )

        image = self._make_image_mock(["ghcr.io/o/img@sha256:abc123"], "sha256:newid")
        client.images.pull.return_value = image
        # No old container
        client.containers.get.side_effect = dm.errors.NotFound("gone")

        new_container = MagicMock()
        client.containers.create.return_value = new_container

        outcome = await b.deploy(record, config, "ghcr.io/o/img:main")

        assert outcome.state == ServiceState.RUNNING
        assert outcome.previous_digest == ""
        assert outcome.deployed_digest == "sha256:abc123"
        new_container.start.assert_called_once()

    async def test_deploy_digest_falls_back_to_image_id(self, backend):
        """When RepoDigests are empty, digest falls back to image.id."""
        b, client, dm = backend
        config = self._make_config()
        record = ServiceRecord(
            name="test-svc", container_name="test-svc", state=ServiceState.STOPPED
        )

        image = self._make_image_mock([], "sha256:backup-digest")
        client.images.pull.return_value = image
        client.containers.get.side_effect = dm.errors.NotFound("gone")
        client.containers.create.return_value = MagicMock()

        outcome = await b.deploy(record, config, "ghcr.io/o/img:main")
        assert outcome.deployed_digest == "sha256:backup-digest"

    async def test_deploy_with_health_check(self, backend):
        """Deploy with health_check configured waits for healthy."""
        b, client, dm = backend
        config = self._make_config(
            health_check=HealthCheck(
                test=["CMD", "check"],
                interval_seconds=10,
                timeout_seconds=5,
                retries=3,
                start_period_seconds=0,
            )
        )
        record = ServiceRecord(
            name="test-svc", container_name="test-svc", state=ServiceState.STOPPED
        )

        image = self._make_image_mock(["ghcr.io/o/img@sha256:abc"], "sha256:new")
        client.images.pull.return_value = image

        new_container = MagicMock()
        # Simulate health check: first reload has "starting", then "healthy"
        health_attrs = [{"Status": "starting"}, {"Status": "healthy"}]

        def _reload():
            new_container.attrs["State"]["Health"] = health_attrs.pop(0)

        new_container.reload.side_effect = _reload
        new_container.attrs = {"State": {"Health": health_attrs[0]}}
        client.containers.create.return_value = new_container

        # _get_container is called first to check for old container (NotFound),
        # then called again during health polling (returns new_container).
        # Use a side_effect list to sequence the responses.
        client.containers.get.side_effect = [
            dm.errors.NotFound("gone"),  # old container lookup
            new_container,  # first health poll
            new_container,  # second health poll → healthy
        ]

        outcome = await b.deploy(record, config, "ghcr.io/o/img:main")

        assert outcome.state == ServiceState.RUNNING
        new_container.start.assert_called_once()

    async def test_deploy_image_pull_fails_raises_runtime_error(self, backend):
        """When image pull fails, RuntimeError is raised."""
        b, client, dm = backend
        config = self._make_config()
        record = ServiceRecord(name="test-svc", container_name="test-svc")

        client.images.pull.side_effect = dm.errors.APIError("pull failed")

        with pytest.raises(RuntimeError, match="Image pull failed"):
            await b.deploy(record, config, "ghcr.io/o/img:main")

    async def test_deploy_create_start_fails_triggers_restore(self, backend):
        """When container create/start fails, _try_restore is called."""
        b, client, dm = backend
        config = self._make_config()
        record = ServiceRecord(
            name="test-svc", container_name="test-svc", state=ServiceState.STOPPED
        )

        image = self._make_image_mock(["ghcr.io/o/img@sha256:abc"], "sha256:new")
        client.images.pull.return_value = image

        # Old container present — provides prior_digest for restore
        old = MagicMock()
        old.image.id = "sha256:prior"
        client.containers.get.return_value = old

        # Container create fails
        client.containers.create.side_effect = RuntimeError("create boom")

        with pytest.raises(RuntimeError, match="create/start failed"):
            await b.deploy(record, config, "ghcr.io/o/img:main")

        # Verify restore was attempted: old container should not be lost
        # The restore creates a new container from prior digest
        client.containers.create.assert_called()
        # At least one call to create is for the restore
        assert client.containers.create.call_count >= 2

    async def test_deploy_pinned_digest_ref(self, backend):
        """Deploy with a pinned digest ref (image@sha256:...) resolves RepoDigests."""
        b, client, dm = backend
        config = self._make_config()
        record = ServiceRecord(
            name="test-svc", container_name="test-svc", state=ServiceState.STOPPED
        )

        image_ref = "ghcr.io/o/img@sha256:deadbeefcafe"
        image = self._make_image_mock(
            ["ghcr.io/o/img@sha256:deadbeefcafe"], "sha256:deadbeefcafe"
        )
        client.images.pull.return_value = image
        client.containers.get.side_effect = dm.errors.NotFound("gone")
        client.containers.create.return_value = MagicMock()

        outcome = await b.deploy(record, config, image_ref)
        assert outcome.deployed_digest == "sha256:deadbeefcafe"


# ---------------------------------------------------------------------------
# Rollback flow
# ---------------------------------------------------------------------------


class TestDockerSdkBackendRollback:
    """Rollback flow tests."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_rollback_happy_path(self, backend):
        """Rollback stops old, creates + starts from prior digest."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc", image="img:latest", container_name="test-svc"
        )
        record = ServiceRecord(
            name="test-svc",
            container_name="test-svc",
            state=ServiceState.RUNNING,
            previous_image_digest="sha256:rollback-target",
        )

        old = MagicMock()
        client.containers.get.return_value = old
        new_container = MagicMock()
        client.containers.create.return_value = new_container

        outcome = await b.rollback(record, config)

        assert isinstance(outcome, RollbackOutcome)
        assert outcome.state == ServiceState.RUNNING
        assert outcome.deployed_digest == "sha256:rollback-target"

        # Old container stopped + removed
        old.stop.assert_called_once()
        old.remove.assert_called_once_with(force=True)

        # New container created from prior digest
        _, kwargs = client.containers.create.call_args
        assert kwargs["image"] == "sha256:rollback-target"
        new_container.start.assert_called_once()

    async def test_rollback_no_existing_container(self, backend):
        """Rollback when no container exists (first deploy was removed)."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc", image="img:latest", container_name="test-svc"
        )
        record = ServiceRecord(
            name="test-svc",
            container_name="test-svc",
            state=ServiceState.UNKNOWN,
            previous_image_digest="sha256:rollback-target",
        )

        client.containers.get.side_effect = dm.errors.NotFound("gone")
        new_container = MagicMock()
        client.containers.create.return_value = new_container

        outcome = await b.rollback(record, config)
        assert outcome.state == ServiceState.RUNNING
        new_container.start.assert_called_once()

    async def test_rollback_stop_remove_fails_raises_runtime_error(self, backend):
        """When _stop_and_remove's remove(force=True) raises APIError,
        RuntimeError propagates from _remove_old_container."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc", image="img:latest", container_name="test-svc"
        )
        record = ServiceRecord(
            name="test-svc",
            container_name="test-svc",
            previous_image_digest="sha256:target",
        )

        old = MagicMock()
        # _stop_and_remove swallows exceptions from stop(), but an
        # APIError from remove(force=True) propagates to _remove_old_container.
        old.remove.side_effect = dm.errors.APIError("cannot remove")
        client.containers.get.return_value = old

        with pytest.raises(RuntimeError, match="Failed to remove container"):
            await b.rollback(record, config)

    async def test_rollback_create_start_fails_raises_runtime_error(self, backend):
        """When create/start fails during rollback, RuntimeError propagates."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc", image="img:latest", container_name="test-svc"
        )
        record = ServiceRecord(
            name="test-svc",
            container_name="test-svc",
            previous_image_digest="sha256:target",
        )

        client.containers.get.side_effect = dm.errors.NotFound("gone")
        client.containers.create.side_effect = RuntimeError("create boom")

        with pytest.raises(
            RuntimeError, match="Rollback container create/start failed"
        ):
            await b.rollback(record, config)

    async def test_rollback_with_health_check(self, backend):
        """Rollback with health_check waits for healthy."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc",
            image="img:latest",
            container_name="test-svc",
            health_check=HealthCheck(
                test=["CMD", "check"],
                interval_seconds=10,
                timeout_seconds=5,
                retries=3,
                start_period_seconds=0,
            ),
        )
        record = ServiceRecord(
            name="test-svc",
            container_name="test-svc",
            previous_image_digest="sha256:target",
        )

        new_container = MagicMock()
        new_container.attrs = {"State": {"Health": {"Status": "healthy"}}}
        client.containers.create.return_value = new_container

        # First get: old container lookup (NotFound)
        # Second get: health poll (returns new_container)
        client.containers.get.side_effect = [
            dm.errors.NotFound("gone"),
            new_container,
        ]

        outcome = await b.rollback(record, config)
        assert outcome.state == ServiceState.RUNNING


# ---------------------------------------------------------------------------
# _wait_healthy
# ---------------------------------------------------------------------------


class TestDockerSdkBackendWaitHealthy:
    """Tests for the _wait_healthy internal method."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_wait_healthy_returns_when_healthy(self, backend):
        """_wait_healthy returns immediately when health status is healthy."""
        b, client, dm = backend
        container = MagicMock()

        def _reload():
            container.attrs["State"]["Health"] = {"Status": "healthy"}

        container.reload.side_effect = _reload
        container.attrs = {"State": {"Health": {"Status": "starting"}}}
        client.containers.get.return_value = container

        # Should not raise — returns when healthy is reached
        await b._wait_healthy("test-svc", timeout=10.0)

    async def test_wait_healthy_raises_on_unhealthy(self, backend):
        """_wait_healthy raises RuntimeError when health goes unhealthy."""
        b, client, dm = backend
        container = MagicMock()
        container.attrs = {"State": {"Health": {"Status": "unhealthy"}}}
        client.containers.get.return_value = container

        with pytest.raises(RuntimeError, match="unhealthy after deploy"):
            await b._wait_healthy("test-svc", timeout=10.0)

    async def test_wait_healthy_no_healthcheck_treats_as_healthy(self, backend):
        """Container with no Health key is treated as healthy."""
        b, client, dm = backend
        container = MagicMock()
        container.attrs = {"State": {}}  # No Health key
        client.containers.get.return_value = container

        # Should not raise
        await b._wait_healthy("test-svc", timeout=10.0)

    async def test_wait_healthy_container_disappears_raises(self, backend):
        """_wait_healthy raises RuntimeError when container disappears."""
        b, client, dm = backend

        # First call returns container, second returns None (disappeared)
        container = MagicMock()
        container.attrs = {"State": {"Health": {"Status": "starting"}}}
        client.containers.get.side_effect = [container, None]

        with pytest.raises(RuntimeError, match="disappeared during health wait"):
            await b._wait_healthy("test-svc", timeout=10.0)

    async def test_wait_healthy_timeout_proceeds(self, backend, monkeypatch):
        """_wait_healthy logs warning and returns when timeout expires."""
        b, client, dm = backend

        container = MagicMock()
        container.attrs = {"State": {"Health": {"Status": "starting"}}}
        client.containers.get.return_value = container

        # Make the loop think time is way past deadline
        mock_loop = MagicMock()
        mock_loop.time.side_effect = [
            0.0,
            1000.0,
        ]  # first call = now, second = past deadline
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: mock_loop)

        # Should not raise — just proceeds after timeout
        await b._wait_healthy("test-svc", timeout=10.0)


# ---------------------------------------------------------------------------
# Log streaming
# ---------------------------------------------------------------------------


class TestDockerSdkBackendStreamLogs:
    """Tests for the stream_logs method."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_stream_logs_yields_chunks(self, backend):
        """stream_logs yields log chunks from the container."""
        b, client, dm = backend
        container = MagicMock()
        container.logs.return_value = iter([b"line1\n", b"line2\n", b"line3\n"])
        client.containers.get.return_value = container

        record = ServiceRecord(name="x", container_name="x")
        chunks = [chunk async for chunk in b.stream_logs(record, tail=50, follow=False)]
        assert len(chunks) == 3
        assert chunks[0] == b"line1\n"
        assert chunks[1] == b"line2\n"
        assert chunks[2] == b"line3\n"

    async def test_stream_logs_container_not_found(self, backend):
        """stream_logs yields info message when container not found."""
        b, client, dm = backend
        client.containers.get.side_effect = dm.errors.NotFound("gone")
        record = ServiceRecord(name="x", container_name="x")
        chunks = [chunk async for chunk in b.stream_logs(record)]
        assert len(chunks) == 1
        assert b"container not found" in chunks[0]

    async def test_stream_logs_docker_error(self, backend):
        """stream_logs yields error message on APIError from _get_container."""
        b, client, dm = backend
        client.containers.get.side_effect = dm.errors.APIError("daemon down")
        record = ServiceRecord(name="x", container_name="x")
        chunks = [chunk async for chunk in b.stream_logs(record)]
        assert len(chunks) == 1
        assert b"docker error" in chunks[0]

    async def test_stream_logs_with_since_parameter(self, backend):
        """stream_logs passes 'since' parameter to container.logs()."""
        b, client, dm = backend
        container = MagicMock()
        container.logs.return_value = iter([b"log\n"])
        client.containers.get.return_value = container
        record = ServiceRecord(name="x", container_name="x")

        chunks = [
            chunk
            async for chunk in b.stream_logs(
                record, tail=100, since="2024-01-01T00:00:00"
            )
        ]
        assert len(chunks) == 1
        _, kwargs = container.logs.call_args
        assert kwargs["since"] == "2024-01-01T00:00:00"
        assert kwargs["tail"] == 100
        assert kwargs["follow"] is False

    async def test_stream_logs_with_follow(self, backend):
        """stream_logs passes follow=True to container.logs()."""
        b, client, dm = backend
        container = MagicMock()
        container.logs.return_value = iter([b"log\n"])
        client.containers.get.return_value = container
        record = ServiceRecord(name="x", container_name="x")

        chunks = [chunk async for chunk in b.stream_logs(record, follow=True)]
        assert len(chunks) == 1
        _, kwargs = container.logs.call_args
        assert kwargs["follow"] is True

    async def test_stream_logs_api_error_during_iteration(self, backend):
        """stream_logs yields error chunk when container.logs raises APIError."""
        b, client, dm = backend
        container = MagicMock()
        container.logs.side_effect = dm.errors.APIError("log stream broken")
        client.containers.get.return_value = container
        record = ServiceRecord(name="x", container_name="x")

        chunks = [chunk async for chunk in b.stream_logs(record)]
        assert len(chunks) == 1
        assert b"docker error" in chunks[0]

    async def test_stream_logs_handles_non_bytes_chunks(self, backend):
        """Non-bytes chunks are encoded to bytes."""
        b, client, dm = backend
        container = MagicMock()
        # Simulate chunks that are strings (some Docker SDK versions)
        container.logs.return_value = iter(["string-chunk\n"])
        client.containers.get.return_value = container
        record = ServiceRecord(name="x", container_name="x")

        chunks = [chunk async for chunk in b.stream_logs(record)]
        assert len(chunks) == 1
        assert isinstance(chunks[0], bytes)
        assert chunks[0] == b"string-chunk\n"


# ---------------------------------------------------------------------------
# remove_container edge cases
# ---------------------------------------------------------------------------


class TestDockerSdkBackendRemoveContainer:
    """Tests for remove_container beyond what test_backend.py covers."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_remove_container_not_found_during_remove(self, backend):
        """remove_container swallows NotFound raised during container.remove()."""
        b, client, dm = backend
        container = MagicMock()
        container.remove.side_effect = dm.errors.NotFound("vanished mid-remove")
        client.containers.get.return_value = container
        record = ServiceRecord(name="x", container_name="x")
        # Must not raise
        await b.remove_container(record)

    async def test_remove_container_generic_exception_logged_not_raised(self, backend):
        """remove_container catches generic Exception, logs warning, does not raise."""
        b, client, dm = backend
        container = MagicMock()
        container.remove.side_effect = RuntimeError("volume in use")
        client.containers.get.return_value = container
        record = ServiceRecord(name="x", container_name="x")
        # Must not raise
        await b.remove_container(record)

    async def test_remove_container_daemon_unreachable_returns_none_then_noop(
        self, backend
    ):
        """When _get_container raises APIError, it propagates (not caught here)."""
        b, client, dm = backend
        client.containers.get.side_effect = dm.errors.APIError("daemon down")
        record = ServiceRecord(name="x", container_name="x")
        with pytest.raises(dm.errors.APIError):
            await b.remove_container(record)


# ---------------------------------------------------------------------------
# _remove_old_container
# ---------------------------------------------------------------------------


class TestDockerSdkBackendRemoveOldContainer:
    """Tests for _remove_old_container."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_remove_old_container_returns_prior_digest(self, backend):
        """Returns the old container's image id as prior_digest."""
        b, client, dm = backend
        old = MagicMock()
        old.image.id = "sha256:old-digest"
        prior = await b._remove_old_container("test-svc", old)
        assert prior == "sha256:old-digest"
        old.stop.assert_called_once()
        old.remove.assert_called_once_with(force=True)

    async def test_remove_old_container_image_id_access_fails_gracefully(self, backend):
        """When image.id raises, prior_digest stays empty but remove proceeds."""
        b, client, dm = backend
        old = MagicMock()
        # Accessing image.id raises
        old_image = MagicMock()
        type(old_image).id = PropertyMock(side_effect=RuntimeError("no image"))
        old.image = old_image
        prior = await b._remove_old_container("test-svc", old)
        assert prior == ""
        old.stop.assert_called_once()
        old.remove.assert_called_once_with(force=True)

    async def test_remove_old_container_stop_remove_fails_raises_runtime_error(
        self, backend
    ):
        """When _stop_and_remove's remove(force=True) raises APIError,
        RuntimeError propagates."""
        b, client, dm = backend
        old = MagicMock()
        # _stop_and_remove swallows exceptions from stop(), but an
        # APIError from remove(force=True) propagates.
        old.remove.side_effect = dm.errors.APIError("permission denied")
        with pytest.raises(RuntimeError, match="Failed to remove existing container"):
            await b._remove_old_container("test-svc", old)


# ---------------------------------------------------------------------------
# _prepare_volumes
# ---------------------------------------------------------------------------


class TestDockerSdkBackendPrepareVolumes:
    """Tests for _prepare_volumes."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_prepare_volumes_creates_named_volumes(self, backend):
        """Named volumes are created via volumes.create()."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc",
            image="img:latest",
            container_name="test-svc",
            named_volumes=["data-vol", "cache-vol"],
        )
        # Patch resolve_user_to_uid_gid to avoid pwd/grp imports
        with (
            patch.object(
                b._volume, "resolve_user_to_uid_gid", return_value=(1000, 1000)
            ),
            patch.object(b._volume, "ensure_volume_ownership"),
        ):
            warnings = await b._prepare_volumes(config)

        assert client.volumes.create.call_count == 2
        client.volumes.create.assert_any_call("data-vol")
        client.volumes.create.assert_any_call("cache-vol")
        assert warnings == []

    async def test_prepare_volumes_409_already_exists(self, backend):
        """When volume already exists (409), creation is skipped."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc",
            image="img:latest",
            container_name="test-svc",
            named_volumes=["existing-vol"],
        )
        api_error_409 = dm.errors.APIError("conflict")
        api_error_409.status_code = 409
        client.volumes.create.side_effect = api_error_409

        with patch.object(
            b._volume, "resolve_user_to_uid_gid", return_value=(1000, 1000)
        ):
            warnings = await b._prepare_volumes(config)

        # No exception raised
        assert warnings == []

    async def test_prepare_volumes_non_409_api_error_raises(self, backend):
        """Non-409 APIError during volume creation raises RuntimeError."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc",
            image="img:latest",
            container_name="test-svc",
            named_volumes=["bad-vol"],
        )
        api_error = dm.errors.APIError("disk full")
        api_error.status_code = 500
        client.volumes.create.side_effect = api_error

        with patch.object(
            b._volume, "resolve_user_to_uid_gid", return_value=(1000, 1000)
        ):
            with pytest.raises(RuntimeError, match="Failed to create volume"):
                await b._prepare_volumes(config)

    async def test_prepare_volumes_docker_exception_raises(self, backend):
        """DockerException during volume creation raises RuntimeError."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc",
            image="img:latest",
            container_name="test-svc",
            named_volumes=["bad-vol"],
        )
        client.volumes.create.side_effect = dm.errors.DockerException("socket error")

        with patch.object(
            b._volume, "resolve_user_to_uid_gid", return_value=(1000, 1000)
        ):
            with pytest.raises(RuntimeError, match="Docker daemon unreachable"):
                await b._prepare_volumes(config)

    async def test_prepare_volumes_with_claude_mount(self, backend):
        """When claude_mount=True, claude-auth volume is created and validated."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc",
            image="img:latest",
            container_name="test-svc",
            claude_mount=True,
            named_volumes=["data-vol"],
        )

        with (
            patch.object(
                b._volume, "resolve_user_to_uid_gid", return_value=(1000, 1000)
            ),
            patch.object(b._volume, "ensure_volume_ownership"),
        ):
            await b._prepare_volumes(config)

        # Both data-vol and claude-auth created
        assert client.volumes.create.call_count == 2
        create_calls = [c[0][0] for c in client.volumes.create.call_args_list]
        assert "data-vol" in create_calls
        assert "claude-auth" in create_calls

    async def test_prepare_volumes_claude_cred_check_warning(self, backend):
        """When claude cred check returns warnings, they're included in result."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc",
            image="img:latest",
            container_name="test-svc",
            claude_mount=True,
        )

        with (
            patch.object(
                b._volume, "resolve_user_to_uid_gid", return_value=(1000, 1000)
            ),
            patch.object(b._volume, "ensure_volume_ownership"),
            patch.object(
                b._auth,
                "check_claude_credentials",
                return_value=["Warning: no credentials"],
            ),
        ):
            warnings = await b._prepare_volumes(config)

        assert "Warning: no credentials" in warnings

    async def test_prepare_volumes_claude_cred_check_exception_non_fatal(self, backend):
        """When claude cred check raises, it's caught and logged (non-fatal)."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc",
            image="img:latest",
            container_name="test-svc",
            claude_mount=True,
        )

        with (
            patch.object(
                b._volume, "resolve_user_to_uid_gid", return_value=(1000, 1000)
            ),
            patch.object(b._volume, "ensure_volume_ownership"),
            patch.object(
                b._auth,
                "check_claude_credentials",
                side_effect=RuntimeError("boom"),
            ),
        ):
            warnings = await b._prepare_volumes(config)

        # No exception raised, no warnings returned for exception case
        assert warnings == []


# ---------------------------------------------------------------------------
# _try_restore
# ---------------------------------------------------------------------------


class TestDockerSdkBackendTryRestore:
    """Tests for _try_restore."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_try_restore_with_prior_digest(self, backend):
        """Restore creates and starts container from prior_digest."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc", image="img:latest", container_name="test-svc"
        )
        restored = MagicMock()
        client.containers.create.return_value = restored

        await b._try_restore("test-svc", config, "sha256:prior")

        client.containers.create.assert_called_once()
        _, kwargs = client.containers.create.call_args
        assert kwargs["image"] == "sha256:prior"
        restored.start.assert_called_once()

    async def test_try_restore_empty_prior_digest_noop(self, backend):
        """When prior_digest is empty, _try_restore is a no-op."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc", image="img:latest", container_name="test-svc"
        )
        await b._try_restore("test-svc", config, "")
        client.containers.create.assert_not_called()

    async def test_try_restore_failure_logged_not_raised(self, backend):
        """When restore fails, exception is logged but not re-raised."""
        b, client, dm = backend
        config = ComponentConfig(
            id="test-svc", image="img:latest", container_name="test-svc"
        )
        client.containers.create.side_effect = RuntimeError("restore boom")

        # Must not raise
        await b._try_restore("test-svc", config, "sha256:prior")


# ---------------------------------------------------------------------------
# disk_df
# ---------------------------------------------------------------------------


class TestDockerSdkBackendDiskDf:
    """Tests for disk_df."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_disk_df_success(self, backend):
        """disk_df returns DockerDfStats with images, build cache, volumes."""
        b, client, dm = backend
        client.api.df.return_value = {
            "Images": [{"Size": 100}],
            "BuildCache": [
                {"Size": 200, "InUse": True},
                {"Size": 300, "InUse": False},
            ],
            "LayersSize": 500,
            "Volumes": [
                {
                    "Name": "vol-a",
                    "UsageData": {"Size": 1024, "RefCount": 1},
                },
                {
                    "Name": "vol-b",
                    "UsageData": {"Size": 2048, "RefCount": 0},
                },
            ],
        }

        result = await b.disk_df()

        assert isinstance(result, DockerDfStats)
        assert result.images_size_bytes == 500  # LayersSize preferred
        assert result.build_cache_size_bytes == 500  # 200 + 300
        assert result.build_cache_reclaimable_bytes == 300  # only InUse=False
        assert len(result.volumes) == 2
        assert result.volumes[0].name == "vol-a"
        assert result.volumes[0].size_bytes == 1024
        assert result.volumes[0].in_use is True
        assert result.volumes[1].name == "vol-b"
        assert result.volumes[1].in_use is False

    async def test_disk_df_layers_size_zero_falls_back_to_sum(self, backend):
        """When LayersSize is 0, falls back to sum of image sizes."""
        b, client, dm = backend
        client.api.df.return_value = {
            "Images": [{"Size": 150}, {"Size": 250}],
            "BuildCache": [],
            "LayersSize": 0,
            "Volumes": [],
        }
        result = await b.disk_df()
        assert result.images_size_bytes == 400  # 150 + 250

    async def test_disk_df_api_error_returns_empty_stats(self, backend):
        """When docker df fails with APIError, returns empty DockerDfStats."""
        b, client, dm = backend
        client.api.df.side_effect = dm.errors.APIError("daemon down")
        result = await b.disk_df()
        assert result.images_size_bytes == 0
        assert result.build_cache_size_bytes == 0
        assert result.build_cache_reclaimable_bytes == 0
        assert result.volumes == []

    async def test_disk_df_volume_sentinel_negative_one_skipped(self, backend):
        """Volumes with Size==-1 (unknown sentinel) are skipped."""
        b, client, dm = backend
        client.api.df.return_value = {
            "Images": [],
            "BuildCache": [],
            "Volumes": [
                {"Name": "unknown-vol", "UsageData": {"Size": -1, "RefCount": 0}},
            ],
        }
        result = await b.disk_df()
        assert len(result.volumes) == 0


# ---------------------------------------------------------------------------
# prune_builds
# ---------------------------------------------------------------------------


class TestDockerSdkBackendPruneBuilds:
    """Tests for prune_builds."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_prune_builds_returns_reclaimed_bytes(self, backend):
        """prune_builds calls api.prune_builds(all=True) and returns SpaceReclaimed."""
        b, client, dm = backend
        client.api.prune_builds.return_value = {"SpaceReclaimed": 12345}
        result = await b.prune_builds()
        assert result == 12345
        client.api.prune_builds.assert_called_once_with(all=True)

    async def test_prune_builds_missing_key_returns_zero(self, backend):
        """When SpaceReclaimed is missing from result, returns 0."""
        b, client, dm = backend
        client.api.prune_builds.return_value = {}
        result = await b.prune_builds()
        assert result == 0


# ---------------------------------------------------------------------------
# run_config_assist
# ---------------------------------------------------------------------------


class TestDockerSdkBackendRunConfigAssist:
    """Tests for run_config_assist."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_run_config_assist_success(self, backend):
        """run_config_assist returns container logs on success."""
        b, client, dm = backend
        container = MagicMock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.return_value = b"config applied\n"
        client.containers.create.return_value = container

        result = await b.run_config_assist(
            image="busybox",
            command_str="cat /config/config.yaml",
            volume_name="data-vol",
            volume_mount_path="/config",
            env_dict={"KEY": "val"},
            timeout_seconds=30,
        )

        assert result == "config applied\n"
        container.start.assert_called_once()
        container.wait.assert_called_once_with(timeout=30)
        container.remove.assert_called_once_with(force=True)

    async def test_run_config_assist_non_zero_exit_raises(self, backend):
        """run_config_assist raises RuntimeError on non-zero exit code."""
        b, client, dm = backend
        container = MagicMock()
        container.wait.return_value = {"StatusCode": 1}
        container.logs.return_value = b"error: something broke\n"
        client.containers.create.return_value = container

        with pytest.raises(RuntimeError, match="exited with code 1"):
            await b.run_config_assist(
                image="busybox",
                command_str="false",
                volume_name="vol",
                volume_mount_path="/mnt",
                env_dict={},
            )

        # Container was still removed
        container.remove.assert_called_once_with(force=True)

    async def test_run_config_assist_timeout(self, backend):
        """run_config_assist raises TimeoutError on timeout, kills container."""
        b, client, dm = backend
        container = MagicMock()
        import requests.exceptions

        container.wait.side_effect = requests.exceptions.ReadTimeout("timed out")
        client.containers.create.return_value = container

        with pytest.raises(TimeoutError, match="timed out"):
            await b.run_config_assist(
                image="busybox",
                command_str="sleep 999",
                volume_name="vol",
                volume_mount_path="/mnt",
                env_dict={},
                timeout_seconds=5,
            )

        # Container was killed and removed
        container.kill.assert_called_once()
        container.remove.assert_called_once_with(force=True)


# ---------------------------------------------------------------------------
# trigger_self_update
# ---------------------------------------------------------------------------


class TestDockerSdkBackendTriggerSelfUpdate:
    """Tests for trigger_self_update."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_trigger_self_update_success(self, backend):
        """Returns watchtower container id on success."""
        from robotsix_central_deploy.lifecycle.models import SelfInspect

        b, client, dm = backend
        client.api.create_container.return_value = {"Id": "watchtower-abc123"}
        target = SelfInspect(
            container_id="self-id",
            container_name="central-deploy",
            image_ref="ghcr.io/o/server:main",
            networks=["proxy"],
        )

        cid = await b.trigger_self_update(
            target=target,
            watchtower_image="containrrr/watchtower:latest",
            docker_host_url="tcp://proxy:2375",
            docker_api_version="1.44",
        )

        assert cid == "watchtower-abc123"
        client.images.pull.assert_called_once_with("containrrr/watchtower:latest")
        client.api.create_container.assert_called_once()
        client.api.start.assert_called_once_with("watchtower-abc123")

        # Verify networking_config was created
        _, kwargs = client.api.create_container.call_args
        assert kwargs["networking_config"] is not None

    async def test_trigger_self_update_no_networks(self, backend):
        """When target has no networks, networking_config is None."""
        from robotsix_central_deploy.lifecycle.models import SelfInspect

        b, client, dm = backend
        client.api.create_container.return_value = {"Id": "ctr-id"}
        target = SelfInspect(
            container_id="self-id",
            container_name="central-deploy",
            image_ref="ghcr.io/o/server:main",
            networks=[],
        )

        _ = await b.trigger_self_update(
            target=target,
            watchtower_image="containrrr/watchtower:latest",
            docker_host_url="tcp://proxy:2375",
            docker_api_version="1.44",
        )

        _, kwargs = client.api.create_container.call_args
        assert kwargs["networking_config"] is None

    async def test_trigger_self_update_api_error_raises(self, backend):
        """APIError during launch raises RuntimeError."""
        from robotsix_central_deploy.lifecycle.models import SelfInspect

        b, client, dm = backend
        client.images.pull.side_effect = dm.errors.APIError("pull failed")
        target = SelfInspect(
            container_id="self-id",
            container_name="central-deploy",
            image_ref="ghcr.io/o/server:main",
            networks=[],
        )

        with pytest.raises(RuntimeError, match="failed to launch self-update"):
            await b.trigger_self_update(
                target=target,
                watchtower_image="containrrr/watchtower:latest",
                docker_host_url="tcp://proxy:2375",
                docker_api_version="1.44",
            )


# ---------------------------------------------------------------------------
# Claude auth delegation
# ---------------------------------------------------------------------------


class TestDockerSdkBackendClaudeAuthDelegation:
    """check/write/read claude auth methods delegate to self._auth."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_check_claude_auth_delegates(self, backend):
        b, client, dm = backend
        with patch.object(
            b._auth,
            "check_claude_auth",
            return_value={"status": "authenticated"},
        ) as mock_check:
            result = await b.check_claude_auth("claude-auth")
            mock_check.assert_called_once_with("claude-auth")
            assert result == {"status": "authenticated"}

    async def test_write_claude_credentials_delegates(self, backend):
        b, client, dm = backend
        with patch.object(
            b._auth,
            "write_claude_credentials",
            return_value={"status": "ok"},
        ) as mock_write:
            result = await b.write_claude_credentials("vol", '{"key":"val"}')
            mock_write.assert_called_once_with("vol", '{"key":"val"}')
            assert result == {"status": "ok"}

    async def test_read_claude_credentials_delegates(self, backend):
        b, client, dm = backend
        with patch.object(
            b._auth,
            "read_claude_credentials",
            return_value={"key": "val"},
        ) as mock_read:
            result = await b.read_claude_credentials("vol")
            mock_read.assert_called_once_with("vol")
            assert result == {"key": "val"}


# ---------------------------------------------------------------------------
# Volume ops delegation
# ---------------------------------------------------------------------------


class TestDockerSdkBackendVolumeOpsDelegation:
    """Config/volume ops delegate to self._volume."""

    @pytest.fixture
    def backend(self):
        dm = _make_docker_mock()
        client = MagicMock()
        dm.DockerClient.return_value = client
        with patch.dict(sys.modules, {"docker": dm}):
            b = DockerSdkBackend()
            yield b, client, dm

    async def test_write_config_to_volume_delegates(self, backend):
        b, client, dm = backend
        with patch.object(b._volume, "write_config_to_volume") as mock_fn:
            await b.write_config_to_volume("vol", {"a": 1})
            mock_fn.assert_called_once_with("vol", {"a": 1})

    async def test_write_llmio_tier_config_to_volume_delegates(self, backend):
        b, client, dm = backend
        with patch.object(b._volume, "write_llmio_tier_config_to_volume") as mock_fn:
            await b.write_llmio_tier_config_to_volume("vol", {"tier": "level1"})
            mock_fn.assert_called_once_with("vol", {"tier": "level1"})

    async def test_read_config_from_volume_delegates(self, backend):
        b, client, dm = backend
        with patch.object(
            b._volume, "read_config_from_volume", return_value={"key": "val"}
        ) as mock_fn:
            result = await b.read_config_from_volume("vol")
            mock_fn.assert_called_once_with("vol")
            assert result == {"key": "val"}

    async def test_measure_volume_bytes_delegates(self, backend):
        b, client, dm = backend
        with patch.object(
            b._volume, "measure_volume_bytes", return_value=42
        ) as mock_fn:
            result = await b.measure_volume_bytes("vol")
            mock_fn.assert_called_once_with("vol")
            assert result == 42

    async def test_list_volume_dir_delegates(self, backend):
        b, client, dm = backend
        with patch.object(
            b._volume,
            "list_volume_dir",
            return_value=[{"name": "f.txt", "type": "file", "size_bytes": 10}],
        ) as mock_fn:
            result = await b.list_volume_dir("vol", "subdir")
            mock_fn.assert_called_once_with("vol", "subdir")
            assert result == [{"name": "f.txt", "type": "file", "size_bytes": 10}]

    async def test_read_volume_file_delegates(self, backend):
        b, client, dm = backend
        expected = {
            "size_bytes": 100,
            "content": "hello",
            "binary": False,
            "truncated": False,
        }
        with patch.object(
            b._volume, "read_volume_file", return_value=expected
        ) as mock_fn:
            result = await b.read_volume_file("vol", "notes.txt", 1024)
            mock_fn.assert_called_once_with("vol", "notes.txt", 1024)
            assert result == expected

    async def test_remove_volume_delegates(self, backend):
        b, client, dm = backend
        with patch.object(b._volume, "remove_volume") as mock_fn:
            await b.remove_volume("vol")
            mock_fn.assert_called_once_with("vol")


# ---------------------------------------------------------------------------
# Contract conformance
# ---------------------------------------------------------------------------


class TestDockerSdkBackendContractConformance:
    """Ensure DockerSdkBackend satisfies the ExecutionBackend interface."""

    def test_all_abstract_methods_implemented(self):
        backend_methods = set(dir(DockerSdkBackend))
        abstract_names: set[str] = set()
        for cls in ExecutionBackend.__mro__:
            for name, attr in cls.__dict__.items():
                if getattr(attr, "__isabstractmethod__", False):
                    abstract_names.add(name)
        missing = abstract_names - backend_methods
        assert not missing, f"DockerSdkBackend is missing abstract methods: {missing}"
