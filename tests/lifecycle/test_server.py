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
from robotsix_central_deploy.registry.env_store import EnvStore
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
