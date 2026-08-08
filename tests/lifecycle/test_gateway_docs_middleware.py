"""Tests for GatewayAwareDocsMiddleware (lifecycle/gateway_docs_middleware.py)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.gateway_docs_middleware import (
    GatewayAwareDocsMiddleware,
)
from robotsix_central_deploy.registry.models import ComponentConfig, PortMapping

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _component_config(
    container_name: str = "invest",
    port: int = 8000,
) -> ComponentConfig:
    """Return a minimal ComponentConfig for testing."""
    return ComponentConfig(
        id="invest",
        image="ghcr.io/example/invest:main",
        container_name=container_name,
        ports=[PortMapping(host=port, container=port, protocol="tcp")],
        mounts=[],
        env={},
    )


def _build_app(
    base_domain: str = "deploy.example",
    *,
    registry: dict[str, ComponentConfig] | None = None,
    gateway_router_installed: bool = True,
) -> FastAPI:
    """Build a minimal FastAPI app with GatewayAwareDocsMiddleware installed.

    When *gateway_router_installed* is True, a catch-all route is registered
    (simulating the gateway router's catch-all) so that pass-through requests
    don't 404.
    """
    app = FastAPI(docs_url="/docs", openapi_url="/openapi.json")

    if gateway_router_installed:

        @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
        async def catch_all(path: str) -> dict[str, str]:
            return {"handler": "gateway", "path": path}

    app.state.config = SimpleNamespace(gateway_base_domain=base_domain)
    app.state.registry = registry or {}
    app.add_middleware(GatewayAwareDocsMiddleware)
    return app


def _client(app: FastAPI, host: str) -> AsyncClient:
    """Return an httpx AsyncClient pointed at *app* with the given *host* header."""
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Host": host},
    )


# ---------------------------------------------------------------------------
# Mock upstream response helper
# ---------------------------------------------------------------------------


def _fake_upstream_response(
    status_code: int = 200,
    body: bytes = b'{"openapi":"3.0.0"}',
    content_type: str = "application/json",
) -> MagicMock:
    """Return an AsyncMock that behaves like an httpx streaming response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": content_type}
    resp.aiter_bytes = MagicMock(return_value=_async_iter([body]))
    return resp


async def _async_iter(chunks: list[bytes]):
    """Yield each chunk for use with aiter_bytes mock."""
    for chunk in chunks:
        yield chunk


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGatewayAwareDocsMiddleware:
    """Middleware intercepts /docs and /openapi.json on component subdomains."""

    @pytest.mark.asyncio
    async def test_docs_on_subdomain_proxies_to_component(self):
        """GET /docs on a component subdomain proxies to the component."""
        registry = {"invest": _component_config()}
        app = _build_app(registry=registry)
        upstream = _fake_upstream_response(
            body=b"<html>Invest Docs</html>", content_type="text/html"
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.send = AsyncMock(return_value=upstream)
            mock_client.aclose = AsyncMock()
            mock_client.build_request = MagicMock(
                return_value=httpx.Request("GET", "http://invest:8000/docs")
            )

            async with _client(app, "invest.deploy.example") as client:
                resp = await client.get("/docs")

        assert resp.status_code == 200
        assert resp.text == "<html>Invest Docs</html>"
        assert resp.headers["content-type"] == "text/html"

    @pytest.mark.asyncio
    async def test_openapi_json_on_subdomain_proxies_to_component(self):
        """GET /openapi.json on a component subdomain proxies to the component."""
        registry = {"invest": _component_config()}
        app = _build_app(registry=registry)
        upstream = _fake_upstream_response(
            body=b'{"openapi":"3.0.0","info":{"title":"Invest"}}'
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.send = AsyncMock(return_value=upstream)
            mock_client.aclose = AsyncMock()
            mock_client.build_request = MagicMock(
                return_value=httpx.Request("GET", "http://invest:8000/openapi.json")
            )

            async with _client(app, "invest.deploy.example") as client:
                resp = await client.get("/openapi.json")

        assert resp.status_code == 200
        data = resp.json()
        assert data["info"]["title"] == "Invest"

    @pytest.mark.asyncio
    async def test_docs_on_base_domain_passes_through(self):
        """GET /docs on the base domain (no subdomain) serves central-deploy docs."""
        registry = {"invest": _component_config()}
        app = _build_app(registry=registry)

        async with _client(app, "deploy.example") as client:
            resp = await client.get("/docs")

        # FastAPI's own /docs returns a 200 HTML page (not JSON from catch-all)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_non_docs_path_on_subdomain_passes_through(self):
        """GET /services on a component subdomain passes through to gateway."""
        registry = {"invest": _component_config()}
        app = _build_app(registry=registry)

        async with _client(app, "invest.deploy.example") as client:
            resp = await client.get("/services")

        assert resp.status_code == 200
        data = resp.json()
        assert data["handler"] == "gateway"
        assert data["path"] == "services"

    @pytest.mark.asyncio
    async def test_docs_on_unknown_subdomain_passes_through(self):
        """GET /docs on an unknown subdomain passes through."""
        app = _build_app(registry={})

        async with _client(app, "nonexistent.deploy.example") as client:
            resp = await client.get("/docs")

        # Passes through to FastAPI's own /docs endpoint
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_docs_on_reserved_name_passes_through(self):
        """GET /docs on a reserved-name subdomain passes through."""
        registry = {
            "docs": _component_config(container_name="docs-svc"),
        }
        app = _build_app(registry=registry)

        async with _client(app, "docs.deploy.example") as client:
            resp = await client.get("/docs")

        # "docs" is reserved — passes through to central-deploy docs
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_upstream_unreachable_returns_502(self):
        """When the component container is unreachable, return 502."""
        registry = {"invest": _component_config()}
        app = _build_app(registry=registry)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.send = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_client.aclose = AsyncMock()
            mock_client.build_request = MagicMock(
                return_value=httpx.Request("GET", "http://invest:8000/docs")
            )

            async with _client(app, "invest.deploy.example") as client:
                resp = await client.get("/docs")

        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_upstream_timeout_returns_504(self):
        """When the upstream times out, return 504."""
        registry = {"invest": _component_config()}
        app = _build_app(registry=registry)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.send = AsyncMock(
                side_effect=httpx.TimeoutException("timed out")
            )
            mock_client.aclose = AsyncMock()
            mock_client.build_request = MagicMock(
                return_value=httpx.Request("GET", "http://invest:8000/docs")
            )

            async with _client(app, "invest.deploy.example") as client:
                resp = await client.get("/docs")

        assert resp.status_code == 504

    @pytest.mark.asyncio
    async def test_query_string_forwarded_to_component(self):
        """Query strings on /docs are forwarded to the component."""
        registry = {"invest": _component_config()}
        app = _build_app(registry=registry)
        upstream = _fake_upstream_response(body=b'{"openapi":"3.0.0"}')

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            mock_client.send = AsyncMock(return_value=upstream)
            mock_client.aclose = AsyncMock()
            mock_client.build_request = MagicMock(
                return_value=httpx.Request(
                    "GET", "http://invest:8000/openapi.json?version=2"
                )
            )

            async with _client(app, "invest.deploy.example") as client:
                resp = await client.get("/openapi.json?version=2")

        assert resp.status_code == 200
        # Verify the query string was included in the upstream request
        built_req = mock_client.build_request.call_args
        assert "version=2" in str(built_req)

    @pytest.mark.asyncio
    async def test_no_registry_passes_through(self):
        """When registry is not set on app.state, /docs passes through."""
        app = _build_app(registry=None)
        # Override registry to None (no .get method)
        app.state.registry = None

        async with _client(app, "invest.deploy.example") as client:
            resp = await client.get("/docs")

        # Passes through to FastAPI's own /docs
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_localhost_passes_through(self):
        """Requests from localhost pass through (no subdomain match)."""
        registry = {"invest": _component_config()}
        app = _build_app(registry=registry)

        async with _client(app, "localhost:8000") as client:
            resp = await client.get("/docs")

        # localhost doesn't match component subdomain — pass through
        assert resp.status_code == 200
