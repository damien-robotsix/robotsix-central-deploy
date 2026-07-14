"""Tests for the gateway-aware CSRF middleware (lifecycle/csrf.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("asgi_csrf")

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.csrf import (
    CSRFHelper,
    GatewayAwareCSRFMiddleware,
)


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
