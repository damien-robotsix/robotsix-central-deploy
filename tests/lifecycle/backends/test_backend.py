"""Tests for the execution backends."""

import base64
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from robotsix_central_deploy.lifecycle.backends import DockerSdkBackend, NoopBackend
from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    PortMapping,
    ServiceConfig,
)


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

    @staticmethod
    def _make_self_container(hostname: str) -> MagicMock:
        container = MagicMock()
        container.attrs = {
            "Id": "newid123",
            "Name": "/central-deploy",
            "Config": {
                "Image": "ghcr.io/damien-robotsix/robotsix-central-deploy:main",
                "Hostname": hostname,
            },
            "NetworkSettings": {"Networks": {"internal": {}, "proxy": {}}},
        }
        container.image.attrs = {
            "RepoDigests": [
                "ghcr.io/damien-robotsix/robotsix-central-deploy@sha256:abc"
            ]
        }
        return container

    async def test_inspect_self_direct_hostname_hit(self, backend, monkeypatch):
        b, client = backend
        monkeypatch.setattr("socket.gethostname", lambda: "selfid")
        container = self._make_self_container("selfid")
        client.containers.get.return_value = container
        info = await b.inspect_self()
        assert info is not None
        assert info.container_name == "central-deploy"
        assert info.running_digest == "sha256:abc"
        assert info.networks == ["internal", "proxy"]

    async def test_inspect_self_falls_back_to_config_hostname(
        self, backend, monkeypatch
    ):
        # After a watchtower self-update the recreated container keeps the
        # previous container's hostname, so the id lookup misses and the
        # Config.Hostname scan must find it.
        import docker

        b, client = backend
        monkeypatch.setattr("socket.gethostname", lambda: "stale-old-id")
        matching = self._make_self_container("stale-old-id")
        other = self._make_self_container("unrelated")
        summary_other, summary_match = MagicMock(id="o1"), MagicMock(id="m1")
        client.containers.list.return_value = [summary_other, summary_match]
        client.containers.get.side_effect = [
            docker.errors.NotFound("no container with id stale-old-id"),
            other,
            matching,
        ]
        info = await b.inspect_self()
        assert info is not None
        assert info.running_digest == "sha256:abc"

    async def test_inspect_self_none_when_nothing_matches(self, backend, monkeypatch):
        import docker

        b, client = backend
        monkeypatch.setattr("socket.gethostname", lambda: "ghost")
        client.containers.list.return_value = []
        client.containers.get.side_effect = docker.errors.NotFound("nope")
        assert await b.inspect_self() is None

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

    async def test_remove_volume_calls_get_and_remove_force(self, backend):
        b, client = backend
        volume = MagicMock()
        client.volumes.get.return_value = volume
        await b.remove_volume("svc-a-data")
        client.volumes.get.assert_called_once_with("svc-a-data")
        volume.remove.assert_called_once_with(force=True)

    async def test_remove_volume_swallows_not_found(self, backend):
        import docker

        b, client = backend
        client.volumes.get.side_effect = docker.errors.NotFound("gone")
        # Must not raise — already-gone volume is a no-op.
        await b.remove_volume("svc-a-data")

    async def test_remove_volume_swallows_other_errors(self, backend):
        b, client = backend
        volume = MagicMock()
        volume.remove.side_effect = RuntimeError("volume in use")
        client.volumes.get.return_value = volume
        # Best-effort: other errors are logged, not raised.
        await b.remove_volume("svc-a-data")

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
# Docker SDK backend — prune_images
# ---------------------------------------------------------------------------


def _make_image(image_id: str, size: int, repo_digests: list[str] | None = None):
    img = MagicMock()
    img.id = image_id
    img.attrs = {"Size": size, "RepoDigests": repo_digests or []}
    return img


