"""Tests for the UI router — auth-gated dashboard with session-based login."""

from __future__ import annotations

import base64
import re
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.backends import NoopBackend
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.models import (
    ExecutionBackendType,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.lifecycle.rate_limiter import RateLimitStore
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
    server_mod.app.state.rate_limit_store = RateLimitStore()


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _basic_header(password: str, username: str = "anyuser") -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


async def _get_csrf_token(client: AsyncClient) -> str:
    """GET /login and return the csrftoken cookie value."""
    resp = await client.get("/login", follow_redirects=False)
    set_cookie = resp.headers.get("set-cookie", "")
    token = ""
    for part in set_cookie.split(";"):
        part = part.strip()
        if part.startswith("csrftoken="):
            token = part[len("csrftoken=") :]
            break
    return token


async def _login(client: AsyncClient, password: str, username: str = "") -> str:
    """POST /login and return the session_token cookie value."""
    csrf_token = await _get_csrf_token(client)
    resp = await client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "next": "/ui",
            "csrftoken": csrf_token,
        },
        cookies={"csrftoken": csrf_token},
        follow_redirects=False,
    )
    # httpx does not persist cookies across requests by default; extract the
    # Set-Cookie header manually.
    set_cookie = resp.headers.get("set-cookie", "")
    token = ""
    for part in set_cookie.split(";"):
        part = part.strip()
        if part.startswith("session_token="):
            token = part[len("session_token=") :]
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
            execution_backend=ExecutionBackendType.NOOP,
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
        csrf_token = await _get_csrf_token(client)
        resp = await client.post(
            "/login",
            data={
                "username": "",
                "password": "wrong",
                "next": "/ui",
                "csrftoken": csrf_token,
            },
            cookies={"csrftoken": csrf_token},
        )
        assert resp.status_code == 401
        assert "Invalid credentials" in resp.text

    async def test_get_ui_auth_not_required(self, client: AsyncClient, monkeypatch):
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_AUTH_REQUIRED", "false")
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend=ExecutionBackendType.NOOP,
            api_key="",
        )
        _wire(cfg)
        await _seed_store()
        resp = await client.get("/ui")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Robotsix Deploy" in resp.text

    async def test_get_deploy_contract_returns_html_with_contract(
        self, client: AsyncClient
    ):
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
        resp = await client.post(
            "/logout", cookies={"session_token": token}, follow_redirects=False
        )
        assert resp.status_code == 303

        # now /ui should redirect
        resp = await client.get(
            "/ui", cookies={"session_token": token}, follow_redirects=False
        )
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


# ---------------------------------------------------------------------------
# CSP regression — no inline event handlers or inline scripts
# ---------------------------------------------------------------------------


class TestCspNoInlineScripts:
    """Assert that the dashboard, login, and JavaScript contain no inline
    event-handler attributes (onclick=, onchange=, onsubmit=, …) or inline
    <script> blocks, so the CSP
    ``script-src 'self'; script-src-attr 'none'`` does not break the UI."""

    _UI_DIR = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "robotsix_central_deploy"
        / "ui"
    )

    _INLINE_HANDLER_RE = re.compile(r"\son\w+\s*=")

    def test_dashboard_html_no_inline_event_handlers(self):
        html = (self._UI_DIR / "dashboard.html").read_text(encoding="utf-8")
        match = self._INLINE_HANDLER_RE.search(html)
        assert match is None, (
            f"dashboard.html must not contain inline event handlers; "
            f"found '{match.group().strip()}'"
        )

    def test_dashboard_js_no_inline_event_handlers(self):
        js = (self._UI_DIR / "static" / "dashboard.js").read_text(encoding="utf-8")
        match = self._INLINE_HANDLER_RE.search(js)
        assert match is None, (
            f"dashboard.js template strings must not contain inline event "
            f"handlers; found '{match.group().strip()}'"
        )

    def test_login_html_no_inline_script(self):
        html = (self._UI_DIR / "login.html").read_text(encoding="utf-8")
        # An inline <script> block has content between <script> and </script>.
        # <script src="..."></script> is fine (no inline body).
        inline_script = re.search(r"(?i)<script[^>]*>[\s\S]*?</script>", html)
        if inline_script:
            tag = inline_script.group()
            has_body = not re.match(r"(?i)<script\s+src=", tag)
        else:
            has_body = False
        assert not has_body, "login.html must not contain inline <script> blocks"

    def test_login_js_exists(self):
        login_js = self._UI_DIR / "static" / "login.js"
        assert login_js.is_file(), "login.js must exist as a static file"


