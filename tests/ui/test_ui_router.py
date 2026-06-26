"""Tests for the UI router — auth-gated dashboard."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

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
    mock_checker = MagicMock()
    mock_checker.get_latest_digest = AsyncMock(return_value=None)
    server_mod._config = cfg
    server_mod._store = store
    server_mod._backend = backend
    server_mod._registry_checker = mock_checker
    server_mod.app.state.config = cfg
    server_mod.app.state.store = store
    server_mod.app.state.backend = backend
    server_mod.app.state.registry_checker = mock_checker


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _basic_header(password: str, username: str = "anyuser") -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


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

    async def test_get_ui_no_auth_returns_401(self, client: AsyncClient):
        resp = await client.get("/ui")
        assert resp.status_code == 401
        www_auth = resp.headers.get("www-authenticate", "")
        assert 'Basic realm="Robotsix Central Deploy"' in www_auth

    async def test_get_ui_wrong_key_returns_401(self, client: AsyncClient):
        resp = await client.get("/ui", headers=_basic_header("wrong-password"))
        assert resp.status_code == 401

    async def test_get_ui_valid_basic_auth_returns_html(self, client: AsyncClient):
        resp = await client.get("/ui", headers=_basic_header(self.API_KEY))
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Robotsix Deploy" in resp.text

    async def test_get_ui_valid_x_api_key_returns_html(self, client: AsyncClient):
        resp = await client.get("/ui", headers={"X-API-Key": self.API_KEY})
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Robotsix Deploy" in resp.text

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

    async def test_verify_auth_basic_ignores_username(self, client: AsyncClient):
        """verify_auth accepts any username as long as the password is correct."""
        resp = await client.get("/ui", headers=_basic_header(self.API_KEY, username="random-user"))
        assert resp.status_code == 200
        assert "Robotsix Deploy" in resp.text