class TestDockerSdkBackendPruneImages:
    @pytest.fixture
    def backend(self):
        client_mock = MagicMock()
        docker_mock = MagicMock()
        docker_mock.DockerClient = MagicMock(return_value=client_mock)
        docker_mock.errors.NotFound = type("NotFound", (Exception,), {})
        docker_mock.errors.APIError = type("APIError", (Exception,), {})
        with patch.dict(sys.modules, {"docker": docker_mock}):
            b = DockerSdkBackend()
            yield b, client_mock, docker_mock

    async def test_prune_removes_unprotected_dangling(self, backend):
        b, client, _ = backend
        client.images.list.return_value = [
            _make_image("sha256:old1", 100),
            _make_image("sha256:old2", 250),
        ]
        reclaimed = await b.prune_images(set())
        assert reclaimed == 350
        removed = [c.args[0] for c in client.images.remove.call_args_list]
        assert removed == ["sha256:old1", "sha256:old2"]
        client.images.list.assert_called_once_with(filters={"dangling": True})

    async def test_prune_skips_protected_by_id_and_digest(self, backend):
        b, client, _ = backend
        client.images.list.return_value = [
            _make_image("sha256:rollback-target", 100),
            _make_image(
                "sha256:local-id",
                200,
                repo_digests=["robotsix/mill@sha256:manifest-digest"],
            ),
            _make_image("sha256:prunable", 50),
        ]
        reclaimed = await b.prune_images(
            {"sha256:rollback-target", "sha256:manifest-digest"}
        )
        assert reclaimed == 50
        removed = [c.args[0] for c in client.images.remove.call_args_list]
        assert removed == ["sha256:prunable"]

    async def test_prune_swallows_remove_errors(self, backend):
        b, client, docker_mock = backend
        client.images.list.return_value = [
            _make_image("sha256:in-use", 100),
            _make_image("sha256:prunable", 70),
        ]

        def _remove(image_id):
            if image_id == "sha256:in-use":
                raise docker_mock.errors.APIError("conflict: image is in use")

        client.images.remove.side_effect = _remove
        reclaimed = await b.prune_images(set())
        assert reclaimed == 70

    async def test_prune_list_failure_returns_zero(self, backend):
        b, client, docker_mock = backend
        client.images.list.side_effect = docker_mock.errors.APIError("boom")
        assert await b.prune_images(set()) == 0


class TestCollectProtectedImageRefs:
    async def test_collects_all_digest_fields(self):
        from robotsix_central_deploy.lifecycle.backends import (
            collect_protected_image_refs,
        )

        records = [
            ServiceRecord(
                name="a",
                deployed_image_digest="sha256:dep-a",
                previous_image_digest="sha256:prev-a",
                image_revision="sha256:rev-a",
            ),
            ServiceRecord(name="b", previous_image_digest="sha256:prev-b"),
            ServiceRecord(name="c"),
        ]
        store = MagicMock()

        async def _list_all():
            return records

        store.list_all = _list_all
        protected = await collect_protected_image_refs(store)
        assert protected == {
            "sha256:dep-a",
            "sha256:prev-a",
            "sha256:rev-a",
            "sha256:prev-b",
        }


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
        record = ServiceRecord(
            name="test-svc", container_name="test-svc", state=ServiceState.STOPPED
        )

        outcome = await b.deploy(record, config, "test:latest")
        assert outcome.state == ServiceState.RUNNING
        client.containers.create.assert_called_once()
        _, kwargs = client.containers.create.call_args
        assert kwargs.get("ports") == {} or "ports" not in kwargs


# ---------------------------------------------------------------------------
# Docker SDK backend — command and entrypoint passthrough
# ---------------------------------------------------------------------------


