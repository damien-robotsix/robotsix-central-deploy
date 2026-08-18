"""Tests for the UI router.

The dashboard carries no auth dependency of its own: the Traefik edge
authenticates every request through tinyauth SSO before it reaches this app
(see ``registry/traefik_labels.py``). The JSON API keeps ``verify_auth`` as
defence-in-depth against a caller already on the internal network.
"""

from __future__ import annotations

import base64
import re
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import _gen_robotsix_ui_css as robotsix_ui_css
import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.backends import NoopBackend
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.models import (
    ExecutionBackendType,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.lifecycle.rate_limiter import RateLimitStore
from robotsix_central_deploy.lifecycle.store import InMemoryStore
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


# ---------------------------------------------------------------------------
# TestUiRouter
# ---------------------------------------------------------------------------


class TestUiRouter:
    API_KEY = "test-key"

    @pytest.fixture(autouse=True)
    async def _setup(self, monkeypatch):
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend=ExecutionBackendType.NOOP,
        )
        _wire(cfg)
        await _seed_store()

    async def test_dashboard_needs_no_session_of_its_own(self, client: AsyncClient):
        """``GET /ui`` serves the dashboard directly.

        There is no login page and no session cookie to present — a request
        only reaches this app after the edge has authenticated it.
        """
        resp = await client.get("/ui", follow_redirects=False)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Robotsix Deploy" in resp.text

    async def test_bare_root_redirects_to_the_dashboard(self, client: AsyncClient):
        """``GET /`` must not 404.

        The retired nginx vhost redirected the bare domain to /ui. Dropping it
        during the Traefik cutover left the fleet's front door returning "not
        found" to anyone typing the hostname without a path.
        """
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/ui"

    async def test_no_login_or_logout_routes_remain(self, client: AsyncClient):
        """The old session endpoints are gone, not merely unlinked."""
        assert (await client.get("/login", follow_redirects=False)).status_code == 404
        assert (await client.post("/logout", follow_redirects=False)).status_code == 404

    async def test_dashboard_sets_a_csrf_cookie(self, client: AsyncClient):
        """CSRF protection survives the removal of the login flow.

        The SSO cookie rides along on a cross-site form post, so the
        dashboard's mutating forms still need a token.
        """
        resp = await client.get("/ui")
        assert "csrftoken=" in resp.headers.get("set-cookie", "")

    async def test_verify_auth_basic_ignores_username(self, client: AsyncClient):
        """verify_auth (JSON API) still accepts any username with correct password."""
        resp = await client.get(
            "/services",
            headers=_basic_header(self.API_KEY, username="random-user"),
        )
        assert resp.status_code == 200

    async def test_api_endpoints_accept_requests_without_credentials(
        self, client: AsyncClient
    ):
        """JSON API endpoints trust the fleet edge — no app-level auth remains."""
        resp = await client.get("/services", follow_redirects=False)
        assert resp.status_code == 200

        # Previously accepted credentials are simply ignored.
        resp = await client.get("/services", headers={"X-API-Key": self.API_KEY})
        assert resp.status_code == 200
        resp = await client.get("/services", headers=_basic_header(self.API_KEY))
        assert resp.status_code == 200

    async def test_get_deploy_contract_returns_html_with_contract(
        self, client: AsyncClient
    ):
        resp = await client.get("/help/deploy-contract")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "central-deploy Docker Compose Contract" in resp.text


# ---------------------------------------------------------------------------
# CSP regression — no inline event handlers or inline scripts
# ---------------------------------------------------------------------------


class TestCspNoInlineScripts:
    """Assert that the dashboard and its JavaScript contain no inline
    event-handler attributes (onclick=, onchange=, onsubmit=, …), so the CSP
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


# ---------------------------------------------------------------------------
# robotsix-ui stylesheet sourcing — build-time fetch, not a vendored copy
# ---------------------------------------------------------------------------


class TestRobotsixUiCssLink:
    """The dashboard stylesheet must come from the build-time-fetched
    ``robotsix-ui.css``, not the retired one-shot ``robotsix-ui-base.css``."""

    _UI_DIR = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "robotsix_central_deploy"
        / "ui"
    )

    def test_dashboard_links_build_fetched_stylesheet(self):
        html = (self._UI_DIR / "dashboard.html").read_text(encoding="utf-8")
        assert 'href="/ui/static/robotsix-ui.css"' in html
        assert "robotsix-ui-base.css" not in html

    def test_css_url_uses_release_download_path(self):
        assert robotsix_ui_css.css_url("v0.1.30") == (
            "https://github.com/damien-robotsix/robotsix-ui/"
            "releases/download/v0.1.30/style.css"
        )

    def test_fetch_rejects_non_semver_version(self):
        assert robotsix_ui_css.main(["latest"]) == 2


# ---------------------------------------------------------------------------
# TestRateLimiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    API_KEY = "test-key"

    @pytest.fixture(autouse=True)
    async def _setup(self, monkeypatch):
        cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend=ExecutionBackendType.NOOP,
            rate_limit_api_per_hour=3,
            gateway_base_domain="deploy.test",
        )
        _wire(cfg)
        await _seed_store()

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
        for _ in range(7):
            resp = await client.get("/health")
            assert resp.status_code == 200
