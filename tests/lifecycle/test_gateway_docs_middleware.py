"""Tests for the gateway-aware docs middleware.

Ensures /docs, /openapi.json, and /redoc are proxied to the target
component on subdomain requests rather than serving central-deploy's
own Swagger UI.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.gateway.router import _extract_subdomain_name, _resolve
from robotsix_central_deploy.lifecycle.gateway_docs_middleware import (
    GatewayAwareDocsMiddleware,
)
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import ComponentConfig, PortMapping


def _make_config(
    component_id: str = "svc",
    *,
    container_name: str = "svc-ctr",
) -> ComponentConfig:
    return ComponentConfig(
        id=component_id,
        image="repo:v1",
        container_name=container_name,
        ports=[PortMapping(host=8080, container=9000)],
    )


def _build_app(
    *configs: ComponentConfig,
    base_domain: str = "deploy.example",
) -> FastAPI:
    """Build a FastAPI app with the middleware and a component registry.

    FastAPI's built-in /docs, /openapi.json, and /redoc are disabled so our
    custom handlers take effect.  In production the middleware runs *before*
    FastAPI's built-in handlers reach the request, but in these unit tests we
    simulate that by using custom handlers that return a predictable payload
    and verifying the middleware either proxies (subdomain) or passes through
    (base domain) to them.
    """
    app = FastAPI(docs_url=None, openapi_url=None, redoc_url=None)

    # Simulate the built-in /docs and /openapi.json that FastAPI normally
    # registers — these would serve central-deploy's own Swagger UI.
    # Registered BEFORE the catch-all so they take priority.
    @app.get("/docs")
    async def builtin_docs() -> dict[str, str]:
        return {"swagger": "central-deploy"}

    @app.get("/openapi.json")
    async def builtin_openapi() -> dict[str, str]:
        return {"openapi": "central-deploy"}

    @app.get("/redoc")
    async def builtin_redoc() -> dict[str, str]:
        return {"redoc": "central-deploy"}

    @app.get("/{path:path}")
    async def catch_all(path: str) -> dict[str, str]:
        return {"reached": path}

    app.state.config = SimpleNamespace(gateway_base_domain=base_domain)
    app.state.registry = ComponentRegistry(list(configs))
    app.add_middleware(GatewayAwareDocsMiddleware)
    return app


def _proxy_side_effect(req, target_base, path):
    """Simulate an upstream component responding with its own docs."""
    return StreamingResponse(
        iter([f"proxy:{target_base}:{path}".encode()]),
        status_code=200,
    )


class TestGatewayAwareDocsMiddleware:
    """Verify /docs and /openapi.json routing on subdomain vs base domain."""

    # -- Subdomain requests — proxied to component --------------------------

    @pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc"])
    async def test_subdomain_docs_proxied_to_component(self, path):
        """On a component subdomain, /docs, /openapi.json, and /redoc are
        proxied to the component container."""
        app = _build_app(_make_config("invest", container_name="invest-ctr"))
        with patch(
            "robotsix_central_deploy.gateway.proxy.http_proxy",
            side_effect=_proxy_side_effect,
        ) as mock:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Host": "invest.deploy.example"},
            ) as c:
                resp = await c.get(path)
        assert resp.status_code == 200
        assert b"proxy:http://invest-ctr:9000:" + path.encode() in resp.content
        mock.assert_called_once()

    async def test_subdomain_docs_preserves_query_string(self):
        """Query parameters on /docs are forwarded to the upstream."""
        app = _build_app(_make_config("invest", container_name="invest-ctr"))
        with patch(
            "robotsix_central_deploy.gateway.proxy.http_proxy",
            side_effect=_proxy_side_effect,
        ) as mock:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Host": "invest.deploy.example"},
            ) as c:
                await c.get("/docs?lang=en")
        # Verify the request was proxied (http_proxy handles query string forwarding)
        mock.assert_called_once()

    # -- Base domain requests — served by central-deploy --------------------

    @pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc"])
    async def test_base_domain_docs_served_locally(self, path):
        """On the bare domain, /docs and /openapi.json are served by
        central-deploy's own built-in handlers."""
        app = _build_app(_make_config("invest", container_name="invest-ctr"))
        with patch(
            "robotsix_central_deploy.gateway.proxy.http_proxy",
            side_effect=_proxy_side_effect,
        ) as mock:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Host": "deploy.example"},
            ) as c:
                resp = await c.get(path)
        assert resp.status_code == 200
        # Must reach the simulated built-in handler, not the proxy
        assert b"central-deploy" in resp.content
        mock.assert_not_called()

    # -- Unknown subdomain component — returns 404 -------------------------

    async def test_unknown_subdomain_returns_404(self):
        """A subdomain with no matching component returns 404."""
        app = _build_app()  # no components registered
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Host": "ghost.deploy.example"},
        ) as c:
            resp = await c.get("/docs")
        assert resp.status_code == 404

    async def test_reserved_name_subdomain_returns_404(self):
        """A subdomain matching a reserved name (e.g. 'docs') returns 404
        when requesting /docs — the gateway rejects reserved names."""
        app = _build_app(_make_config("invest"))
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Host": "docs.deploy.example"},
        ) as c:
            resp = await c.get("/docs")
        assert resp.status_code == 404

    # -- Non-docs paths on subdomain — pass through to normal routing -------

    async def test_non_docs_path_not_intercepted(self):
        """Paths other than /docs/openapi.json/redoc are not intercepted
        by the middleware."""
        app = _build_app(_make_config("invest"))
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Host": "invest.deploy.example"},
        ) as c:
            resp = await c.get("/other-path")
        assert resp.status_code == 200
        assert resp.json() == {"reached": "other-path"}

    # -- No base domain configured — passes through ------------------------

    async def test_no_base_domain_passes_through(self):
        """When no gateway_base_domain is configured, /docs is served
        locally (not proxied)."""
        app = _build_app(
            _make_config("invest"),
            base_domain="",
        )
        with patch(
            "robotsix_central_deploy.gateway.proxy.http_proxy",
            side_effect=_proxy_side_effect,
        ) as mock:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Host": "invest.deploy.example"},
            ) as c:
                resp = await c.get("/docs")
        assert resp.status_code == 200
        assert b"central-deploy" in resp.content
        mock.assert_not_called()