class TestDockerSdkBackendCommandAndEntrypoint:
    """_create_container must pass command and entrypoint through to the
    Docker SDK, with None meaning "use image default"."""

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

    def test_create_container_passes_command(self, backend):
        b, client = backend
        config = ComponentConfig(
            id="test-svc",
            image="test:latest",
            container_name="test-svc",
            command=["serve", "--port", "8080"],
        )
        b._create_container(config, "test:latest")
        _, kwargs = client.containers.create.call_args
        assert kwargs["command"] == ["serve", "--port", "8080"]

    def test_create_container_none_command(self, backend):
        b, client = backend
        config = ComponentConfig(
            id="test-svc",
            image="test:latest",
            container_name="test-svc",
        )
        b._create_container(config, "test:latest")
        _, kwargs = client.containers.create.call_args
        assert kwargs["command"] is None

    def test_create_container_passes_entrypoint(self, backend):
        b, client = backend
        config = ComponentConfig(
            id="test-svc",
            image="test:latest",
            container_name="test-svc",
            entrypoint=["/usr/bin/env", "python", "-m", "app"],
        )
        b._create_container(config, "test:latest")
        _, kwargs = client.containers.create.call_args
        assert kwargs["entrypoint"] == ["/usr/bin/env", "python", "-m", "app"]

    def test_create_container_none_entrypoint(self, backend):
        b, client = backend
        config = ComponentConfig(
            id="test-svc",
            image="test:latest",
            container_name="test-svc",
        )
        b._create_container(config, "test:latest")
        _, kwargs = client.containers.create.call_args
        assert kwargs["entrypoint"] is None

    def test_create_container_passes_tmpfs(self, backend):
        """tmpfs list is converted to a dict for the Docker SDK."""
        b, client = backend
        config = ComponentConfig(
            id="test-svc",
            image="test:latest",
            container_name="test-svc",
            tmpfs=["/run", "/tmp"],
        )
        b._create_container(config, "test:latest")
        _, kwargs = client.containers.create.call_args
        assert kwargs["tmpfs"] == {"/run": "", "/tmp": ""}

    def test_create_container_empty_tmpfs_passes_none(self, backend):
        """Empty tmpfs list results in tmpfs=None (no tmpfs mounts)."""
        b, client = backend
        config = ComponentConfig(
            id="test-svc",
            image="test:latest",
            container_name="test-svc",
            tmpfs=[],
        )
        b._create_container(config, "test:latest")
        _, kwargs = client.containers.create.call_args
        assert kwargs.get("tmpfs") is None


# ---------------------------------------------------------------------------
# Docker SDK backend — user= injection (host UID/GID)
# ---------------------------------------------------------------------------


class TestDockerSdkBackendUserInjection:
    """_create_container must pass user=<uid>:<gid> for every service,
    derived at runtime from the host process."""

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

    def test_create_container_injects_host_uid_gid(self, backend):
        """A plain service (no claude_mount) gets user=<uid>:<gid>."""
        b, client = backend
        config = ComponentConfig(
            id="test-svc",
            image="test:latest",
            container_name="test-svc",
        )
        b._create_container(config, "test:latest")
        _, kwargs = client.containers.create.call_args
        assert kwargs["user"] == f"{os.getuid()}:{os.getgid()}"

    def test_create_container_with_claude_mount_also_injects_uid_gid(self, backend):
        """A service with claude_mount=True gets the claude-auth named volume
        mounted at /home/app/.claude and user=<uid>:<gid>."""
        b, client = backend
        config = ComponentConfig(
            id="test-svc",
            image="test:latest",
            container_name="test-svc",
            claude_mount=True,
        )
        b._create_container(config, "test:latest")
        _, kwargs = client.containers.create.call_args
        assert kwargs["user"] == f"{os.getuid()}:{os.getgid()}"
        # Verify named volume mount (not a host bind mount)
        volumes = kwargs["volumes"]
        assert "claude-auth" in volumes
        assert volumes["claude-auth"] == {"bind": "/home/app/.claude", "mode": "rw"}

    def test_create_container_respects_explicit_user(self, backend):
        """When config.user is set, it overrides the host UID:GID."""
        b, client = backend
        config = ComponentConfig(
            id="test-svc",
            image="test:latest",
            container_name="test-svc",
            user="root",
        )
        b._create_container(config, "test:latest")
        _, kwargs = client.containers.create.call_args
        assert kwargs["user"] == "root"

    def test_create_container_host_docker_sock_keeps_image_user(self, backend):
        """A socket-mounting service without an explicit user keeps the
        image's default user (needed to reach the root:docker socket)."""
        b, client = backend
        config = ComponentConfig(
            id="test-proxy",
            image="tecnativa/docker-socket-proxy:latest",
            container_name="test-proxy",
            host_docker_sock=True,
        )
        b._create_container(config, "tecnativa/docker-socket-proxy:latest")
        _, kwargs = client.containers.create.call_args
        assert kwargs["user"] is None

    def test_create_container_host_docker_sock_explicit_user_wins(self, backend):
        """An explicit config.user still applies to socket-mounting services."""
        b, client = backend
        config = ComponentConfig(
            id="test-proxy",
            image="tecnativa/docker-socket-proxy:latest",
            container_name="test-proxy",
            host_docker_sock=True,
            user="1000:994",
        )
        b._create_container(config, "tecnativa/docker-socket-proxy:latest")
        _, kwargs = client.containers.create.call_args
        assert kwargs["user"] == "1000:994"


