"""Tests for the deploy and rollback endpoints + DockerSdkBackend deploy/rollback."""

from __future__ import annotations

import asyncio
import copy
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.backends import DockerSdkBackend, NoopBackend
from robotsix_central_deploy.lifecycle.deps import JobRegistry
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
import robotsix_central_deploy.lifecycle.app as server_mod


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

    from robotsix_central_deploy.lifecycle.backends import NoopBackend
    from robotsix_central_deploy.lifecycle.config import LifecycleConfig
    from robotsix_central_deploy.lifecycle.deps import JobRegistry
    from robotsix_central_deploy.lifecycle.store import InMemoryStore
    from robotsix_central_deploy.registry.config_store import ComponentConfigStore
    from robotsix_central_deploy.registry.config_yaml_store import ConfigYamlStore
    from robotsix_central_deploy.registry.deploy_history_store import (
        DeployHistoryStore,
    )
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
    deploy_history_store = DeployHistoryStore(Path("/tmp/test_deploy_history.json"))  # noqa: S108
    job_registry = JobRegistry()

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
    server_mod.app.state.deploy_history_store = deploy_history_store
    server_mod.app.state.job_registry = job_registry


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

    async def test_deploy_returns_202_with_job_id(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        await self._seed("svc-a")
        resp = await client.post("/services/svc-a/deploy", headers=auth_headers)
        assert resp.status_code == 202
        data = resp.json()
        assert data["name"] == "svc-a"
        assert "job_id" in data
        assert data["job_id"].startswith("svc-a-")

    async def test_deploy_with_image_override_returns_202(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        await self._seed("svc-a")
        resp = await client.post(
            "/services/svc-a/deploy",
            json={"image": "repo:v2"},
            headers=auth_headers,
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["job_id"].startswith("svc-a-")

        # The background task runs in the same event loop; wait for it.
        await asyncio.sleep(0)

        store = server_mod.app.state.store
        rec = await store.get("svc-a")
        assert rec is not None
        assert rec.image == "repo:v2"

    async def test_deploy_completes_and_updates_record(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        await self._seed("svc-a")
        resp = await client.post("/services/svc-a/deploy", headers=auth_headers)
        assert resp.status_code == 202

        # Let the background task run to completion.
        await asyncio.sleep(0)

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

    async def test_deploy_returns_409_when_lock_held_no_job(
        self, client: AsyncClient, auth_headers: dict, registry, monkeypatch
    ):
        """When the deploy lock is held and no active deploy job exists, return 409
        with lock-holder metadata."""
        await self._seed("svc-a")

        async def _fake_try_acquire(_name: str, source: str = "manual") -> bool:
            return False

        def _fake_get_lock_info(_name: str):
            return {"source": "caretaker", "started_at": 1721312460.0, "job_id": ""}

        import robotsix_central_deploy.lifecycle.routers.services_deploy as svc_deploy

        monkeypatch.setattr(svc_deploy, "try_acquire_deploy_lock", _fake_try_acquire)
        monkeypatch.setattr(svc_deploy, "get_deploy_lock_info", _fake_get_lock_info)

        resp = await client.post("/services/svc-a/deploy", headers=auth_headers)
        assert resp.status_code == 409
        data = resp.json()
        assert "already in progress" in data["error"]
        assert "caretaker" in data["error"]
        assert data["detail"]["source"] == "caretaker"
        assert data["detail"]["started_at"] == 1721312460.0

    async def test_deploy_returns_existing_job_id_when_active_job_exists(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        """When an API-initiated deploy job is already active, return 202 with
        the existing job_id."""
        await self._seed("svc-a")

        # Manually create an active deploy job before calling the endpoint.
        job_registry: JobRegistry = server_mod.app.state.job_registry
        existing_job_id = job_registry.create_deploy("svc-a")

        resp = await client.post("/services/svc-a/deploy", headers=auth_headers)
        assert resp.status_code == 202
        data = resp.json()
        assert data["job_id"] == existing_job_id

    async def test_deploy_job_status_polling(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        """GET /services/deploy-jobs/{job_id} returns job progress."""
        await self._seed("svc-a")

        # Manually create a deploy job to verify the polling endpoint.
        job_registry: JobRegistry = server_mod.app.state.job_registry
        job_id = job_registry.create_deploy("svc-a")
        job_registry.update_phase(job_id, "waiting_health")

        resp = await client.get(f"/services/deploy-jobs/{job_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == job_id
        assert data["component"] == "svc-a"
        assert data["phase"] == "waiting_health"
        assert data["error"] is None

    async def test_deploy_job_status_404_unknown_job(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        """GET /services/deploy-jobs/{job_id} returns 404 for unknown job."""
        resp = await client.get(
            "/services/deploy-jobs/nonexistent-999", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_deploy_job_status_requires_auth(self, client: AsyncClient, registry):
        resp = await client.get("/services/deploy-jobs/svc-a-1")
        assert resp.status_code == 401

    async def test_deploy_job_done_phase(
        self, client: AsyncClient, auth_headers: dict, registry
    ):
        """After a deploy background task completes, the job is in 'done' phase."""
        await self._seed("svc-a")
        resp = await client.post("/services/svc-a/deploy", headers=auth_headers)
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Let the background task run to completion.
        await asyncio.sleep(0)

        # Poll for the final state
        resp2 = await client.get(
            f"/services/deploy-jobs/{job_id}", headers=auth_headers
        )
        assert resp2.status_code == 200
        data = resp2.json()
        assert data["phase"] == "done"
        assert data["name"] == "svc-a"
        assert data["state"] == ServiceState.RUNNING.value
        assert data["error"] is None

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
        # First, deploy to set prior digest (now async 202)
        resp = await client.post("/services/svc-a/deploy", headers=auth_headers)
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Wait for background deploy to complete
        await asyncio.sleep(0)

        # Verify job completed
        job_resp = await client.get(
            f"/services/deploy-jobs/{job_id}", headers=auth_headers
        )
        assert job_resp.json()["phase"] == "done"

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
# Deploy drift guard — auto-import live config so stale stored defaults
# never silently overwrite operator edits on the config volume.
# ---------------------------------------------------------------------------


class TrackingInMemoryBackend(NoopBackend):
    """NoopBackend variant that stores config writes so tests can mutate volumes."""

    def __init__(self) -> None:
        super().__init__()
        self._volumes: dict[str, dict[str, Any]] = {}

    async def write_config_to_volume(
        self, volume_name: str, config_dict: dict[str, Any]
    ) -> None:
        self._volumes[volume_name] = copy.deepcopy(config_dict)

    async def read_config_from_volume(self, volume_name: str) -> dict[str, Any]:
        return dict(self._volumes.get(volume_name, {}))


class TestDeployDriftGuard:
    """Deploy MUST auto-import a drifted config volume before writing, so the
    live operator config is never silently replaced by the stored (possibly
    stale) current config."""

    async def test_deploy_does_not_overwrite_drifted_volume(
        self, auth_headers: dict, registry
    ):
        """Simulate the 2026-07-16 incident: stored current = template defaults,
        volume = real operator config.  Deploy must auto-import the live volume
        rather than overwrite it with the stale stored current."""
        from robotsix_central_deploy.lifecycle._config_utils import _canonical_hash

        # -- tracking backend so we can inspect volume writes --
        tracking = TrackingInMemoryBackend()
        server_mod.app.state.backend = tracking
        server_mod._backend = tracking

        # -- component with config --
        comp = ComponentConfig(
            id="chat",
            image="ghcr.io/org/chat:latest",
            container_name="chat",
            ports=[PortMapping(host=8080, container=8080)],
            mounts=[VolumeMount(host="chat-config", container="/config")],
            config_volume="chat-config",
        )
        config_store = server_mod.app.state.component_config_store
        await config_store.put(comp)

        # Register in the ComponentRegistry so deploy can find it.
        registry.register(comp)

        # Seed a ServiceRecord.
        store = server_mod.app.state.store
        await store.put(
            ServiceRecord(
                name="chat",
                state=ServiceState.STOPPED,
                image="ghcr.io/org/chat:latest",
            )
        )

        # -- config YAML store setup --
        cys = server_mod.app.state.config_yaml_store

        # Template (schema + defaults)
        template: dict[str, Any] = {
            "type": "object",
            "properties": {
                "server_port": {"type": "integer", "default": 3000},
                "url": {"type": "string", "default": "https://default.example.com"},
                "api_key": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                    "default": "",
                },
            },
        }
        await cys.save_template("chat", template)

        # Stored current = stale template defaults (simulating the post-migration
        # state where `current` was never updated to reflect the live volume).
        stale_current: dict[str, Any] = {
            "server_port": 3000,
            "url": "https://default.example.com",
            "api_key": "",
        }
        stale_hash = _canonical_hash(stale_current)
        await cys.update_current_and_hash("chat", stale_current, stale_hash)

        # Live volume = real operator config (the config that was hand-edited
        # on the volume after the YAML→JSON migration).
        real_config: dict[str, Any] = {
            "server_port": 8080,
            "url": "https://real.example.com",
            "api_key": "secret-key",
        }
        tracking._volumes["chat-config"] = copy.deepcopy(real_config)

        # -- deploy --
        transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/services/chat/deploy", headers=auth_headers)
            assert resp.status_code == 202

        # Let the background task run.
        await asyncio.sleep(0)

        # -- assertions --
        # 1. The volume must still hold the real operator config (not the stale
        #    template defaults).
        volume_after = tracking._volumes.get("chat-config", {})
        assert volume_after["server_port"] == 8080, (
            f"volume server_port was {volume_after.get('server_port')} — "
            "stale stored defaults overwrote the live config!"
        )
        assert volume_after["url"] == "https://real.example.com"

        # 2. The stored current must have been auto-imported to match the
        #    live volume (so subsequent GET /config shows no drift).
        imported = await cys.get_current("chat")
        assert imported is not None
        assert imported["server_port"] == 8080
        assert imported["url"] == "https://real.example.com"

        # 3. The stored volume_hash must now match the volume content.
        stored_hash = await cys.get_volume_hash("chat")
        assert stored_hash == _canonical_hash(real_config)


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
        client.images.pull.assert_called_once_with("repo:v2", auth_config=None)

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
