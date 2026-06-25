"""Tests for the API-key auth guard."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.backend import NoopBackend
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.lifecycle import server as server_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_store() -> None:
    s = server_mod.app.state.store
    assert s is not None
    await s.put(ServiceRecord(name="svc", state=ServiceState.RUNNING, image="img"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch):
    """Wire a fresh store/backend/config into the server module before each test."""
    monkeypatch.setenv("ROBOTSIX_LIFECYCLE_API_KEY", "my-secret")
    cfg = LifecycleConfig(  # type: ignore[call-arg]
        store_backend="memory",
        execution_backend="noop",
        api_key="my-secret",
    )
    store = InMemoryStore()
    backend = NoopBackend()

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAuthRequired:
    """When an API key is configured, mutating endpoints must reject
    requests that lack a valid key."""

    MUTATING_PATHS = [
        ("POST", "/services/svc/start"),
        ("POST", "/services/svc/stop"),
        ("POST", "/services/svc/restart"),
    ]

    @pytest.fixture(autouse=True)
    async def _seed(self):
        await _seed_store()

    @pytest.mark.parametrize("method,path", MUTATING_PATHS)
    async def test_missing_key_returns_403(self, client: AsyncClient, method, path):
        resp = await client.request(method, path)
        assert resp.status_code == 403
        data = resp.json()
        assert "error" in data
        assert "Missing" in data["error"]

    @pytest.mark.parametrize("method,path", MUTATING_PATHS)
    async def test_wrong_key_returns_403(self, client: AsyncClient, method, path):
        resp = await client.request(method, path, headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 403
        data = resp.json()
        assert "Invalid" in data["error"]

    @pytest.mark.parametrize("method,path", MUTATING_PATHS)
    async def test_correct_key_passes(self, client: AsyncClient, method, path):
        resp = await client.request(method, path, headers={"X-API-Key": "my-secret"})
        assert resp.status_code == 200


class TestAuthOptional:
    """When no API key is set, all endpoints are open (dev mode)."""

    @pytest.fixture(autouse=True)
    async def _setup(self):
        """Override globals for dev-mode (no API key)."""
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend="noop",
            api_key="",
        )
        store = InMemoryStore()
        backend = NoopBackend()

        server_mod._config = cfg
        server_mod._store = store
        server_mod._backend = backend
        server_mod.app.state.config = cfg
        server_mod.app.state.store = store
        server_mod.app.state.backend = backend
        await _seed_store()

    async def test_start_without_key_succeeds(self, client: AsyncClient):
        resp = await client.post("/services/svc/start")
        assert resp.status_code == 200

    async def test_stop_without_key_succeeds(self, client: AsyncClient):
        resp = await client.post("/services/svc/stop")
        assert resp.status_code == 200

    async def test_restart_without_key_succeeds(self, client: AsyncClient):
        s = server_mod.app.state.store
        rec = await s.get("svc")
        rec.state = ServiceState.RUNNING
        await s.put(rec)
        resp = await client.post("/services/svc/restart")
        assert resp.status_code == 200

    async def test_read_endpoint_never_requires_auth(self, client: AsyncClient):
        resp = await client.get("/services")
        assert resp.status_code == 200
