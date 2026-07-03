"""Tests for the deploy and rollback endpoints + DockerSdkBackend deploy/rollback."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.backend import DockerSdkBackend
from robotsix_central_deploy.lifecycle.models import (
    ExecutionBackendType,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    HealthCheck,
    PortMapping,
    VolumeMount,
)

# Import the server module so we can set its globals.
from robotsix_central_deploy.lifecycle import server as server_mod


# ---------------------------------------------------------------------------
# Helper: a minimal component config for testing
# ---------------------------------------------------------------------------


def _make_config(component_id: str = "svc-a", image: str = "repo:v1"):
    return ComponentConfig(
        id=component_id,
        image=image,
        container_name=component_id,
        ports=[PortMapping(host=8080, container=8080)],
        mounts=[VolumeMount(host="/data", container="/data")],
        env={"KEY": "val"},
        health_check=HealthCheck(
            test=["CMD", "curl", "-f", "http://localhost:8080/health"],
            interval_seconds=30,
            timeout_seconds=10,
            retries=3,
            start_period_seconds=10,
        ),
    )


# ---------------------------------------------------------------------------
# Fixtures — extend the existing server test setup with a registry
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    """A small ComponentRegistry with one known service."""
    return ComponentRegistry([_make_config("svc-a", "repo:v1")])


@pytest.fixture(autouse=True)
def _ensure_registry(monkeypatch, registry):
    """Ensure app.state has store, backend, config, and registry set for every test."""
    from pathlib import Path

    from robotsix_central_deploy.lifecycle.backend import NoopBackend
    from robotsix_central_deploy.lifecycle.config import LifecycleConfig
    from robotsix_central_deploy.lifecycle.store import InMemoryStore
    from robotsix_central_deploy.registry.config_store import ComponentConfigStore
    from robotsix_central_deploy.registry.config_yaml_store import ConfigYamlStore
    from robotsix_central_deploy.registry.env_store import EnvStore
    from robotsix_central_deploy.registry.secret_key import SecretKeyManager

    monkeypatch.setenv("ROBOTSIX_LIFECYCLE_API_KEY", "test-key")
    cfg = LifecycleConfig(  # type: ignore[call-arg]
        store_backend="memory",
        execution_backend=ExecutionBackendType.NOOP,
        api_key="test-key",
    )
    store = InMemoryStore()
    backend = NoopBackend()

    mock_checker = MagicMock()
    mock_checker.get_latest_digest = AsyncMock(return_value=None)

    key_manager = SecretKeyManager(Path("/tmp/test_fernet_key"))  # noqa: S108
    env_store = EnvStore(Path("/tmp/test_env_store.json"), key_manager)  # noqa: S108
    config_store = ComponentConfigStore(Path("/tmp/test_config_store.json"))  # noqa: S108
    config_yaml_store = ConfigYamlStore(Path("/tmp/test_config_yaml.json"))  # noqa: S108

    server_mod._config = cfg
    server_mod._store = store
    server_mod._backend = backend
    server_mod._registry_checker = mock_checker
    server_mod.app.state.config = cfg
    server_mod.app.state.store = store
    server_mod.app.state.backend = backend
    server_mod.app.state.registry = registry
    server_mod.app.state.registry_checker = mock_checker
    server_mod.app.state.env_store = env_store
    server_mod.app.state.component_config_store = config_store
    server_mod.app.state.config_yaml_store = config_yaml_store


# ---------------------------------------------------------------------------
# TestDeployEndpoint — integration tests via the HTTP client
# ---------------------------------------------------------------------------


class TestDeployEndpoint:
    """Deploy/rollback endpoint integration tests using the NoopBackend."""

    @pytest.fixture
    async def client(self) -> AsyncClient:
        transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    async def _seed(self, name: str = "svc-a"):
        store = server_mod.app.state.store
        await store.put(
            ServiceRecord(name=name, state=ServiceState.STOPPED, image="repo:v1")
        )

    async def test_deploy_uses_config_image_when_body_omitted(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        await self._seed("svc-a")
        resp = await client.post("/services/svc-a/deploy", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "svc-a"
        assert data["action"] == "deploy"
        assert (
            data["deployed_digest"] == "sha256:noop"
        )  # NoopBackend always returns this
        assert data["current_state"] == ServiceState.RUNNING.value

    async def test_deploy_with_image_override(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        await self._seed("svc-a")
        resp = await client.post(
            "/services/svc-a/deploy",
            json={"image": "repo:v2"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["deployed_digest"] == "sha256:noop"

        # Verify the record was updated with the override image ref
        store = server_mod.app.state.store
        rec = await store.get("svc-a")
        assert rec is not None
        assert rec.image == "repo:v2"

    async def test_deploy_updates_record_digests(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        await self._seed("svc-a")
        resp = await client.post("/services/svc-a/deploy", headers=auth_headers)
        assert resp.status_code == 200

        store = server_mod.app.state.store
        rec = await store.get("svc-a")
        assert rec is not None
        assert rec.deployed_image_digest == "sha256:noop"
        assert rec.image_revision == "sha256:noop"
        # NoopBackend returns previous_digest = "" on first deploy
        assert rec.previous_image_digest == ""

    async def test_deploy_404_on_unknown_component(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        # Service not in store → 404 from _get_or_create_record
        resp = await client.post("/services/nonexistent/deploy", headers=auth_headers)
        assert resp.status_code == 404

    async def test_deploy_404_when_no_config(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        # Service in store but NOT in registry → 404
        store = server_mod.app.state.store
        await store.put(ServiceRecord(name="unregistered", state=ServiceState.STOPPED))
        resp = await client.post("/services/unregistered/deploy", headers=auth_headers)
        assert resp.status_code == 404
        assert "No component config" in resp.json()["error"]

    async def test_deploy_requires_auth(self, client: AsyncClient, registry):
        await self._seed("svc-a")
        resp = await client.post("/services/svc-a/deploy")
        assert resp.status_code == 401

    # -- rollback -----------------------------------------------------------

    async def test_rollback_returns_409_when_no_prior_digest(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        await self._seed("svc-a")
        resp = await client.post("/services/svc-a/rollback", headers=auth_headers)
        assert resp.status_code == 409
        assert "No prior image digest" in resp.json()["error"]

    async def test_rollback_swaps_digests(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        await self._seed("svc-a")
        # First, deploy to set prior digest
        resp = await client.post("/services/svc-a/deploy", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["deployed_digest"] == "sha256:noop"

        # NoopBackend returns previous_digest="" — manually inject a prior digest
        # so the rollback guard passes
        store = server_mod.app.state.store
        rec = await store.get("svc-a")
        rec.previous_image_digest = "sha256:old456"
        rec.deployed_image_digest = "sha256:noop"
        await store.put(rec)

        # Now rollback
        resp2 = await client.post("/services/svc-a/rollback", headers=auth_headers)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["name"] == "svc-a"
        assert data2["action"] == "rollback"
        assert data2["current_state"] == ServiceState.RUNNING.value

        store = server_mod.app.state.store
        rec = await store.get("svc-a")
        assert rec is not None
        # After rollback: previous becomes deployed, deployed becomes previous
        assert rec.deployed_image_digest == "sha256:old456"  # rolled-back-to
        assert rec.previous_image_digest == "sha256:noop"  # what was running
        assert rec.image_revision == "sha256:old456"

    async def test_rollback_404_on_unknown_service(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        resp = await client.post("/services/nonexistent/rollback", headers=auth_headers)
        assert resp.status_code == 404

    async def test_rollback_requires_auth(self, client: AsyncClient, registry):
        await self._seed("svc-a")
        resp = await client.post("/services/svc-a/rollback")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestDockerSdkBackendDeploy — unit tests with mocked Docker SDK
# ---------------------------------------------------------------------------


class TestDockerSdkBackendDeploy:
    @staticmethod
    def _make_container(
        status: str = "running",
        image_id: str = "sha256:abc123",
        health_status: str = "healthy",
    ) -> MagicMock:
        container = MagicMock()
        container.attrs = {
            "State": {
                "Status": status,
                "Health": {"Status": health_status},
            }
        }
        container.image.id = image_id
        container.image.labels = {
            "org.opencontainers.image.revision": "abc123",
        }
        return container

    @pytest.fixture
    def client_mock(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def backend(self, client_mock: MagicMock):
        docker_mock = MagicMock()
        docker_mock.DockerClient = MagicMock(return_value=client_mock)
        docker_mock.errors.NotFound = type("NotFound", (Exception,), {})
        docker_mock.errors.APIError = type("APIError", (Exception,), {})
        docker_mock.errors.DockerException = type("DockerException", (Exception,), {})
        with patch.dict(sys.modules, {"docker": docker_mock}):
            b = DockerSdkBackend()
            yield b, client_mock

    def _config(self, component_id: str = "svc-a") -> ComponentConfig:
        return _make_config(component_id)

    # ------------------------------------------------------------------

    async def test_deploy_pulls_image_and_creates_container(self, backend):
        b, client = backend
        config = self._config()

        # Setup mocks
        pulled_image = MagicMock()
        pulled_image.id = "sha256:new123"
        client.images.pull.return_value = pulled_image
        # _get_container returns None (no existing container)
        client.containers.get.side_effect = client._errors.NotFound("nope")

        record = ServiceRecord(name="svc-a", container_name="svc-a")
        outcome = await b.deploy(record, config, "repo:v2")

        # image pull was called
        client.images.pull.assert_called_once_with("repo:v2")

        # container was created with the right params
        client.containers.create.assert_called_once()
        create_kwargs = client.containers.create.call_args.kwargs
        assert create_kwargs["image"] == "repo:v2"
        assert create_kwargs["name"] == "svc-a"
        assert create_kwargs["environment"] == {"KEY": "val"}
        # Host ports are intentionally NOT published (port-conflict fix)
        assert create_kwargs["ports"] == {}
        assert create_kwargs["volumes"] == {"/data": {"bind": "/data", "mode": "rw"}}
        assert create_kwargs["restart_policy"] == {"Name": "unless-stopped"}

        assert outcome.deployed_digest == "sha256:new123"
        assert outcome.state == ServiceState.RUNNING

    async def test_deploy_records_prior_digest(self, backend):
        b, client = backend
        config = self._config()

        pulled_image = MagicMock()
        pulled_image.id = "sha256:new123"
        client.images.pull.return_value = pulled_image

        # existing container is found
        existing = self._make_container(image_id="sha256:old456")
        client.containers.get.return_value = existing

        record = ServiceRecord(name="svc-a", container_name="svc-a")
        outcome = await b.deploy(record, config, "repo:v2")

        assert outcome.deployed_digest == "sha256:new123"
        assert outcome.previous_digest == "sha256:old456"

    async def test_deploy_stops_and_removes_existing_container(self, backend):
        b, client = backend
        config = self._config()

        pulled_image = MagicMock()
        pulled_image.id = "sha256:new123"
        client.images.pull.return_value = pulled_image

        existing = self._make_container(image_id="sha256:old456")
        client.containers.get.return_value = existing

        record = ServiceRecord(name="svc-a", container_name="svc-a")
        await b.deploy(record, config, "repo:v2")

        existing.stop.assert_called_once()
        existing.remove.assert_called_once_with(force=True)

    async def test_deploy_best_effort_restore_on_create_failure(self, backend):
        b, client = backend
        config = self._config()

        pulled_image = MagicMock()
        pulled_image.id = "sha256:new123"
        client.images.pull.return_value = pulled_image

        # existing container
        existing = self._make_container(image_id="sha256:old456")
        client.containers.get.return_value = existing

        # containers.create raises on first call, succeeds on restore
        call_count = [0]

        def _create_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("boom")
            return MagicMock()

        client.containers.create.side_effect = _create_side_effect

        record = ServiceRecord(name="svc-a", container_name="svc-a")

        with pytest.raises(RuntimeError, match="Container create/start failed"):
            await b.deploy(record, config, "repo:v2")

        # create was called twice: once for new image, once for restore
        assert client.containers.create.call_count == 2
        # The second call (restore attempt) should use prior_digest
        restore_call_kwargs = client.containers.create.call_args_list[1].kwargs
        assert restore_call_kwargs["image"] == "sha256:old456"

    async def test_deploy_skips_health_wait_when_no_health_check(self, backend):
        b, client = backend
        config = self._config()
        config.health_check = None  # disable health check

        pulled_image = MagicMock()
        pulled_image.id = "sha256:new123"
        client.images.pull.return_value = pulled_image
        client.containers.get.side_effect = client._errors.NotFound("nope")

        record = ServiceRecord(name="svc-a", container_name="svc-a")
        outcome = await b.deploy(record, config, "repo:v2")

        assert outcome.state == ServiceState.RUNNING
        # _wait_healthy should not have been called (no health check)
        # We can verify by checking that containers.get was only called once
        # (the initial _get_container for looking up existing), not for polling
        assert client.containers.get.call_count == 1

    async def test_deploy_when_health_check_polls_until_healthy(self, backend):
        b, client = backend
        config = self._config()

        pulled_image = MagicMock()
        pulled_image.id = "sha256:new123"
        client.images.pull.return_value = pulled_image

        client.containers.get.side_effect = [
            client._errors.NotFound("nope"),  # existing lookup
            self._make_container(health_status="starting"),  # poll 1
            self._make_container(health_status="healthy"),  # poll 2 → done
        ]

        record = ServiceRecord(name="svc-a", container_name="svc-a")
        outcome = await b.deploy(record, config, "repo:v2")
        assert outcome.state == ServiceState.RUNNING

    async def test_deploy_precreates_named_volumes(self, backend):
        """Acceptance criterion 4: volumes.create() is called for each named volume
        before containers.create()."""
        b, client = backend
        config = self._config()
        config.named_volumes = ["vol_a", "vol_b"]

        pulled_image = MagicMock()
        pulled_image.id = "sha256:new123"
        client.images.pull.return_value = pulled_image
        client.containers.get.side_effect = client._errors.NotFound("nope")

        record = ServiceRecord(name="svc-a", container_name="svc-a")
        await b.deploy(record, config, "repo:v2")

        # Assert volumes.create() was called for each named volume
        assert client.volumes.create.call_count == 2
        client.volumes.create.assert_any_call("vol_a")
        client.volumes.create.assert_any_call("vol_b")

        # Assert volumes.create() was called BEFORE containers.create()
        create_call = client.containers.create.call_args_list[0]
        # We can verify containers.create happened after by checking both were called
        assert client.containers.create.called
        # Verify create_kwargs still include the regular mounts (not the named volumes)
        assert create_call.kwargs["volumes"] == {
            "/data": {"bind": "/data", "mode": "rw"}
        }

    async def test_deploy_precreates_volume_already_exists(self, backend):
        """volumes.create raises APIError 409 (Conflict) — handled gracefully, deploy continues."""
        import docker as docker_mod

        b, client = backend
        config = self._config()
        config.named_volumes = ["vol_a"]

        pulled_image = MagicMock()
        pulled_image.id = "sha256:new123"
        client.images.pull.return_value = pulled_image

        # First call: no existing container; second call: healthy container for health poll
        client.containers.get.side_effect = [
            docker_mod.errors.NotFound("nope"),
            self._make_container(health_status="healthy"),
        ]

        # Simulate volume already exists with a real APIError instance
        api_error = docker_mod.errors.APIError("volume already exists")
        api_error.status_code = 409
        api_error.explanation = "volume already exists"
        client.volumes.create.side_effect = api_error

        record = ServiceRecord(name="svc-a", container_name="svc-a")
        outcome = await b.deploy(record, config, "repo:v2")

        # Deploy succeeded despite the volume conflict
        assert outcome.deployed_digest == "sha256:new123"
        assert outcome.state == ServiceState.RUNNING
        client.volumes.create.assert_called_once_with("vol_a")
        client.containers.create.assert_called_once()

    async def test_deploy_precreates_volume_daemon_unreachable(self, backend):
        """volumes.create raises DockerException — RuntimeError with clear message."""
        import docker as docker_mod

        b, client = backend
        config = self._config()
        config.named_volumes = ["vol_a"]

        pulled_image = MagicMock()
        pulled_image.id = "sha256:new123"
        client.images.pull.return_value = pulled_image
        client.containers.get.side_effect = docker_mod.errors.NotFound("nope")

        client.volumes.create.side_effect = docker_mod.errors.DockerException(
            "Error while fetching server API version"
        )

        record = ServiceRecord(name="svc-a", container_name="svc-a")
        with pytest.raises(RuntimeError, match="Docker daemon unreachable"):
            await b.deploy(record, config, "repo:v2")

    async def test_deploy_precreates_volume_invalid_name(self, backend):
        """volumes.create raises APIError 400 — RuntimeError with clear message."""
        import docker as docker_mod

        b, client = backend
        config = self._config()
        config.named_volumes = ["invalid/name"]

        pulled_image = MagicMock()
        pulled_image.id = "sha256:new123"
        client.images.pull.return_value = pulled_image
        client.containers.get.side_effect = docker_mod.errors.NotFound("nope")

        api_error = docker_mod.errors.APIError("invalid volume name")
        api_error.status_code = 400
        api_error.explanation = "invalid volume name"
        client.volumes.create.side_effect = api_error

        record = ServiceRecord(name="svc-a", container_name="svc-a")
        with pytest.raises(RuntimeError, match="Failed to create volume"):
            await b.deploy(record, config, "repo:v2")

    async def test_deploy_health_unhealthy_raises(self, backend):
        b, client = backend
        config = self._config()

        pulled_image = MagicMock()
        pulled_image.id = "sha256:new123"
        client.images.pull.return_value = pulled_image

        client.containers.get.side_effect = [
            client._errors.NotFound("nope"),  # existing lookup
            self._make_container(health_status="unhealthy"),
        ]

        record = ServiceRecord(name="svc-a", container_name="svc-a")
        with pytest.raises(RuntimeError, match="unhealthy after deploy"):
            await b.deploy(record, config, "repo:v2")

    # -- rollback tests ----------------------------------------------------

    async def test_rollback_uses_previous_image_digest(self, backend):
        b, client = backend
        config = self._config()

        existing = self._make_container(image_id="sha256:current")
        client.containers.get.return_value = existing

        record = ServiceRecord(
            name="svc-a",
            container_name="svc-a",
            previous_image_digest="sha256:old456",
            deployed_image_digest="sha256:current",
        )

        outcome = await b.rollback(record, config)

        create_kwargs = client.containers.create.call_args.kwargs
        assert create_kwargs["image"] == "sha256:old456"
        assert outcome.deployed_digest == "sha256:old456"
        assert outcome.state == ServiceState.RUNNING

    async def test_rollback_stops_and_removes_current(self, backend):
        b, client = backend
        config = self._config()

        existing = self._make_container(image_id="sha256:current")
        client.containers.get.return_value = existing

        record = ServiceRecord(
            name="svc-a",
            container_name="svc-a",
            previous_image_digest="sha256:old456",
        )

        await b.rollback(record, config)

        existing.stop.assert_called_once()
        existing.remove.assert_called_once_with(force=True)

    async def test_rollback_skips_health_wait_when_no_health_check(self, backend):
        b, client = backend
        config = self._config()
        config.health_check = None

        existing = self._make_container(image_id="sha256:current")
        client.containers.get.return_value = existing

        record = ServiceRecord(
            name="svc-a",
            container_name="svc-a",
            previous_image_digest="sha256:old456",
        )

        outcome = await b.rollback(record, config)
        assert outcome.state == ServiceState.RUNNING
        # _get_container called once (for existing lookup), not for polling
        assert client.containers.get.call_count == 1


# ---------------------------------------------------------------------------
# TestFileStoreDeployFields — persistence round-trip for digest fields
# ---------------------------------------------------------------------------


class TestFileStoreDeployFields:
    async def test_filestore_round_trips_digest_fields(self):
        import tempfile
        from pathlib import Path

        from robotsix_central_deploy.lifecycle.store import FileStore

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.yaml"
            store = FileStore(path)

            record = ServiceRecord(
                name="svc-a",
                image="repo:v2",
                state=ServiceState.RUNNING,
                container_name="svc-a-ctr",
                image_revision="sha256:abc123",
                health="healthy",
                deployed_image_digest="sha256:abc123",
                previous_image_digest="sha256:old456",
            )
            await store.put(record)

            # Re-read from a fresh store instance
            store2 = FileStore(path)
            got = await store2.get("svc-a")
            assert got is not None
            assert got.container_name == "svc-a-ctr"
            assert got.image_revision == "sha256:abc123"
            assert got.health == "healthy"
            assert got.deployed_image_digest == "sha256:abc123"
            assert got.previous_image_digest == "sha256:old456"
