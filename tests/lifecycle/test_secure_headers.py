"""Tests for the gateway-aware security-headers middleware.

Regression guard for the CSP that broke proxied component UIs (mill/chat):
the strict ``script-src-attr 'none'`` policy must apply to central-deploy's own
base-domain responses but NOT to gateway-proxied component subdomains, whose
frontends rely on inline event handlers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("secure")

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from secure import Secure, Preset

from robotsix_central_deploy.lifecycle.secure_headers import (
    GatewayAwareSecureMiddleware,
)


def _build_app(base_domain: str = "deploy.example") -> FastAPI:
    app = FastAPI()

    @app.get("/{path:path}")
    async def catch_all(path: str) -> dict[str, str]:
        return {"reached": path}

    app.state.config = SimpleNamespace(gateway_base_domain=base_domain)
    app.add_middleware(
        GatewayAwareSecureMiddleware, secure=Secure.from_preset(Preset.BALANCED)
    )
    return app


def _client(app: FastAPI, host: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Host": host},
    )


class TestGatewayAwareSecureMiddleware:
    async def test_base_domain_gets_strict_csp(self):
        """Central-deploy's own UI on the base domain keeps the strict CSP."""
        app = _build_app()
        async with _client(app, "deploy.example") as c:
            resp = await c.get("/ui")
        csp = resp.headers.get("content-security-policy", "")
        assert "script-src-attr 'none'" in csp

    async def test_component_subdomain_skips_csp(self):
        """Proxied component responses must NOT receive central-deploy's CSP —
        their inline event handlers would otherwise be blocked."""
        app = _build_app()
        async with _client(app, "mill.deploy.example") as c:
            resp = await c.get("/static/mill/board-mill.js")
        assert resp.status_code == 200
        assert "content-security-policy" not in resp.headers

    async def test_unconfigured_base_domain_applies_csp_everywhere(self):
        """With no gateway_base_domain, nothing is a component subdomain, so the
        CSP applies to all requests (safe default)."""
        app = _build_app(base_domain="")
        async with _client(app, "mill.deploy.example") as c:
            resp = await c.get("/anything")
        assert "script-src-attr 'none'" in resp.headers.get(
            "content-security-policy", ""
        )
