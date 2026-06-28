"""Integration tests for the lifecycle REST server.

Uses ``httpx.AsyncClient`` against a FastAPI test transport so we exercise the
full request/response pipeline including middleware, auth, and error handlers.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from unittest.mock import AsyncMock, MagicMock

from robotsix_central_deploy.lifecycle.backend import ComponentInspect, NoopBackend
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.config_yaml_store import ConfigYamlStore
from robotsix_central_deploy.registry.env_store import EnvStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import ComponentConfig
from robotsix_central_deploy.registry.secret_key import SecretKeyManager

# Import the server module itself (not just symbols) so we can set its globals.
from robotsix_central_deploy.lifecycle import server as server_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_store(*names: str, image: str = "", deployed_digest: str = "") -> None:
    """Populate the server's store with records for testing."""
    s = server_mod.app.state.store
    assert s is not None
    for name in names:
        rec = ServiceRecord(name=name, state=ServiceState.STOPPED, image=image or f"{name}:latest")
        if deployed_digest:
            rec.deployed_image_digest = deployed_digest
        await s.put(rec)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch, tmp_path):
    """Wire a fresh store/backend/config into the server module before each test."""
    monkeypatch.setenv("ROBOTSIX_LIFECYCLE_API_KEY", "test-key")
    cfg = LifecycleConfig(  # type: ignore[call-arg]
        store_backend="memory",
        execution_backend="noop",
        api_key="test-key",
    )
    store = InMemoryStore()
    backend = NoopBackend()

    # Registry checker mock
    mock_checker = MagicMock()
    mock_checker.get_latest_digest = AsyncMock(return_value=None)

    # Env store + secret key
    km = SecretKeyManager(tmp_path / "secrets.key")
    env_store = EnvStore(tmp_path / "env.json", km)

    # Config store + registry
    config_store = ComponentConfigStore(tmp_path / "config_store.json")
    config_yaml_store = ConfigYamlStore(tmp_path / "config_yaml.json")
    registry = ComponentRegistry([])

    # Set both the module-level globals and app.state so all code paths work.
    server_mod._config = cfg
    server_mod._store = store
    server_mod._backend = backend
    server_mod._registry_checker = mock_checker
    server_mod.app.state.config = cfg
    server_mod.app.state.store = store
    server_mod.app.state.backend = backend
    server_mod.app.state.registry_checker = mock_checker
    server_mod.app.state.key_manager = km
    server_mod.app.state.env_store = env_store
    server_mod.app.state.config_yaml_store = config_yaml_store
    server_mod.app.state.component_config_store = config_store
    server_mod.app.state.registry = registry


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_health_returns_ok(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# GET /services
# ---------------------------------------------------------------------------


class TestListServices:
    async def test_empty_list(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/services", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"services": []}

    async def test_list_with_items(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a", "svc-b")
        resp = await client.get("/services", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        names = {s["name"] for s in data["services"]}
        assert names == {"svc-a", "svc-b"}
        for s in data["services"]:
            assert s["state"] in {e.value for e in ServiceState}


# ---------------------------------------------------------------------------
# GET /services/{name}
# ---------------------------------------------------------------------------


class TestGetStatus:
    async def test_not_found(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/services/nonexistent", headers=auth_headers)
        assert resp.status_code == 404

    async def test_returns_status(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        resp = await client.get("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "svc-a"
        assert data["state"] in {e.value for e in ServiceState}
        assert "image" in data


# ---------------------------------------------------------------------------
# GET /services/{name}/health
# ---------------------------------------------------------------------------


class TestGetServiceHealth:
    async def test_not_found(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/services/nonexistent/health", headers=auth_headers)
        assert resp.status_code == 404

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        await _seed_store("svc-a")
        resp = await client.get("/services/svc-a/health")
        assert resp.status_code == 401

    async def test_no_health_check_returns_unknown(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        # NoopBackend.status returns ComponentInspect with health="" by default
        resp = await client.get("/services/svc-a/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"name": "svc-a", "health": "unknown"}

    async def test_healthy_container(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("svc-a")
        inspect = ComponentInspect(state=ServiceState.RUNNING, health="healthy")
        mock_status = AsyncMock(return_value=inspect)
        monkeypatch.setattr(server_mod.app.state.backend, "status", mock_status)

        resp = await client.get("/services/svc-a/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"name": "svc-a", "health": "healthy"}

    async def test_unhealthy_container(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("svc-a")
        inspect = ComponentInspect(state=ServiceState.RUNNING, health="unhealthy")
        mock_status = AsyncMock(return_value=inspect)
        monkeypatch.setattr(server_mod.app.state.backend, "status", mock_status)

        resp = await client.get("/services/svc-a/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"name": "svc-a", "health": "unhealthy"}


# ---------------------------------------------------------------------------
# POST /services/{name}/start
# ---------------------------------------------------------------------------


class TestStart:
    async def test_start_stopped_service(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        resp = await client.post("/services/svc-a/start", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "start"
        assert data["previous_state"] == ServiceState.STOPPED.value
        assert data["current_state"] == ServiceState.RUNNING.value

    async def test_start_already_running_is_idempotent(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.RUNNING
        await s.put(rec)

        resp = await client.post("/services/svc-a/start", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_state"] == ServiceState.RUNNING.value
        assert "already running" in data["detail"]

    async def test_start_not_found(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/services/nonexistent/start", headers=auth_headers)
        assert resp.status_code == 404

    async def test_start_from_failed_state(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.FAILED
        await s.put(rec)

        resp = await client.post("/services/svc-a/start", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["previous_state"] == ServiceState.FAILED.value
        assert data["current_state"] == ServiceState.RUNNING.value

    async def test_start_conflict_from_stopping(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.STOPPING
        await s.put(rec)

        resp = await client.post("/services/svc-a/start", headers=auth_headers)
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /services/{name}/stop
# ---------------------------------------------------------------------------


class TestStop:
    async def test_stop_running_service(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.RUNNING
        await s.put(rec)

        resp = await client.post("/services/svc-a/stop", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "stop"
        assert data["previous_state"] == ServiceState.RUNNING.value
        assert data["current_state"] == ServiceState.STOPPED.value

    async def test_stop_already_stopped_is_idempotent(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        resp = await client.post("/services/svc-a/stop", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_state"] == ServiceState.STOPPED.value
        assert "already stopped" in data["detail"]

    async def test_stop_not_found(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/services/nonexistent/stop", headers=auth_headers)
        assert resp.status_code == 404

    async def test_stop_conflict_from_starting(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.STARTING
        await s.put(rec)

        resp = await client.post("/services/svc-a/stop", headers=auth_headers)
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /services/{name}/restart
# ---------------------------------------------------------------------------


class TestRestart:
    async def test_restart_running_service(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.RUNNING
        await s.put(rec)

        resp = await client.post("/services/svc-a/restart", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "restart"
        assert data["previous_state"] == ServiceState.RUNNING.value
        assert data["current_state"] == ServiceState.RUNNING.value

    async def test_restart_already_restarting_is_idempotent(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.RESTARTING
        await s.put(rec)

        resp = await client.post("/services/svc-a/restart", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_state"] == ServiceState.RESTARTING.value
        assert "already in progress" in data["detail"]

    async def test_restart_not_found(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/services/nonexistent/restart", headers=auth_headers)
        assert resp.status_code == 404

    async def test_restart_conflict_from_stopped(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        resp = await client.post("/services/svc-a/restart", headers=auth_headers)
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# GET /services/{name}/logs
# ---------------------------------------------------------------------------


class TestLogsEndpoint:
    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.get("/services/svc-a/logs")
        assert resp.status_code == 401

    async def test_unknown_service_returns_404(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/services/nonexistent/logs", headers=auth_headers)
        assert resp.status_code == 404

    async def test_noop_backend_returns_stub_body(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        resp = await client.get("/services/svc-a/logs", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        body = resp.content
        assert b"[noop backend]\n" in body

    async def test_query_params_forwarded_to_backend(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")

        captured: list[dict] = []

        async def _fake_stream(service, tail=100, since=None, follow=False):
            captured.append({"tail": tail, "since": since, "follow": follow})
            yield f"tail={tail} since={since}".encode()

        original = server_mod.app.state.backend.stream_logs
        server_mod.app.state.backend.stream_logs = _fake_stream
        try:
            resp = await client.get(
                "/services/svc-a/logs?tail=50&since=1700000000",
                headers=auth_headers,
            )
            assert resp.status_code == 200
            assert b"tail=50 since=1700000000" in resp.content
            assert len(captured) == 1
            assert captured[0] == {"tail": 50, "since": "1700000000", "follow": False}
        finally:
            server_mod.app.state.backend.stream_logs = original

    async def test_follow_param_forwarded_to_backend(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        captured: list[dict] = []

        async def _fake_stream(service, tail=100, since=None, follow=False):
            captured.append({"tail": tail, "since": since, "follow": follow})
            yield b"live line\n"

        original = server_mod.app.state.backend.stream_logs
        server_mod.app.state.backend.stream_logs = _fake_stream
        try:
            resp = await client.get(
                "/services/svc-a/logs?follow=true",
                headers=auth_headers,
            )
            assert resp.status_code == 200
            assert len(captured) == 1
            assert captured[0]["follow"] is True
        finally:
            server_mod.app.state.backend.stream_logs = original

    async def test_tail_out_of_range_returns_422(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        resp = await client.get("/services/svc-a/logs?tail=0", headers=auth_headers)
        assert resp.status_code == 422
        resp2 = await client.get("/services/svc-a/logs?tail=10001", headers=auth_headers)
        assert resp2.status_code == 422


# ---------------------------------------------------------------------------
# Update available (registry check)
# ---------------------------------------------------------------------------


class TestUpdateAvailable:
    async def test_up_to_date_when_digests_match(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("svc-a", image="ghcr.io/o/img:main", deployed_digest="sha256:aaa")
        mock_checker = MagicMock()
        mock_checker.get_latest_digest = AsyncMock(return_value="sha256:aaa")
        monkeypatch.setattr(server_mod.app.state, "registry_checker", mock_checker)
        resp = await client.get("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["update_available"] is False
        assert data["update_state"] == "up-to-date"
        assert data["running_digest"] == "sha256:aaa"
        assert data["latest_digest"] == "sha256:aaa"

    async def test_update_available_when_digests_differ(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("svc-a", image="ghcr.io/o/img:main", deployed_digest="sha256:aaa")
        mock_checker = MagicMock()
        mock_checker.get_latest_digest = AsyncMock(return_value="sha256:bbb")
        monkeypatch.setattr(server_mod.app.state, "registry_checker", mock_checker)
        resp = await client.get("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["update_available"] is True
        assert data["update_state"] == "update-available"
        assert data["running_digest"] == "sha256:aaa"
        assert data["latest_digest"] == "sha256:bbb"

    async def test_registry_unreachable_degrades_gracefully(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("svc-a", image="ghcr.io/o/img:main", deployed_digest="sha256:aaa")
        mock_checker = MagicMock()
        mock_checker.get_latest_digest = AsyncMock(return_value=None)
        monkeypatch.setattr(server_mod.app.state, "registry_checker", mock_checker)
        resp = await client.get("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["update_available"] is False
        assert data["update_state"] == "unknown"
        assert data["latest_digest"] == ""

    async def test_list_returns_update_available_field(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        resp = await client.get("/services", headers=auth_headers)
        assert resp.status_code == 200
        items = resp.json()["services"]
        assert all("update_available" in item for item in items)

    async def test_running_digest_populated_from_inspect(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("svc-a", image="ghcr.io/o/img:main", deployed_digest="")
        inspect = ComponentInspect(
            state=ServiceState.RUNNING, running_digest="sha256:e9f0"
        )
        monkeypatch.setattr(
            server_mod.app.state.backend, "status",
            AsyncMock(return_value=inspect),
        )
        resp = await client.get("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["running_digest"] == "sha256:e9f0"
        assert data["update_state"] == "unknown"  # latest not yet fetched


# ---------------------------------------------------------------------------
# GET / PUT / DELETE /services/{name}/env
# ---------------------------------------------------------------------------


class TestEnvEndpoints:
    async def test_get_env_empty_for_fresh_component(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        resp = await client.get("/services/chat/env", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"env": {}, "secrets": {}}

    async def test_put_then_get_returns_env_and_masked_secrets(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        put_body = {"env": {"LOG_LEVEL": "debug"}, "secrets": {"API_KEY": "my-token"}}
        r = await client.put("/services/chat/env", json=put_body, headers=auth_headers)
        assert r.status_code == 204

        r = await client.get("/services/chat/env", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["env"] == {"LOG_LEVEL": "debug"}
        assert data["secrets"] == {"API_KEY": "***"}

    async def test_put_merges_not_replaces(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        await client.put("/services/chat/env", json={"env": {"A": "1"}}, headers=auth_headers)
        await client.put("/services/chat/env", json={"env": {"B": "2"}}, headers=auth_headers)
        r = await client.get("/services/chat/env", headers=auth_headers)
        data = r.json()
        assert data["env"] == {"A": "1", "B": "2"}

    async def test_get_env_nonexistent_service_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get("/services/nonexistent/env", headers=auth_headers)
        assert resp.status_code == 404

    async def test_unauthenticated_get_returns_401(self, client: AsyncClient):
        await _seed_store("chat")
        resp = await client.get("/services/chat/env")
        assert resp.status_code == 401

    async def test_unauthenticated_put_returns_401(self, client: AsyncClient):
        await _seed_store("chat")
        resp = await client.put("/services/chat/env", json={"env": {"A": "1"}})
        assert resp.status_code == 401

    async def test_unauthenticated_delete_returns_401(self, client: AsyncClient):
        await _seed_store("chat")
        resp = await client.delete("/services/chat/env/A")
        assert resp.status_code == 401

    async def test_delete_key_removes_from_env(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        await client.put("/services/chat/env", json={"env": {"A": "1", "B": "2"}}, headers=auth_headers)
        r = await client.delete("/services/chat/env/A", headers=auth_headers)
        assert r.status_code == 204
        r = await client.get("/services/chat/env", headers=auth_headers)
        data = r.json()
        assert data["env"] == {"B": "2"}

    async def test_delete_key_removes_from_secrets(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        await client.put("/services/chat/env", json={"secrets": {"TOKEN": "val"}}, headers=auth_headers)
        r = await client.delete("/services/chat/env/TOKEN", headers=auth_headers)
        assert r.status_code == 204
        r = await client.get("/services/chat/env", headers=auth_headers)
        data = r.json()
        assert data["secrets"] == {}

    async def test_delete_absent_key_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        await client.put("/services/chat/env", json={"env": {"A": "1"}}, headers=auth_headers)
        r = await client.delete("/services/chat/env/NOTFOUND", headers=auth_headers)
        assert r.status_code == 404

    async def test_deploy_injects_merged_env(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """deploy_service must call backend.deploy with merged env including secrets."""
        await _seed_store("chat", image="ghcr.io/o/img:main")

        # Set up a fake registry with a component config that has a base env
        from robotsix_central_deploy.registry.loader import ComponentRegistry
        cfg = ComponentConfig(
            id="chat",
            image="ghcr.io/o/img:main",
            container_name="chat",
            env={"BASE_KEY": "base-val", "OVERRIDE": "base"},
        )
        registry = ComponentRegistry([cfg])
        server_mod.app.state.registry = registry

        # Store a secret and an env override via the API
        await client.put(
            "/services/chat/env",
            json={"env": {"OVERRIDE": "user-val"}, "secrets": {"SECRET": "s3cret"}},
            headers=auth_headers,
        )

        # Monkeypatch backend.deploy to capture the config
        captured_configs: list = []
        original_deploy = server_mod.app.state.backend.deploy

        async def _fake_deploy(service, config, image_ref):
            captured_configs.append(config)
            return await original_deploy(service, config, image_ref)

        monkeypatch.setattr(server_mod.app.state.backend, "deploy", _fake_deploy)

        r = await client.post("/services/chat/deploy", headers=auth_headers)
        assert r.status_code == 200

        assert len(captured_configs) == 1
        deployed_env = captured_configs[0].env
        assert deployed_env == {
            "BASE_KEY": "base-val",
            "OVERRIDE": "user-val",
            "SECRET": "s3cret",
        }


# ---------------------------------------------------------------------------
# DELETE /services/{name}
# ---------------------------------------------------------------------------


async def _seed_config(
    config_store: ComponentConfigStore, name: str, *, siblings: list = None
) -> ComponentConfig:
    """Create and persist a ComponentConfig in the config store, plus register it."""
    cfg = ComponentConfig(
        id=name,
        image=f"{name}:latest",
        container_name=name,
        siblings=siblings or [],
    )
    await config_store.put(cfg)
    server_mod.app.state.registry.register(cfg)
    return cfg


class TestDeleteService:
    async def test_nonexistent_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.delete("/services/nonexistent", headers=auth_headers)
        assert resp.status_code == 404

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.delete("/services/svc-a")
        assert resp.status_code == 401

    async def test_delete_existing_returns_204(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")
        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204

    async def test_after_delete_service_list_excludes_name(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")
        await client.delete("/services/svc-a", headers=auth_headers)

        resp = await client.get("/services", headers=auth_headers)
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()["services"]}
        assert "svc-a" not in names

    async def test_after_delete_get_service_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")
        await client.delete("/services/svc-a", headers=auth_headers)

        resp = await client.get("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 404

    async def test_after_delete_env_store_cleared(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        env_store = server_mod.app.state.env_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")

        # Put some env data
        await env_store.upsert("svc-a", {"FOO": "bar"}, {})

        await client.delete("/services/svc-a", headers=auth_headers)

        # Env should be empty
        cfg = await env_store.get("svc-a")
        assert cfg.env == {}
        assert cfg.secret_tokens == {}

    async def test_stop_container_false_backend_not_called(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")

        stop_called = False
        remove_called = False
        original_stop = server_mod.app.state.backend.stop
        original_remove = server_mod.app.state.backend.remove_container

        async def _fake_stop(service):
            nonlocal stop_called
            stop_called = True
            return await original_stop(service)

        async def _fake_remove(service):
            nonlocal remove_called
            remove_called = True
            return await original_remove(service)

        monkeypatch.setattr(server_mod.app.state.backend, "stop", _fake_stop)
        monkeypatch.setattr(server_mod.app.state.backend, "remove_container", _fake_remove)

        resp = await client.delete(
            "/services/svc-a?stop_container=false", headers=auth_headers
        )
        assert resp.status_code == 204
        assert not stop_called
        assert not remove_called

    async def test_stop_container_true_calls_backend(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")

        stop_called = False
        remove_called = False
        original_stop = server_mod.app.state.backend.stop
        original_remove = server_mod.app.state.backend.remove_container

        async def _fake_stop(service):
            nonlocal stop_called
            stop_called = True
            return await original_stop(service)

        async def _fake_remove(service):
            nonlocal remove_called
            remove_called = True
            return await original_remove(service)

        monkeypatch.setattr(server_mod.app.state.backend, "stop", _fake_stop)
        monkeypatch.setattr(server_mod.app.state.backend, "remove_container", _fake_remove)

        # Default stop_container=true
        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204
        assert stop_called
        assert remove_called

    async def test_backend_stop_error_does_not_abort(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")

        async def _failing_stop(service):
            raise RuntimeError("docker daemon down")

        monkeypatch.setattr(server_mod.app.state.backend, "stop", _failing_stop)

        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204

    async def test_delete_with_sibling(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        from robotsix_central_deploy.registry.models import ServiceConfig

        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store
        env_store = server_mod.app.state.env_store

        # Create a component with a sibling
        sibling = ServiceConfig(
            service_key="redis",
            container_name="svc-a-redis",
            image="redis:7",
        )
        cfg = ComponentConfig(
            id="svc-a",
            image="svc-a:latest",
            container_name="svc-a",
            siblings=[sibling],
        )
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        # Seed primary and sibling records
        prim = ServiceRecord(name="svc-a", image="svc-a:latest")
        sib_rec = ServiceRecord(name="svc-a-redis", image="redis:7")
        await store.put(prim)
        await store.put(sib_rec)

        # Put env for both
        await env_store.upsert("svc-a", {"PRIMARY": "1"}, {})
        await env_store.upsert("svc-a-redis", {"SIBLING": "1"}, {})

        # Track backend calls
        stop_names: list[str] = []
        remove_names: list[str] = []
        original_stop = server_mod.app.state.backend.stop
        original_remove = server_mod.app.state.backend.remove_container

        async def _fake_stop(service):
            stop_names.append(service.name)
            return await original_stop(service)

        async def _fake_remove(service):
            remove_names.append(service.name)
            return await original_remove(service)

        monkeypatch.setattr(server_mod.app.state.backend, "stop", _fake_stop)
        monkeypatch.setattr(server_mod.app.state.backend, "remove_container", _fake_remove)

        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204

        # Both containers were stopped and removed
        assert "svc-a" in stop_names
        assert "svc-a-redis" in stop_names
        assert "svc-a" in remove_names
        assert "svc-a-redis" in remove_names

        # Both records are gone
        assert await store.get("svc-a") is None
        assert await store.get("svc-a-redis") is None

        # Both env entries are cleared
        prim_env = await env_store.get("svc-a")
        sib_env = await env_store.get("svc-a-redis")
        assert prim_env.env == {}
        assert sib_env.env == {}

        # Config is gone
        assert config_store.get("svc-a") is None

        # Registry entry is gone
        assert server_mod.app.state.registry.get("svc-a") is None


# ---------------------------------------------------------------------------
# Lifecycle sibling fan-out
# ---------------------------------------------------------------------------


class TestStartWithSibling:
    async def test_start_propagates_to_sibling(
        self, client: AsyncClient, auth_headers: dict
    ):
        """start_service fans out to sibling records — both transition STOPPED→RUNNING."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store

        sibling = ServiceConfig(
            service_key="redis",
            container_name="svc-a-redis",
            image="redis:7",
        )
        cfg = ComponentConfig(
            id="svc-a",
            image="svc-a:latest",
            container_name="svc-a",
            siblings=[sibling],
        )
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        prim = ServiceRecord(name="svc-a", image="svc-a:latest", state=ServiceState.STOPPED)
        sib_rec = ServiceRecord(name="svc-a-redis", image="redis:7", state=ServiceState.STOPPED)
        await store.put(prim)
        await store.put(sib_rec)

        resp = await client.post("/services/svc-a/start", headers=auth_headers)
        assert resp.status_code == 200

        # Primary transitioned
        prim_after = await store.get("svc-a")
        assert prim_after.state == ServiceState.RUNNING

        # Sibling transitioned
        sib_after = await store.get("svc-a-redis")
        assert sib_after.state == ServiceState.RUNNING


class TestStopWithSibling:
    async def test_stop_propagates_to_sibling(
        self, client: AsyncClient, auth_headers: dict
    ):
        """stop_service fans out to sibling records — both transition RUNNING→STOPPED."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store

        sibling = ServiceConfig(
            service_key="redis",
            container_name="svc-a-redis",
            image="redis:7",
        )
        cfg = ComponentConfig(
            id="svc-a",
            image="svc-a:latest",
            container_name="svc-a",
            siblings=[sibling],
        )
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        prim = ServiceRecord(name="svc-a", image="svc-a:latest", state=ServiceState.RUNNING)
        sib_rec = ServiceRecord(name="svc-a-redis", image="redis:7", state=ServiceState.RUNNING)
        await store.put(prim)
        await store.put(sib_rec)

        resp = await client.post("/services/svc-a/stop", headers=auth_headers)
        assert resp.status_code == 200

        prim_after = await store.get("svc-a")
        assert prim_after.state == ServiceState.STOPPED

        sib_after = await store.get("svc-a-redis")
        assert sib_after.state == ServiceState.STOPPED


class TestRestartWithSibling:
    async def test_restart_propagates_to_sibling(
        self, client: AsyncClient, auth_headers: dict
    ):
        """restart_service fans out to sibling records — both stay RUNNING after restart."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store

        sibling = ServiceConfig(
            service_key="redis",
            container_name="svc-a-redis",
            image="redis:7",
        )
        cfg = ComponentConfig(
            id="svc-a",
            image="svc-a:latest",
            container_name="svc-a",
            siblings=[sibling],
        )
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        prim = ServiceRecord(name="svc-a", image="svc-a:latest", state=ServiceState.RUNNING)
        sib_rec = ServiceRecord(name="svc-a-redis", image="redis:7", state=ServiceState.RUNNING)
        await store.put(prim)
        await store.put(sib_rec)

        resp = await client.post("/services/svc-a/restart", headers=auth_headers)
        assert resp.status_code == 200

        prim_after = await store.get("svc-a")
        assert prim_after.state == ServiceState.RUNNING

        sib_after = await store.get("svc-a-redis")
        assert sib_after.state == ServiceState.RUNNING


class TestDeployWithSibling:
    async def test_deploy_propagates_to_sibling(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """deploy_service fans out to siblings — backend.deploy called for both primary and sibling."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store

        sibling = ServiceConfig(
            service_key="redis",
            container_name="svc-a-redis",
            image="redis:7",
        )
        cfg = ComponentConfig(
            id="svc-a",
            image="svc-a:latest",
            container_name="svc-a",
            siblings=[sibling],
        )
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        prim = ServiceRecord(name="svc-a", image="svc-a:latest")
        sib_rec = ServiceRecord(name="svc-a-redis", image="redis:7")
        await store.put(prim)
        await store.put(sib_rec)

        # Capture backend.deploy calls
        deploy_names: list[str] = []
        original_deploy = server_mod.app.state.backend.deploy

        async def _fake_deploy(service, config, image_ref):
            deploy_names.append(service.name)
            return await original_deploy(service, config, image_ref)

        monkeypatch.setattr(server_mod.app.state.backend, "deploy", _fake_deploy)

        resp = await client.post("/services/svc-a/deploy", headers=auth_headers)
        assert resp.status_code == 200

        # Both primary and sibling were deployed
        assert "svc-a" in deploy_names
        assert "svc-a-redis" in deploy_names

        # Sibling record updated with deploy outcome
        sib_after = await store.get("svc-a-redis")
        assert sib_after.state == ServiceState.RUNNING
        assert sib_after.image == "redis:7"
        assert sib_after.deployed_image_digest == "sha256:noop"


class TestRollbackWithSibling:
    async def test_rollback_propagates_to_sibling(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """rollback_service fans out to siblings — backend.rollback called, digests swapped."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store

        sibling = ServiceConfig(
            service_key="redis",
            container_name="svc-a-redis",
            image="redis:7",
        )
        cfg = ComponentConfig(
            id="svc-a",
            image="svc-a:latest",
            container_name="svc-a",
            siblings=[sibling],
        )
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        prim = ServiceRecord(
            name="svc-a",
            image="svc-a:latest",
            deployed_image_digest="sha256:current",
            previous_image_digest="sha256:prior",
        )
        sib_rec = ServiceRecord(
            name="svc-a-redis",
            image="redis:7",
            deployed_image_digest="sha256:sib-current",
            previous_image_digest="sha256:sib-prior",
        )
        await store.put(prim)
        await store.put(sib_rec)

        # Capture backend.rollback calls
        rollback_names: list[str] = []
        original_rollback = server_mod.app.state.backend.rollback

        async def _fake_rollback(service, config):
            rollback_names.append(service.name)
            return await original_rollback(service, config)

        monkeypatch.setattr(server_mod.app.state.backend, "rollback", _fake_rollback)

        resp = await client.post("/services/svc-a/rollback", headers=auth_headers)
        assert resp.status_code == 200

        # Both primary and sibling were rolled back
        assert "svc-a" in rollback_names
        assert "svc-a-redis" in rollback_names

        # Sibling digests swapped
        sib_after = await store.get("svc-a-redis")
        assert sib_after.state == ServiceState.RUNNING
        assert sib_after.deployed_image_digest == "sha256:sib-prior"
        assert sib_after.previous_image_digest == "sha256:sib-current"
        assert sib_after.image_revision == "sha256:sib-prior"


# ---------------------------------------------------------------------------
# GET /services/{name}/config
# ---------------------------------------------------------------------------


class TestGetServiceConfig:
    async def test_returns_schema_and_masked_current(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {"host": "localhost", "password": ""}
        await store.save_template("chat", template)
        await store.update_current("chat", {"host": "0.0.0.0", "password": "realpass"})

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "schema" in data
        assert data["schema"] == template
        assert "current" in data
        assert data["current"]["host"] == "0.0.0.0"
        assert data["current"]["password"] == "***"

    async def test_returns_template_as_current_when_no_current_stored(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {"host": "localhost", "port": 8080}
        await store.save_template("chat", template)

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["schema"] == template
        assert data["current"] == template  # no current stored → template is current

    async def test_no_config_schema_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        # No template saved for "chat"

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 404
        assert "No config schema" in resp.json()["error"]

    async def test_nonexistent_service_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get("/services/nonexistent/config", headers=auth_headers)
        assert resp.status_code == 404
        # Service not found takes priority
        assert "not found" in resp.json()["error"].lower()

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.get("/services/chat/config")
        assert resp.status_code == 401

    async def test_nested_secrets_are_masked(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {"server": {"host": "localhost", "password": ""}}
        await store.save_template("chat", template)
        await store.update_current("chat", {"server": {"host": "0.0.0.0", "password": "s3cret"}})

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current"]["server"]["host"] == "0.0.0.0"
        assert data["current"]["server"]["password"] == "***"

    async def test_null_template_secret_is_masked(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {"api_key": None}
        await store.save_template("chat", template)
        await store.update_current("chat", {"api_key": "real-key"})

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current"]["api_key"] == "***"


# ---------------------------------------------------------------------------
# PUT /services/{name}/config
# ---------------------------------------------------------------------------


class TestPutServiceConfig:
    async def test_merge_and_return_204(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {"host": "localhost", "port": 8080}
        await store.save_template("chat", template)

        resp = await client.put(
            "/services/chat/config",
            json={"values": {"host": "10.0.0.1", "port": 3000}},
            headers=auth_headers,
        )
        assert resp.status_code == 204

        current = await store.get_current("chat")
        assert current == {"host": "10.0.0.1", "port": 3000}

    async def test_preserves_masked_secret(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {"host": "localhost", "password": ""}
        await store.save_template("chat", template)
        await store.update_current("chat", {"host": "localhost", "password": "realpass"})

        # Submit "***" for the secret — existing value should be preserved
        resp = await client.put(
            "/services/chat/config",
            json={"values": {"host": "10.0.0.1", "password": "***"}},
            headers=auth_headers,
        )
        assert resp.status_code == 204

        current = await store.get_current("chat")
        assert current == {"host": "10.0.0.1", "password": "realpass"}

    async def test_replaces_secret_with_new_value(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {"password": ""}
        await store.save_template("chat", template)
        await store.update_current("chat", {"password": "oldpass"})

        resp = await client.put(
            "/services/chat/config",
            json={"values": {"password": "newpass"}},
            headers=auth_headers,
        )
        assert resp.status_code == 204

        current = await store.get_current("chat")
        assert current == {"password": "newpass"}

    async def test_no_config_schema_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        # No template saved

        resp = await client.put(
            "/services/chat/config",
            json={"values": {"key": "val"}},
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert "No config schema" in resp.json()["error"]

    async def test_nonexistent_service_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.put(
            "/services/nonexistent/config",
            json={"values": {"key": "val"}},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.put("/services/chat/config", json={"values": {}})
        assert resp.status_code == 401

    async def test_merge_nested_config(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {"server": {"host": "localhost", "port": 8080, "password": ""}}
        await store.save_template("chat", template)
        await store.update_current(
            "chat",
            {"server": {"host": "0.0.0.0", "port": 3000, "password": "realpass"}},
        )

        resp = await client.put(
            "/services/chat/config",
            json={"values": {"server": {"host": "10.0.0.1", "password": "***"}}},
            headers=auth_headers,
        )
        assert resp.status_code == 204

        current = await store.get_current("chat")
        # port not in submitted → falls back to template default (8080), not existing (3000)
        assert current == {
            "server": {"host": "10.0.0.1", "port": 8080, "password": "realpass"},
        }

    async def test_write_config_to_volume_called_on_put(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {"host": "localhost"}
        await store.save_template("chat", template)

        captured: list[tuple] = []
        original = server_mod.app.state.backend.write_config_to_volume

        async def _fake_write(volume_name: str, config_dict: dict) -> None:
            captured.append((volume_name, config_dict))
            return await original(volume_name, config_dict)

        monkeypatch.setattr(
            server_mod.app.state.backend, "write_config_to_volume", _fake_write,
        )

        resp = await client.put(
            "/services/chat/config",
            json={"values": {"host": "10.0.0.1"}},
            headers=auth_headers,
        )
        assert resp.status_code == 204
        assert len(captured) == 1
        assert captured[0][0] == "chat-config"
        assert captured[0][1] == {"host": "10.0.0.1"}
