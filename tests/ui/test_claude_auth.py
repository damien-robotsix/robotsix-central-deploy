"""Tests for the Claude auth router — status, PKCE login, credentials endpoints."""

from __future__ import annotations

import base64
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.backends import NoopBackend
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.models import (
    ExecutionBackendType,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.lifecycle.routers import claude_auth as claude_auth_mod
from robotsix_central_deploy.lifecycle.session import SessionStore
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.settings_store import SystemSettingsStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_store() -> None:
    s = server_mod.app.state.store
    assert s is not None
    await s.put(ServiceRecord(name="svc", state=ServiceState.RUNNING, image="img"))


def _wire(cfg: LifecycleConfig) -> SystemSettingsStore:
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
        claude_auth_mod._login_sessions.clear()

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

    async def test_start_claude_login_returns_pkce_url(self, client: AsyncClient):
        resp = await client.post(
            "/claude-auth/login",
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "login_id" in data
        assert data["oauth_url"].startswith(claude_auth_mod.OAUTH_AUTHORIZE_URL + "?")

        query = parse_qs(urlparse(data["oauth_url"]).query)
        assert query["client_id"] == [claude_auth_mod.OAUTH_CLIENT_ID]
        assert query["response_type"] == ["code"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["state"] == [data["login_id"]]

        # The stored verifier must hash to the challenge in the URL.
        verifier, _ = claude_auth_mod._login_sessions[data["login_id"]]
        expected_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert query["code_challenge"] == [expected_challenge]

    # -- POST /claude-auth/login/complete ----------------------------------

    async def test_complete_claude_login_requires_auth(self, client: AsyncClient):
        resp = await client.post(
            "/claude-auth/login/complete",
            json={"login_id": "some-state", "auth_code": "test-code"},
        )
        assert resp.status_code == 401

    async def test_complete_claude_login_unknown_session(self, client: AsyncClient):
        resp = await client.post(
            "/claude-auth/login/complete",
            json={"login_id": "unknown-state", "auth_code": "test-code"},
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 404

    async def test_complete_claude_login_success(
        self, client: AsyncClient, monkeypatch
    ):
        start = await client.post(
            "/claude-auth/login", headers={"X-API-Key": self.API_KEY}
        )
        login_id = start.json()["login_id"]

        exchange = AsyncMock(
            return_value={
                "access_token": "at-123",
                "refresh_token": "rt-456",
                "expires_in": 3600,
                "scope": "user:inference",
            }
        )
        monkeypatch.setattr(claude_auth_mod, "_exchange_code", exchange)

        resp = await client.post(
            "/claude-auth/login/complete",
            json={"login_id": login_id, "auth_code": "the-code#" + login_id},
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "authenticated"

        # The "#state" suffix must be stripped before the exchange.
        assert exchange.await_args.args[0] == "the-code"
        # The session is consumed on success.
        assert login_id not in claude_auth_mod._login_sessions

    async def test_complete_claude_login_empty_code(self, client: AsyncClient):
        start = await client.post(
            "/claude-auth/login", headers={"X-API-Key": self.API_KEY}
        )
        login_id = start.json()["login_id"]
        resp = await client.post(
            "/claude-auth/login/complete",
            json={"login_id": login_id, "auth_code": "   "},
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 400

    # -- POST /claude-auth/login/cancel ------------------------------------

    async def test_cancel_claude_login_requires_auth(self, client: AsyncClient):
        resp = await client.post(
            "/claude-auth/login/cancel",
            json={"login_id": "some-state"},
        )
        assert resp.status_code == 401

    async def test_cancel_claude_login_discards_session(self, client: AsyncClient):
        start = await client.post(
            "/claude-auth/login", headers={"X-API-Key": self.API_KEY}
        )
        login_id = start.json()["login_id"]
        resp = await client.post(
            "/claude-auth/login/cancel",
            json={"login_id": login_id},
            headers={"X-API-Key": self.API_KEY},
        )
        assert resp.status_code == 204
        assert login_id not in claude_auth_mod._login_sessions

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