# ---------------------------------------------------------------------------
# Claude auth credential validation
# ---------------------------------------------------------------------------


class TestClaudeAuthCredentialCheck:
    """_check_claude_credentials must warn when the claude-auth volume
    is missing or lacks a readable .credentials.json."""

    @pytest.fixture
    def client_mock(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def backend(self, client_mock: MagicMock):
        docker_mock = MagicMock()
        docker_mock.DockerClient = MagicMock(return_value=client_mock)
        docker_mock.errors.NotFound = type("NotFound", (Exception,), {})
        docker_mock.errors.APIError = type("APIError", (Exception,), {})
        docker_mock.errors.ContainerError = type("ContainerError", (Exception,), {})
        with patch.dict(sys.modules, {"docker": docker_mock}):
            b = DockerSdkBackend()
            yield b, client_mock

    def test_warns_when_volume_missing(self, backend):
        """Returns a warning when claude-auth volume does not exist."""
        b, client = backend
        import docker

        client.volumes.get.side_effect = docker.errors.NotFound("no such volume")
        warnings = b._check_claude_credentials()
        assert len(warnings) == 1
        assert "does not exist" in warnings[0]
        assert "claude-auth" in warnings[0]
        assert "Claude auth" in warnings[0]

    def test_warns_when_credentials_missing(self, backend):
        """Returns a warning when volume exists but .credentials.json
        is not readable."""
        b, client = backend
        import docker

        # volumes.get succeeds (volume exists)
        # containers.run raises ContainerError (test -f fails)
        client.containers.run.side_effect = docker.errors.ContainerError()
        warnings = b._check_claude_credentials()
        assert len(warnings) == 1
        assert "does not contain a readable .credentials.json" in warnings[0]
        assert "claude-auth" in warnings[0]
        assert "Claude auth" in warnings[0]

    def test_no_warning_when_credentials_present(self, backend):
        """Returns empty list when .credentials.json is readable."""
        b, client = backend
        # containers.run succeeds (test -f exits 0)
        warnings = b._check_claude_credentials()
        assert warnings == []


# ---------------------------------------------------------------------------
# Volume directory listing & file reading (DockerSdkBackend)
# ---------------------------------------------------------------------------


class TestDockerSdkBackendVolumeBrowser:
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

    async def test_list_volume_dir_empty_root(self, backend):
        b, client = backend
        client.containers.run.return_value = b""
        result = await b.list_volume_dir("test-vol", "")
        assert result == []

    async def test_list_volume_dir_files_and_dirs(self, backend):
        b, client = backend
        client.containers.run.return_value = (
            b"dir\t0\tsubdir\nfile\t1024\tconfig.yaml\nfile\t512\tnotes.txt\n"
        )
        result = await b.list_volume_dir("test-vol", "")
        assert len(result) == 3
        assert result[0] == {"name": "subdir", "type": "dir", "size_bytes": 0}
        assert result[1] == {
            "name": "config.yaml",
            "type": "file",
            "size_bytes": 1024,
        }
        assert result[2] == {
            "name": "notes.txt",
            "type": "file",
            "size_bytes": 512,
        }

    async def test_list_volume_dir_passes_rel_path_as_positional_arg(self, backend):
        b, client = backend
        client.containers.run.return_value = b""
        await b.list_volume_dir("test-vol", "subdir/logs")
        call_args = client.containers.run.call_args
        command = call_args[1]["command"]
        # command = ["sh", "-c", script, "sh", rel_path]
        # $0="sh", $1="subdir/logs"
        assert command[4] == "subdir/logs"

    async def test_list_volume_dir_read_only_mount(self, backend):
        b, client = backend
        client.containers.run.return_value = b""
        await b.list_volume_dir("test-vol", "")
        call_kwargs = client.containers.run.call_args[1]
        vol_mount = call_kwargs["volumes"]["test-vol"]
        assert vol_mount["mode"] == "ro"

    async def test_read_volume_file_text(self, backend):
        b, client = backend
        client.containers.run.return_value = b"42\nhello world\n"
        result = await b.read_volume_file("test-vol", "notes.txt", 1_048_576)
        assert result["size_bytes"] == 42
        assert result["content"] == "hello world\n"
        assert result["binary"] is False
        assert result["truncated"] is False

    async def test_read_volume_file_truncation(self, backend):
        b, client = backend
        # Content is 10 bytes, max_bytes=4 → truncated
        client.containers.run.return_value = b"100\n0123456789\n"
        result = await b.read_volume_file("test-vol", "big.txt", 4)
        assert result["size_bytes"] == 100
        assert result["truncated"] is True
        assert result["content"] == "0123"
        assert result["binary"] is False

    async def test_read_volume_file_binary_nul_byte(self, backend):
        b, client = backend
        client.containers.run.return_value = b"256\nsome text\x00here\n"
        result = await b.read_volume_file("test-vol", "data.db", 1_048_576)
        assert result["binary"] is True
        assert result["content"] is None

    async def test_read_volume_file_binary_decode_error(self, backend):
        b, client = backend
        # Raw bytes that are not valid UTF-8
        client.containers.run.return_value = b"4\n\x80\x81\x82\x83\n"
        result = await b.read_volume_file("test-vol", "bad.bin", 1_048_576)
        assert result["binary"] is True
        assert result["content"] is None

    async def test_read_volume_file_empty_size(self, backend):
        b, client = backend
        client.containers.run.return_value = b"0\n\n"
        result = await b.read_volume_file("test-vol", "empty.txt", 1_048_576)
        assert result["size_bytes"] == 0
        assert result["content"] == "\n"
        assert result["binary"] is False
        assert result["truncated"] is False

    # -- write_config_to_volume --

    async def test_write_config_to_volume_writes_parseable_json(self, backend):
        """write_config_to_volume writes parseable JSON to /config/config.json."""
        b, client = backend
        client.containers.run.return_value = b""
        config = {"host": "localhost", "port": 8080, "nested": {"key": "value"}}

        await b.write_config_to_volume("test-vol", config)

        call_kwargs = client.containers.run.call_args[1]
        cmd = call_kwargs["command"][2]
        # Target path is the JSON config-standard file
        assert "/config/config.json" in cmd
        # The base64 payload decodes back to the JSON we wrote
        encoded = cmd.split("echo ", 1)[1].split(" | base64 -d", 1)[0]
        written = base64.b64decode(encoded).decode()
        assert json.loads(written) == config
        # Volume mounted read-write
        assert call_kwargs["volumes"]["test-vol"]["mode"] == "rw"

    # -- NoopBackend stubs --

    async def test_noop_list_volume_dir_raises(self):
        from robotsix_central_deploy.lifecycle.backends import NoopBackend

        b = NoopBackend()
        with pytest.raises(NotImplementedError):
            await b.list_volume_dir("v", "")

    async def test_noop_read_volume_file_raises(self):
        from robotsix_central_deploy.lifecycle.backends import NoopBackend

        b = NoopBackend()
        with pytest.raises(NotImplementedError):
            await b.read_volume_file("v", "f", 100)