# ---------------------------------------------------------------------------
# TestRateLimiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    API_KEY = "test-key"

    @pytest.fixture(autouse=True)
    async def _setup(self, monkeypatch):
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_AUTH_REQUIRED", "true")
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend=ExecutionBackendType.NOOP,
            api_key=self.API_KEY,
            rate_limit_login_per_minute=3,
            rate_limit_login_max_attempts=3,
            rate_limit_login_lockout_seconds=600,
            rate_limit_api_per_hour=3,
            gateway_base_domain="deploy.test",
        )
        _wire(cfg)
        await _seed_store()

    async def test_login_rate_limit_returns_429(self, client: AsyncClient):
        """After exceeding the per-minute login limit, further POSTs get 429."""
        csrf_token = await _get_csrf_token(client)
        for _ in range(3):
            resp = await client.post(
                "/login",
                data={
                    "username": "",
                    "password": self.API_KEY,
                    "next": "/ui",
                    "csrftoken": csrf_token,
                },
                cookies={"csrftoken": csrf_token},
            )
            # First 3 should succeed (correct password → 303 redirect)
            assert resp.status_code == 303

        # 4th request within the same window should be rate-limited
        resp = await client.post(
            "/login",
            data={
                "username": "",
                "password": self.API_KEY,
                "next": "/ui",
                "csrftoken": csrf_token,
            },
            cookies={"csrftoken": csrf_token},
        )
        assert resp.status_code == 429

    async def test_login_lockout_after_failures(self, client: AsyncClient):
        """After N failed logins the IP is locked out."""
        csrf_token = await _get_csrf_token(client)
        for _ in range(3):
            resp = await client.post(
                "/login",
                data={
                    "username": "",
                    "password": "wrong",
                    "next": "/ui",
                    "csrftoken": csrf_token,
                },
                cookies={"csrftoken": csrf_token},
            )
            assert resp.status_code == 401

        # Next attempt, even with correct password, should be locked out
        resp = await client.post(
            "/login",
            data={
                "username": "",
                "password": self.API_KEY,
                "next": "/ui",
                "csrftoken": csrf_token,
            },
            cookies={"csrftoken": csrf_token},
        )
        assert resp.status_code == 429
        assert "Too many login attempts" in resp.json()["detail"]

    async def test_api_rate_limit_returns_429(self, client: AsyncClient):
        """After exceeding the per-hour API limit, further requests get 429."""
        for _ in range(3):
            resp = await client.get("/services", headers={"X-API-Key": self.API_KEY})
            assert resp.status_code == 200

        # 4th request within the same window should be rate-limited
        resp = await client.get("/services", headers={"X-API-Key": self.API_KEY})
        assert resp.status_code == 429

    async def test_non_api_paths_are_not_rate_limited(self, client: AsyncClient):
        """Paths like /health and /ui pass through without rate limiting."""
        # Many requests should all succeed
        for _ in range(7):
            resp = await client.get("/health")
            assert resp.status_code == 200

    async def test_login_get_not_rate_limited(self, client: AsyncClient):
        """GET /login is not subject to the POST rate limit."""
        for _ in range(7):
            resp = await client.get("/login", follow_redirects=False)
            assert resp.status_code in (200, 303)

    async def test_gateway_host_traffic_not_rate_limited(self, client: AsyncClient):
        """Requests to a component subdomain bypass the limiter entirely.

        A component's own paths may collide with the limited prefixes
        (chat's ``POST /chat``, a component's ``/login``) — proxied traffic
        must neither consume central-deploy's budget nor be throttled.
        """
        headers = {"host": "chat.deploy.test"}
        for _ in range(7):
            resp = await client.get("/chat", headers=headers)
            assert resp.status_code != 429

        # The gateway traffic above must not have consumed the API budget
        # of the direct-host client either.
        for _ in range(3):
            resp = await client.get("/services", headers={"X-API-Key": self.API_KEY})
            assert resp.status_code == 200

    async def test_gateway_host_login_no_lockout(self, client: AsyncClient):
        """A component's own /login endpoint never feeds the lockout counter."""
        gw = {"host": "mail.deploy.test"}
        for _ in range(7):
            resp = await client.post("/login", data={"password": "wrong"}, headers=gw)
            assert resp.status_code != 429

        # Direct-host login still works: no lockout was recorded.
        csrf_token = await _get_csrf_token(client)
        resp = await client.post(
            "/login",
            data={
                "username": "",
                "password": self.API_KEY,
                "next": "/ui",
                "csrftoken": csrf_token,
            },
            cookies={"csrftoken": csrf_token},
        )
        assert resp.status_code == 303
