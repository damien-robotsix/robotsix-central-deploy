"""Tests for the UI router — auth-gated dashboard with session-based login."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.backend import NoopBackend
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState
from robotsix_central_deploy.lifecycle.session import SessionStore
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.loader import ComponentRegistry


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
    mock_checker = MagicMock()
    mock_checker.get_latest_digest = AsyncMock(return_value=None)
    session_store = SessionStore()
    registry = ComponentRegistry([])
    tmpdir = Path(tempfile.mkdtemp())
    component_config_store = ComponentConfigStore(tmpdir / "components.json")

    server_mod._config = cfg
    server_mod._store = store
    server_mod._backend = backend
    server_mod._registry_checker = mock_checker
    server_mod.app.state.config = cfg
    server_mod.app.state.store = store
    server_mod.app.state.backend = backend
    server_mod.app.state.registry_checker = mock_checker
    server_mod.app.state.session_store = session_store
    server_mod.app.state.registry = registry
    server_mod.app.state.component_config_store = component_config_store


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _basic_header(password: str, username: str = "anyuser") -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


async def _login(client: AsyncClient, password: str, username: str = "") -> str:
    """POST /login and return the session_token cookie value."""
    resp = await client.post(
        "/login",
        data={"username": username, "password": password, "next": "/ui"},
        follow_redirects=False,
    )
    # httpx does not persist cookies across requests by default; extract the
    # Set-Cookie header manually.
    set_cookie = resp.headers.get("set-cookie", "")
    token = ""
    for part in set_cookie.split(";"):
        part = part.strip()
        if part.startswith("session_token="):
            token = part[len("session_token="):]
            break
    return token


# ---------------------------------------------------------------------------
# TestUiRouter
# ---------------------------------------------------------------------------


class TestUiRouter:
    API_KEY = "test-key"

    @pytest.fixture(autouse=True)
    async def _setup(self, monkeypatch):
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_AUTH_REQUIRED", "true")
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend="noop",
            api_key=self.API_KEY,
        )
        _wire(cfg)
        await _seed_store()

    async def test_get_ui_no_auth_redirects_to_login(self, client: AsyncClient):
        """Without a session cookie, /ui redirects to /login with next param."""
        resp = await client.get("/ui", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login?next=")

    async def test_get_ui_wrong_creds_redirects_to_login(self, client: AsyncClient):
        """Even with a Basic Auth header, /ui now uses session auth — redirect."""
        resp = await client.get(
            "/ui",
            headers=_basic_header("wrong-password"),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]

    async def test_login_then_dashboard_returns_html(self, client: AsyncClient):
        """Login via form, then access /ui with the session cookie."""
        token = await _login(client, self.API_KEY)
        resp = await client.get("/ui", cookies={"session_token": token})
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Robotsix Deploy" in resp.text

    async def test_login_wrong_password_returns_error(self, client: AsyncClient):
        resp = await client.post("/login", data={"username": "", "password": "wrong", "next": "/ui"})
        assert resp.status_code == 401
        assert "Invalid credentials" in resp.text

    async def test_get_ui_auth_not_required(self, client: AsyncClient, monkeypatch):
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_AUTH_REQUIRED", "false")
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend="noop",
            api_key="",
        )
        _wire(cfg)
        await _seed_store()
        resp = await client.get("/ui")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Robotsix Deploy" in resp.text

    async def test_get_deploy_contract_returns_html_with_contract(self, client: AsyncClient):
        resp = await client.get("/help/deploy-contract")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "central-deploy Docker Compose Contract" in resp.text

    async def test_verify_auth_basic_ignores_username(self, client: AsyncClient):
        """verify_auth (JSON API) still accepts any username with correct password."""
        resp = await client.get(
            "/services",
            headers=_basic_header(self.API_KEY, username="random-user"),
        )
        assert resp.status_code == 200

    async def test_logout_then_ui_redirects(self, client: AsyncClient):
        """After login + logout, /ui redirects to /login."""
        token = await _login(client, self.API_KEY)
        # verify we can access /ui
        resp = await client.get("/ui", cookies={"session_token": token})
        assert resp.status_code == 200

        # logout
        resp = await client.post("/logout", cookies={"session_token": token}, follow_redirects=False)
        assert resp.status_code == 303

        # now /ui should redirect
        resp = await client.get("/ui", cookies={"session_token": token}, follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]

    async def test_api_endpoints_still_use_verify_auth(self, client: AsyncClient):
        """JSON API endpoints continue to accept X-API-Key and Basic Auth."""
        # Without auth → 401
        resp = await client.get("/services", follow_redirects=False)
        assert resp.status_code == 401
        assert "www-authenticate" in resp.headers

        # With valid X-API-Key → 200
        resp = await client.get("/services", headers={"X-API-Key": self.API_KEY})
        assert resp.status_code == 200

        # With valid Basic Auth → 200
        resp = await client.get("/services", headers=_basic_header(self.API_KEY))
        assert resp.status_code == 200
