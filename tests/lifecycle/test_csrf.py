"""Tests for the gateway-aware CSRF middleware (lifecycle/csrf.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.csrf import (
    CSRFHelper,
    _HAS_ITSDANGEROUS,
)

try:
    from robotsix_central_deploy.lifecycle.csrf import GatewayAwareCSRFMiddleware

    _HAS_ASGI_CSRF = True
except ImportError:
    GatewayAwareCSRFMiddleware = None  # type: ignore[assignment]
    _HAS_ASGI_CSRF = False


def _build_app(base_domain: str = "deploy.example") -> FastAPI:
    app = FastAPI()

    @app.post("/{path:path}")
    async def catch_all(path: str) -> dict[str, str]:
        return {"reached": path}

    app.state.config = SimpleNamespace(gateway_base_domain=base_domain)
    app.add_middleware(GatewayAwareCSRFMiddleware, secret="test-secret")
    return app


def _client(app: FastAPI, host: str, cookies: dict[str, str]) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Host": host},
        cookies=cookies,
    )


class TestCSRFHelper:
    """Unit tests for the core CSRFHelper token generation and validation."""

    @pytest.mark.skipif(not _HAS_ITSDANGEROUS, reason="itsdangerous not installed")
    def test_generate_returns_url_safe_token(self):
        """generate() returns a signed, URL-safe token when itsdangerous is available."""
        helper = CSRFHelper("test-secret")
        token = helper.generate()
        assert isinstance(token, str)
        assert len(token) > 0
        # itsdangerous-signed tokens contain a dot separator (payload.signature)
        assert "." in token

    def test_generate_returns_raw_token_when_itsdangerous_absent(self, monkeypatch):
        """generate() returns a raw random token when itsdangerous is not installed."""
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.csrf._HAS_ITSDANGEROUS", False
        )
        helper = CSRFHelper("test-secret")
        assert helper.serializer is None
        token = helper.generate()
        assert isinstance(token, str)
        assert len(token) > 0

    @pytest.mark.skipif(not _HAS_ITSDANGEROUS, reason="itsdangerous not installed")
    def test_init_creates_serializer_when_itsdangerous_available(self):
        """__init__ creates a URLSafeSerializer when itsdangerous is available."""
        helper = CSRFHelper("test-secret")
        assert helper.serializer is not None

    def test_init_serializer_is_none_when_itsdangerous_absent(self, monkeypatch):
        """__init__ leaves serializer as None when itsdangerous is not installed."""
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.csrf._HAS_ITSDANGEROUS", False
        )
        helper = CSRFHelper("test-secret")
        assert helper.serializer is None

    @pytest.mark.skipif(not _HAS_ITSDANGEROUS, reason="itsdangerous not installed")
    def test_validate_matching_pair_returns_true(self):
        """validate() returns True when cookie and token match."""
        helper = CSRFHelper("test-secret")
        token = helper.generate()
        assert helper.validate(token, token) is True

    @pytest.mark.skipif(not _HAS_ITSDANGEROUS, reason="itsdangerous not installed")
    def test_validate_mismatched_pair_returns_false(self):
        """validate() returns False when cookie and token differ."""
        helper = CSRFHelper("test-secret")
        cookie = helper.generate()
        token = helper.generate()
        assert helper.validate(cookie, token) is False

    @pytest.mark.skipif(not _HAS_ITSDANGEROUS, reason="itsdangerous not installed")
    def test_validate_empty_cookie_returns_false(self):
        """validate() returns False when cookie_value is empty."""
        helper = CSRFHelper("test-secret")
        token = helper.generate()
        assert helper.validate("", token) is False

    @pytest.mark.skipif(not _HAS_ITSDANGEROUS, reason="itsdangerous not installed")
    def test_validate_empty_token_returns_false(self):
        """validate() returns False when token is empty."""
        helper = CSRFHelper("test-secret")
        cookie = helper.generate()
        assert helper.validate(cookie, "") is False

    def test_validate_pass_through_when_itsdangerous_absent(self, monkeypatch):
        """validate() returns True (pass-through) when itsdangerous is not installed."""
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.csrf._HAS_ITSDANGEROUS", False
        )
        helper = CSRFHelper("test-secret")
        assert helper.serializer is None
        # Even with garbage or empty values, validate should pass through
        assert helper.validate("anything", "whatever") is True
        assert helper.validate("", "") is True

    @pytest.mark.skipif(not _HAS_ITSDANGEROUS, reason="itsdangerous not installed")
    def test_validate_tampered_token_returns_false(self):
        """validate() returns False when one character in the token is flipped."""
        helper = CSRFHelper("test-secret")
        token = helper.generate()
        # Flip a character in the middle of the token
        idx = len(token) // 2
        chars = list(token)
        chars[idx] = "X" if chars[idx] != "X" else "Y"
        tampered = "".join(chars)
        assert helper.validate(token, tampered) is False


@pytest.mark.skipif(not _HAS_ASGI_CSRF, reason="asgi_csrf not installed")
class TestGatewayAwareCSRFMiddleware:
    async def test_component_subdomain_post_bypasses_csrf(self):
        """POSTs routed to a component subdomain must not require the panel's
        CSRF token — proxied apps never receive it."""
        app = _build_app()
        async with _client(app, "chat.deploy.example", {"session": "s"}) as c:
            resp = await c.post("/api/message", json={"text": "hi"})
        assert resp.status_code == 200
        assert resp.json() == {"reached": "api/message"}

    async def test_panel_host_post_without_token_is_rejected(self):
        app = _build_app()
        async with _client(app, "deploy.example", {"session": "s"}) as c:
            resp = await c.post("/form", json={})
        assert resp.status_code == 403

    async def test_panel_host_post_with_valid_token_passes(self):
        app = _build_app()
        token = CSRFHelper("test-secret").generate()
        async with _client(app, "deploy.example", {"csrftoken": token}) as c:
            resp = await c.post("/form", headers={"x-csrftoken": token}, json={})
        assert resp.status_code == 200

    async def test_unconfigured_base_domain_still_enforces_csrf(self):
        app = _build_app(base_domain="")
        async with _client(app, "chat.deploy.example", {"session": "s"}) as c:
            resp = await c.post("/api/message", json={})
        assert resp.status_code == 403
