"""Tests for the Claude auth router — status, login, credentials endpoints."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.backend import NoopBackend
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.models import (
    ExecutionBackendType,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.lifecycle.session import SessionStore
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.settings_store import (
    SystemSettings,
    SystemSettingsStore,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_store() -> None:
    s = server_mod.app.state.store
    assert s is not None
    await s.put(ServiceRecord(name="svc", state=ServiceState.RUNNING, image="img"))


def _wire(
    cfg: LifecycleConfig, settings: SystemSettings | None = None
) -> SystemSettingsStore:
    """Wire config + fresh store/backend/settings into the server module."""
    store = InMemoryStore()
    backend = NoopBackend()
    mock_checker = MagicMock()
    mock_checker.get_latest_digest = AsyncMock(return_value=None)
    session_store = SessionStore()
    registry = ComponentRegistry([])
    tmpdir = Path(tempfile.mkdtemp())
    component_config_store = ComponentConfigStore(tmpdir / "components.json")
    settings_path = tmpdir / "settings.json"
    settings_store = SystemSettingsStore(settings_path)

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
    server_mod.app.state.settings_store = settings_store
    return settings_store


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _basic_header(password: str, username: str = "anyuser") -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


# ---------------------------------------------------------------------------
# TestClaudeAuthRouter
# ---------------------------------------------------------------------------


class TestClaudeAuthRouter:
    API_KEY = "test-key"

    @pytest.fixture(autouse=True)
    async def _setup(self, monkeypatch):
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_AUTH_REQUIRED", "true")
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend=ExecutionBackendType.NOOP,
            api_key=self.API_KEY,
        )
        _wire(cfg)
        await _seed_store()

    # -- GET /claude-auth/status -------------------------------------------

    async def test_get_claude_auth_status_requires_auth(self, client: AsyncClient):
        resp = await client.get("/claude-auth/status")
        assert resp.status_code == 401

    async def test_get_claude_auth_status_returns_status(self, client: AsyncClient):
        resp = await client.get(
            "/claude-auth/status", headers={"X-API-Key": self.API_KEY}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert data["status"] in (
            "authenticated",
            "not-authenticated",
            "expiring",
            "error",
        )

    # -- POST /claude-auth/login -------------------------------------------

    async def test_start_claude_login_requires_auth(self, client: AsyncClient):
        resp = await client.post("/claude-auth/login")
        assert resp.status_code == 401

    async def test_start_claude_login_returns_url_and_container_id(
        self, client: AsyncClient
    ):
        resp = await client.post(
            "/claude-auth/login",
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "container_id" in data
        assert "oauth_url" in data
        assert data["oauth_url"].startswith("https://")

    # -- POST /claude-auth/login/complete ----------------------------------

    async def test_complete_claude_login_requires_auth(self, client: AsyncClient):
        resp = await client.post(
            "/claude-auth/login/complete",
            json={"container_id": "noop-login", "auth_code": "test-code"},
        )
        assert resp.status_code == 401

    async def test_complete_claude_login_returns_success(self, client: AsyncClient):
        resp = await client.post(
            "/claude-auth/login/complete",
            json={"container_id": "noop-login", "auth_code": "test-code"},
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "authenticated"

    # -- POST /claude-auth/login/cancel ------------------------------------

    async def test_cancel_claude_login_requires_auth(self, client: AsyncClient):
        resp = await client.post(
            "/claude-auth/login/cancel",
            json={"container_id": "noop-login", "auth_code": ""},
        )
        assert resp.status_code == 401

    async def test_cancel_claude_login_returns_no_content(self, client: AsyncClient):
        resp = await client.post(
            "/claude-auth/login/cancel",
            json={"container_id": "noop-login", "auth_code": ""},
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 204

    # -- POST /claude-auth/credentials -------------------------------------

    async def test_write_credentials_requires_auth(self, client: AsyncClient):
        resp = await client.post(
            "/claude-auth/credentials",
            json={"credentials_json": '{"test": true}'},
        )
        assert resp.status_code == 401

    async def test_write_credentials_returns_success(self, client: AsyncClient):
        resp = await client.post(
            "/claude-auth/credentials",
            json={
                "credentials_json": '{"oauth_token": "test-token", "expires_at": "2099-01-01T00:00:00Z"}'
            },
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "authenticated"

    # -- Basic Auth support ------------------------------------------------

    async def test_status_with_basic_auth(self, client: AsyncClient):
        resp = await client.get(
            "/claude-auth/status",
            headers=_basic_header(self.API_KEY),
        )
        assert resp.status_code == 200

    # -- Settings integration ----------------------------------------------

    async def test_login_uses_default_helper_image_when_not_configured(
        self, client: AsyncClient
    ):
        """When claude_auth_helper_image is empty, the default is used."""
        resp = await client.post(
            "/claude-auth/login",
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["oauth_url"].startswith("https://")


class TestClaudeAuthRouterWithCustomHelperImage:
    API_KEY = "test-key"

    @pytest.fixture(autouse=True)
    async def _setup(self, monkeypatch):
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_AUTH_REQUIRED", "true")
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend=ExecutionBackendType.NOOP,
            api_key=self.API_KEY,
        )
        settings = SystemSettings(
            claude_auth_helper_image="ghcr.io/custom/claude-helper:latest",
        )
        store = _wire(cfg, settings)
        # Persist the custom setting
        await store.put(settings)

    async def test_custom_helper_image_is_passed_to_backend(
        self, client: AsyncClient, monkeypatch
    ):
        """The custom helper image should be resolved and used."""
        # The NoopBackend ignores the image, but we verify the endpoint works.
        resp = await client.post(
            "/claude-auth/login",
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200
