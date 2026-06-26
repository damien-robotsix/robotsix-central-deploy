"""Integration tests for the lifecycle REST server.

Uses ``httpx.AsyncClient`` against a FastAPI test transport so we exercise the
full request/response pipeline including middleware, auth, and error handlers.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.backend import NoopBackend
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState
from robotsix_central_deploy.lifecycle.store import InMemoryStore

# Import the server module itself (not just symbols) so we can set its globals.
from robotsix_central_deploy.lifecycle import server as server_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_store(*names: str) -> None:
    """Populate the server's store with records for testing."""
    s = server_mod.app.state.store
    assert s is not None
    for name in names:
        await s.put(ServiceRecord(name=name, state=ServiceState.STOPPED, image=f"{name}:latest"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch):
    """Wire a fresh store/backend/config into the server module before each test."""
    monkeypatch.setenv("ROBOTSIX_LIFECYCLE_API_KEY", "test-key")
    cfg = LifecycleConfig(  # type: ignore[call-arg]
        store_backend="memory",
        execution_backend="noop",
        api_key="test-key",
    )
    store = InMemoryStore()
    backend = NoopBackend()

    # Set both the module-level globals and app.state so all code paths work.
    server_mod._config = cfg
    server_mod._store = store
    server_mod._backend = backend
    server_mod.app.state.config = cfg
    server_mod.app.state.store = store
    server_mod.app.state.backend = backend


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

        async def _fake_stream(service, tail=100, since=None):
            captured.append({"tail": tail, "since": since})
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
            assert captured[0] == {"tail": 50, "since": "1700000000"}
        finally:
            server_mod.app.state.backend.stream_logs = original

    async def test_tail_out_of_range_returns_422(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        resp = await client.get("/services/svc-a/logs?tail=0", headers=auth_headers)
        assert resp.status_code == 422
        resp2 = await client.get("/services/svc-a/logs?tail=10001", headers=auth_headers)
        assert resp2.status_code == 422
