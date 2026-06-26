"""Tests for the auth guard — X-API-Key and HTTP Basic Auth."""

from __future__ import annotations

import base64

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


def _wire(cfg: LifecycleConfig) -> None:
    """Wire config + fresh store/backend into the server module."""
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
# Test: API Key only
# ---------------------------------------------------------------------------


class TestApiKeyAuth:
    """X-API-Key accepted when api_key is configured."""

    MUTATING_PATHS = [
        ("POST", "/services/svc/start"),
        ("POST", "/services/svc/stop"),
        ("POST", "/services/svc/restart"),
    ]

    @pytest.fixture(autouse=True)
    async def _setup(self):
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend="noop",
            api_key="my-secret",
        )
        _wire(cfg)
        await _seed_store()

    @pytest.mark.parametrize("method,path", MUTATING_PATHS)
    async def test_missing_key_returns_401(self, client: AsyncClient, method, path):
        resp = await client.request(method, path)
        assert resp.status_code == 401
        data = resp.json()
        assert "error" in data
        assert "www-authenticate" in {k.lower() for k in resp.headers}

    @pytest.mark.parametrize("method,path", MUTATING_PATHS)
    async def test_wrong_key_returns_401(self, client: AsyncClient, method, path):
        resp = await client.request(method, path, headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401
        data = resp.json()
        assert "error" in data

    @pytest.mark.parametrize("method,path", MUTATING_PATHS)
    async def test_correct_key_passes(self, client: AsyncClient, method, path):
        resp = await client.request(method, path, headers={"X-API-Key": "my-secret"})
        assert resp.status_code == 200

    # --- Read endpoints now require auth ---
    async def test_list_services_no_auth_returns_401(self, client: AsyncClient):
        resp = await client.get("/services")
        assert resp.status_code == 401
        assert "www-authenticate" in {k.lower() for k in resp.headers}

    async def test_get_service_no_auth_returns_401(self, client: AsyncClient):
        resp = await client.get("/services/svc")
        assert resp.status_code == 401

    async def test_list_services_with_key_passes(self, client: AsyncClient):
        resp = await client.get("/services", headers={"X-API-Key": "my-secret"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: HTTP Basic Auth only
# ---------------------------------------------------------------------------


class TestBasicAuth:
    """HTTP Basic Auth accepted when api_key is configured — password must match."""

    API_KEY = "mypass"

    @pytest.fixture(autouse=True)
    async def _setup(self):
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend="noop",
            api_key=self.API_KEY,
        )
        _wire(cfg)
        await _seed_store()

    def _basic_header(self, username: str = "anyuser", password: str = None) -> dict:
        p = password if password is not None else self.API_KEY
        encoded = base64.b64encode(f"{username}:{p}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    async def test_basic_auth_passes_get_services(self, client: AsyncClient):
        resp = await client.get("/services", headers=self._basic_header())
        assert resp.status_code == 200

    async def test_basic_auth_passes_start(self, client: AsyncClient):
        resp = await client.post("/services/svc/start", headers=self._basic_header())
        assert resp.status_code == 200

    async def test_missing_auth_returns_401(self, client: AsyncClient):
        resp = await client.get("/services")
        assert resp.status_code == 401
        assert "www-authenticate" in {k.lower() for k in resp.headers}

    async def test_wrong_password_returns_401(self, client: AsyncClient):
        resp = await client.get("/services", headers=self._basic_header(password="bad"))
        assert resp.status_code == 401
        assert "www-authenticate" in {k.lower() for k in resp.headers}

    async def test_wrong_username_with_correct_password_succeeds(self, client: AsyncClient):
        # Username is ignored — only the password matters against api_key.
        # Wrong username with correct password should succeed.
        resp = await client.get("/services", headers=self._basic_header(username="bad"))
        assert resp.status_code == 200

    async def test_api_key_not_accepted_when_only_basic_configured(self, client: AsyncClient):
        resp = await client.get("/services", headers={"X-API-Key": "some-key"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test: Both credentials configured
# ---------------------------------------------------------------------------


class TestBothCredentials:
    """When api_key is configured, both X-API-Key and Basic Auth are accepted."""

    API_KEY = "my-secret"

    @pytest.fixture(autouse=True)
    async def _setup(self):
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend="noop",
            api_key=self.API_KEY,
        )
        _wire(cfg)
        await _seed_store()

    def _basic_header(self) -> dict:
        encoded = base64.b64encode(f"anyuser:{self.API_KEY}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    async def test_api_key_accepted(self, client: AsyncClient):
        resp = await client.get("/services", headers={"X-API-Key": self.API_KEY})
        assert resp.status_code == 200

    async def test_basic_auth_accepted(self, client: AsyncClient):
        resp = await client.get("/services", headers=self._basic_header())
        assert resp.status_code == 200

    async def test_no_credentials_returns_401(self, client: AsyncClient):
        resp = await client.get("/services")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test: Username + Password auth (no api_key)
# ---------------------------------------------------------------------------


class TestUsernamePasswordAuth:
    """HTTP Basic Auth with username+password credentials — api_key is blank."""

    USERNAME = "admin"
    PASSWORD = "hunter2"

    @pytest.fixture(autouse=True)
    async def _setup(self):
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend="noop",
            api_key="",
            auth_username=self.USERNAME,
            auth_password=self.PASSWORD,
        )
        _wire(cfg)
        await _seed_store()

    def _basic_header(self, username: str = None, password: str = None) -> dict:
        u = username if username is not None else self.USERNAME
        p = password if password is not None else self.PASSWORD
        encoded = base64.b64encode(f"{u}:{p}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    async def test_no_credentials_returns_401(self, client: AsyncClient):
        resp = await client.get("/services")
        assert resp.status_code == 401
        assert "www-authenticate" in {k.lower() for k in resp.headers}

    async def test_correct_credentials_returns_200(self, client: AsyncClient):
        resp = await client.get("/services", headers=self._basic_header())
        assert resp.status_code == 200

    async def test_wrong_password_returns_401(self, client: AsyncClient):
        resp = await client.get(
            "/services", headers=self._basic_header(password="wrong")
        )
        assert resp.status_code == 401

    async def test_wrong_username_returns_401(self, client: AsyncClient):
        resp = await client.get(
            "/services", headers=self._basic_header(username="wronguser")
        )
        assert resp.status_code == 401

    async def test_api_key_header_not_accepted(self, client: AsyncClient):
        resp = await client.get("/services", headers={"X-API-Key": "hunter2"})
        assert resp.status_code == 401

    async def test_start_requires_auth(self, client: AsyncClient):
        resp = await client.post("/services/svc/start")
        assert resp.status_code == 401

    async def test_start_with_valid_credentials_returns_200(self, client: AsyncClient):
        resp = await client.post(
            "/services/svc/start", headers=self._basic_header()
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: Dev mode — no credentials configured
# ---------------------------------------------------------------------------


class TestDevMode:
    """When no credentials are configured, all endpoints are open."""

    @pytest.fixture(autouse=True)
    async def _setup(self):
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend="noop",
            api_key="",
            auth_username="",
            auth_password="",
        )
        _wire(cfg)
        await _seed_store()

    async def test_start_without_auth_succeeds(self, client: AsyncClient):
        resp = await client.post("/services/svc/start")
        assert resp.status_code == 200

    async def test_stop_without_auth_succeeds(self, client: AsyncClient):
        resp = await client.post("/services/svc/stop")
        assert resp.status_code == 200

    async def test_restart_without_auth_succeeds(self, client: AsyncClient):
        s = server_mod.app.state.store
        rec = await s.get("svc")
        rec.state = ServiceState.RUNNING
        await s.put(rec)
        resp = await client.post("/services/svc/restart")
        assert resp.status_code == 200

    async def test_list_services_succeeds(self, client: AsyncClient):
        resp = await client.get("/services")
        assert resp.status_code == 200

    async def test_get_service_succeeds(self, client: AsyncClient):
        resp = await client.get("/services/svc")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: Health endpoint is always unauthenticated
# ---------------------------------------------------------------------------


class TestHealthUnauthenticated:
    """GET /health must never require credentials."""

    @pytest.fixture(autouse=True)
    async def _setup(self):
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend="noop",
            api_key="my-secret",
            auth_username="myuser",
            auth_password="mypass",
        )
        _wire(cfg)
        await _seed_store()

    async def test_health_no_auth_succeeds(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200

    async def test_health_with_api_key_succeeds(self, client: AsyncClient):
        resp = await client.get("/health", headers={"X-API-Key": "my-secret"})
        assert resp.status_code == 200

    async def test_health_with_basic_auth_succeeds(self, client: AsyncClient):
        encoded = base64.b64encode(b"myuser:mypass").decode()
        resp = await client.get("/health", headers={"Authorization": f"Basic {encoded}"})
        assert resp.status_code == 200
