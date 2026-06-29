"""Tests for the gateway reverse-proxy module.

Covers the low-level relay helpers in ``gateway/proxy.py`` (HTTP + WebSocket
proxying, header filtering, upstream error mapping) and the FastAPI routes in
``gateway/router.py`` (name resolution, redirect, HTTP/WebSocket dispatch).

All external calls (httpx, websockets) are mocked — no real network or Docker.
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

# Import the lifecycle package first so its server module wires up gateway.router
# in the correct order — importing gateway.router directly first triggers a
# circular import (router -> lifecycle.auth -> lifecycle.__init__ -> server ->
# gateway.router, still partially initialized).
import robotsix_central_deploy.lifecycle  # noqa: F401  isort: skip

from robotsix_central_deploy.gateway import proxy as proxy_mod
from robotsix_central_deploy.gateway import router as router_mod
from robotsix_central_deploy.gateway.proxy import (
    filter_hop_by_hop,
    http_proxy,
    ws_proxy,
)
from robotsix_central_deploy.gateway.router import (
    RESERVED_NAMES,
    _extract_subdomain_name,
    _resolve,
    gateway_http,
    gateway_router,
    gateway_ws,
)
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import ComponentConfig, PortMapping


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    component_id: str = "svc",
    *,
    container_name: str = "svc-ctr",
    ports: list[PortMapping] | None = None,
) -> ComponentConfig:
    return ComponentConfig(
        id=component_id,
        image="repo:v1",
        container_name=container_name,
        ports=[PortMapping(host=8080, container=9000)] if ports is None else ports,
    )


def _make_request(
    *,
    method: str = "GET",
    query: str = "",
    headers: dict[str, str] | None = None,
    client_host: str | None = "1.2.3.4",
    scheme: str = "http",
) -> MagicMock:
    """A MagicMock standing in for a Starlette ``Request``.

    ``request.stream()`` is never iterated because ``build_request`` is mocked,
    so a throwaway sentinel is fine.
    """
    req = MagicMock()
    req.method = method
    req.url.query = query
    req.url.scheme = scheme
    req.headers = headers if headers is not None else {"host": "deploy.example"}
    if client_host is None:
        req.client = None
    else:
        req.client = MagicMock()
        req.client.host = client_host
    req.stream = MagicMock(return_value=MagicMock())
    return req


def _make_upstream(
    *,
    status: int = 200,
    content_type: str = "text/plain",
    chunks: tuple[bytes, ...] = (b"hello",),
    extra_headers: dict[str, str] | None = None,
) -> MagicMock:
    headers = {
        "content-type": content_type,
        "content-length": "5",
        "transfer-encoding": "chunked",
        "x-custom": "kept",
    }
    if extra_headers:
        headers.update(extra_headers)

    async def _aiter() -> object:
        for chunk in chunks:
            yield chunk

    upstream = MagicMock()
    upstream.status_code = status
    upstream.headers = headers
    upstream.aiter_bytes = MagicMock(side_effect=_aiter)
    return upstream


def _make_client(send_result: object, *, side_effect: object = None) -> MagicMock:
    client = MagicMock()
    client.build_request = MagicMock(return_value="BUILT_REQUEST")
    if side_effect is not None:
        client.send = AsyncMock(side_effect=side_effect)
    else:
        client.send = AsyncMock(return_value=send_result)
    client.aclose = AsyncMock()
    return client


async def _drain(response: StreamingResponse) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


# ---------------------------------------------------------------------------
# filter_hop_by_hop
# ---------------------------------------------------------------------------


class TestFilterHopByHop:
    def test_strips_hop_by_hop_headers_case_insensitively(self):
        headers = {
            "Host": "example",
            "Connection": "keep-alive",
            "Upgrade": "websocket",
            "X-Real": "value",
            "content-type": "application/json",
        }
        out = filter_hop_by_hop(headers)
        assert "Host" not in out
        assert "Connection" not in out
        assert "Upgrade" not in out
        assert out["X-Real"] == "value"
        assert out["content-type"] == "application/json"

    def test_returns_copy_not_mutating_input(self):
        headers = {"host": "x", "keep": "me"}
        out = filter_hop_by_hop(headers)
        assert out == {"keep": "me"}
        assert headers == {"host": "x", "keep": "me"}


# ---------------------------------------------------------------------------
# http_proxy
# ---------------------------------------------------------------------------


class TestHttpProxy:
    async def test_streams_plain_response_and_forwards_headers(self):
        upstream = _make_upstream(
            content_type="application/json", chunks=(b"ab", b"cd")
        )
        client = _make_client(upstream)
        request = _make_request(query="page=2", headers={"host": "deploy.example"})

        with patch.object(proxy_mod.httpx, "AsyncClient", return_value=client):
            response = await http_proxy(request, "http://backend:9000", "api/items")

        assert isinstance(response, StreamingResponse)
        assert response.status_code == 200

        # Target URL includes path and forwarded query string.
        build_kwargs = client.build_request.call_args.kwargs
        assert build_kwargs["url"] == "http://backend:9000/api/items?page=2"
        assert build_kwargs["method"] == "GET"

        # Forwarded headers carry x-forwarded-* and drop hop-by-hop "host".
        fwd = build_kwargs["headers"]
        assert "host" not in fwd
        assert fwd["x-forwarded-for"] == "1.2.3.4"
        assert fwd["x-forwarded-proto"] == "http"
        assert fwd["x-forwarded-host"] == "deploy.example"

        # Response headers strip content-length / transfer-encoding but keep others.
        assert "content-length" not in response.headers
        assert "transfer-encoding" not in response.headers
        assert response.headers["x-custom"] == "kept"

        # Body streams through and the upstream client is closed afterwards.
        assert await _drain(response) == b"abcd"
        client.aclose.assert_awaited_once()

    async def test_sse_response_uses_event_stream_media_type(self):
        upstream = _make_upstream(
            content_type="text/event-stream", chunks=(b"data: x\n\n",)
        )
        client = _make_client(upstream)
        request = _make_request()

        with patch.object(proxy_mod.httpx, "AsyncClient", return_value=client):
            response = await http_proxy(request, "http://backend:9000", "events")

        assert isinstance(response, StreamingResponse)
        assert response.media_type == "text/event-stream"
        assert await _drain(response) == b"data: x\n\n"
        client.aclose.assert_awaited_once()

    async def test_no_query_string_builds_bare_url(self):
        upstream = _make_upstream()
        client = _make_client(upstream)
        request = _make_request(query="")

        with patch.object(proxy_mod.httpx, "AsyncClient", return_value=client):
            response = await http_proxy(request, "http://backend:9000", "path/here")

        assert client.build_request.call_args.kwargs["url"] == (
            "http://backend:9000/path/here"
        )
        await _drain(response)

    async def test_missing_client_uses_unknown_forwarded_for(self):
        upstream = _make_upstream()
        client = _make_client(upstream)
        request = _make_request(client_host=None)

        with patch.object(proxy_mod.httpx, "AsyncClient", return_value=client):
            await http_proxy(request, "http://backend:9000", "x")

        assert client.build_request.call_args.kwargs["headers"]["x-forwarded-for"] == (
            "unknown"
        )

    async def test_connect_error_maps_to_502(self):
        client = _make_client(None, side_effect=httpx.ConnectError("refused"))
        request = _make_request()

        with patch.object(proxy_mod.httpx, "AsyncClient", return_value=client):
            with pytest.raises(HTTPException) as exc_info:
                await http_proxy(request, "http://backend:9000", "x")

        assert exc_info.value.status_code == 502
        assert "unreachable" in exc_info.value.detail
        client.aclose.assert_awaited_once()

    async def test_timeout_maps_to_504(self):
        client = _make_client(None, side_effect=httpx.TimeoutException("slow"))
        request = _make_request()

        with patch.object(proxy_mod.httpx, "AsyncClient", return_value=client):
            with pytest.raises(HTTPException) as exc_info:
                await http_proxy(request, "http://backend:9000", "x")

        assert exc_info.value.status_code == 504
        client.aclose.assert_awaited_once()

    async def test_generic_http_error_maps_to_502(self):
        client = _make_client(None, side_effect=httpx.HTTPError("boom"))
        request = _make_request()

        with patch.object(proxy_mod.httpx, "AsyncClient", return_value=client):
            with pytest.raises(HTTPException) as exc_info:
                await http_proxy(request, "http://backend:9000", "x")

        assert exc_info.value.status_code == 502
        assert "boom" in exc_info.value.detail
        client.aclose.assert_awaited_once()

    async def test_x_forwarded_prefix_injected_when_prefix_set(self):
        """When *prefix* is passed, x-forwarded-prefix is included in
        the upstream request headers."""
        upstream = _make_upstream()
        client = _make_client(upstream)
        request = _make_request()

        with patch.object(proxy_mod.httpx, "AsyncClient", return_value=client):
            await http_proxy(
                request, "http://backend:9000", "api/items", prefix="/mail"
            )

        fwd = client.build_request.call_args.kwargs["headers"]
        assert fwd["x-forwarded-prefix"] == "/mail"

    async def test_x_forwarded_prefix_absent_when_prefix_empty(self):
        """When *prefix* is empty (default), x-forwarded-prefix is NOT set."""
        upstream = _make_upstream()
        client = _make_client(upstream)
        request = _make_request()

        with patch.object(proxy_mod.httpx, "AsyncClient", return_value=client):
            await http_proxy(request, "http://backend:9000", "x")

        fwd = client.build_request.call_args.kwargs["headers"]
        assert "x-forwarded-prefix" not in fwd


# ---------------------------------------------------------------------------
# ws_proxy — fakes for the optional ``websockets`` dependency
# ---------------------------------------------------------------------------


class _ConnectionClosed(Exception):
    pass


class _FakeBackendWS:
    """Async-iterable backend websocket with a recordable ``send``."""

    def __init__(
        self, incoming: list[object], *, iter_error: BaseException | None = None
    ):
        self._incoming = list(incoming)
        self._iter_error = iter_error
        self.send = AsyncMock()

    def __aiter__(self) -> "_FakeBackendWS":
        return self

    async def __anext__(self) -> object:
        if self._incoming:
            return self._incoming.pop(0)
        if self._iter_error is not None:
            raise self._iter_error
        raise StopAsyncIteration


class _FakeConnect:
    def __init__(self, backend: _FakeBackendWS):
        self._backend = backend

    async def __aenter__(self) -> _FakeBackendWS:
        return self._backend

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _fake_websockets_module(connect: MagicMock) -> types.ModuleType:
    mod = types.ModuleType("websockets")
    mod.connect = connect  # type: ignore[attr-defined]
    exc_mod = types.ModuleType("websockets.exceptions")
    exc_mod.ConnectionClosed = _ConnectionClosed  # type: ignore[attr-defined]
    mod.exceptions = exc_mod  # type: ignore[attr-defined]
    return mod


class TestWsProxy:
    async def test_relays_both_directions_until_disconnect(self):
        client_ws = MagicMock()
        client_ws.receive = AsyncMock(
            side_effect=[
                {"type": "websocket.receive", "bytes": b"to-backend"},
                {"type": "websocket.receive", "text": "hello"},
                {"type": "websocket.disconnect"},
            ]
        )
        client_ws.send_bytes = AsyncMock()
        client_ws.send_text = AsyncMock()

        backend = _FakeBackendWS([b"binary-msg", "text-msg"])
        connect = MagicMock(return_value=_FakeConnect(backend))
        fake_ws = _fake_websockets_module(connect)

        with patch.dict(sys.modules, {"websockets": fake_ws}):
            await ws_proxy(
                client_ws,
                "ws://backend:9000/path",
                additional_headers={"x-fwd": "1"},
            )

        connect.assert_called_once_with(
            "ws://backend:9000/path", additional_headers={"x-fwd": "1"}
        )
        # client→backend ferried both a bytes and a text frame
        backend.send.assert_any_await(b"to-backend")
        backend.send.assert_any_await("hello")
        # backend→client ferried a bytes and a text frame
        client_ws.send_bytes.assert_any_await(b"binary-msg")
        client_ws.send_text.assert_any_await("text-msg")

    async def test_connection_closed_on_both_sides_is_swallowed(self):
        client_ws = MagicMock()
        client_ws.receive = AsyncMock(side_effect=_ConnectionClosed())
        client_ws.send_bytes = AsyncMock()
        client_ws.send_text = AsyncMock()

        backend = _FakeBackendWS([], iter_error=_ConnectionClosed())
        connect = MagicMock(return_value=_FakeConnect(backend))
        fake_ws = _fake_websockets_module(connect)

        with patch.dict(sys.modules, {"websockets": fake_ws}):
            # Should return cleanly without raising.
            await ws_proxy(client_ws, "ws://backend:9000/path")

    async def test_generic_exceptions_in_both_tasks_are_logged_not_raised(self):
        client_ws = MagicMock()
        client_ws.receive = AsyncMock(side_effect=RuntimeError("client boom"))
        client_ws.send_bytes = AsyncMock()
        client_ws.send_text = AsyncMock()

        backend = _FakeBackendWS([], iter_error=RuntimeError("backend boom"))
        connect = MagicMock(return_value=_FakeConnect(backend))
        fake_ws = _fake_websockets_module(connect)

        with patch.dict(sys.modules, {"websockets": fake_ws}):
            # Both tasks swallow the generic error via their except-Exception arm.
            await ws_proxy(client_ws, "ws://backend:9000/path")

    async def test_pending_task_is_cancelled_when_one_side_finishes(self):
        client_ws = MagicMock()
        client_ws.receive = AsyncMock(side_effect=[{"type": "websocket.disconnect"}])
        client_ws.send_bytes = AsyncMock()
        client_ws.send_text = AsyncMock()

        class _BlockingBackend(_FakeBackendWS):
            async def __anext__(self) -> object:
                # Never yields — forces _backend_to_client to stay pending until
                # the client side finishes and cancels it.
                await asyncio.Event().wait()
                raise StopAsyncIteration

        backend = _BlockingBackend([])
        connect = MagicMock(return_value=_FakeConnect(backend))
        fake_ws = _fake_websockets_module(connect)

        with patch.dict(sys.modules, {"websockets": fake_ws}):
            await ws_proxy(client_ws, "ws://backend:9000/path")


# ---------------------------------------------------------------------------
# router._resolve
# ---------------------------------------------------------------------------


def _app_with_registry(registry: object | None) -> SimpleNamespace:
    state = SimpleNamespace()
    if registry is not None:
        state.registry = registry
    return SimpleNamespace(state=state)


class TestResolve:
    def test_reserved_name_returns_404(self):
        app = _app_with_registry(ComponentRegistry([_make_config("svc")]))
        for name in RESERVED_NAMES:
            config, status = _resolve(app, name)
            assert config is None
            assert status == 404

    def test_missing_registry_returns_503(self):
        app = _app_with_registry(None)
        config, status = _resolve(app, "svc")
        assert config is None
        assert status == 503

    def test_unknown_component_returns_404(self):
        app = _app_with_registry(ComponentRegistry([]))
        config, status = _resolve(app, "ghost")
        assert config is None
        assert status == 404

    def test_component_without_ports_returns_503(self):
        registry = ComponentRegistry([_make_config("svc", ports=[])])
        app = _app_with_registry(registry)
        config, status = _resolve(app, "svc")
        assert config is None
        assert status == 503

    def test_resolved_component_returns_config(self):
        cfg = _make_config("svc")
        app = _app_with_registry(ComponentRegistry([cfg]))
        config, status = _resolve(app, "svc")
        assert config is cfg
        assert status is None


# ---------------------------------------------------------------------------
# router HTTP endpoints (integration via ASGI)
# ---------------------------------------------------------------------------


def _build_app(*configs: ComponentConfig) -> FastAPI:
    app = FastAPI()
    app.include_router(gateway_router)
    app.state.config = SimpleNamespace(auth_required=False, gateway_base_domain="")
    app.state.registry = ComponentRegistry(list(configs))
    return app


class TestGatewayHttpRoutes:
    async def test_bare_name_redirects_with_trailing_slash(self):
        app = _build_app(_make_config("svc"))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/svc", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/svc/"

    async def test_http_path_dispatches_to_http_proxy(self):
        app = _build_app(_make_config("svc", container_name="svc-ctr"))

        async def _fake_proxy(req, target_base, path, *, prefix=""):
            return StreamingResponse(iter([b"proxied"]), status_code=299)

        with patch.object(router_mod, "http_proxy", side_effect=_fake_proxy) as mock:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.get("/svc/api/thing?x=1")

        assert resp.status_code == 299
        assert resp.content == b"proxied"
        # http_proxy got the container-derived base URL, the sub-path, and the
        # gateway prefix for x-forwarded-prefix.
        _, target_base, path = mock.call_args.args
        assert target_base == "http://svc-ctr:9000"
        assert path == "api/thing"
        assert mock.call_args.kwargs["prefix"] == "/svc"

    async def test_unknown_component_returns_404(self):
        app = _build_app(_make_config("svc"))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/ghost/page")
        assert resp.status_code == 404

    async def test_component_without_ports_returns_503(self):
        app = _build_app(_make_config("noports", ports=[]))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/noports/page")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# router.gateway_http — direct unit call (covers err_status raise path)
# ---------------------------------------------------------------------------


class TestGatewayHttpUnit:
    async def test_resolved_calls_http_proxy_with_target_base(self):
        cfg = _make_config("svc", container_name="svc-ctr")
        request = MagicMock()
        request.headers = {"host": "deploy.example"}
        request.app = _app_with_registry(ComponentRegistry([cfg]))
        request.app.state.config = SimpleNamespace(gateway_base_domain="")

        sentinel = StreamingResponse(iter([b""]))
        with patch.object(
            router_mod, "http_proxy", new=AsyncMock(return_value=sentinel)
        ) as mock:
            result = await gateway_http(request, "svc/deep/path", _auth=None)

        assert result is sentinel
        mock.assert_awaited_once_with(
            request, "http://svc-ctr:9000", "deep/path", prefix="/svc"
        )

    async def test_error_status_raises_http_exception(self):
        request = MagicMock()
        request.headers = {"host": "deploy.example"}
        request.app = _app_with_registry(ComponentRegistry([]))
        request.app.state.config = SimpleNamespace(gateway_base_domain="")
        with pytest.raises(HTTPException) as exc_info:
            await gateway_http(request, "ghost/x", _auth=None)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# router.gateway_ws — direct unit calls
# ---------------------------------------------------------------------------


def _make_ws(
    *,
    auth_required: bool,
    registry: object,
    token: str | None = None,
    valid: bool = True,
) -> MagicMock:
    ws = MagicMock()
    ws.app.state.config.auth_required = auth_required
    ws.app.state.config.gateway_base_domain = ""
    ws.app.state.registry = registry
    ws.cookies = {"session_token": token} if token is not None else {}
    ws.app.state.session_store = MagicMock()
    ws.app.state.session_store.validate = MagicMock(return_value=valid)
    ws.headers = {"host": "deploy.example", "connection": "upgrade"}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    return ws


class TestGatewayWs:
    async def test_accepts_and_proxies_when_authorized(self):
        cfg = _make_config("svc", container_name="svc-ctr")
        ws = _make_ws(auth_required=False, registry=ComponentRegistry([cfg]))

        with patch.object(router_mod, "ws_proxy", new=AsyncMock()) as mock:
            await gateway_ws(ws, "svc/stream/sub")

        ws.accept.assert_awaited_once()
        target = mock.call_args.args[1]
        assert target == "ws://svc-ctr:9000/stream/sub"
        # hop-by-hop "connection"/"host" headers are filtered out of the handshake.
        fwd = mock.call_args.kwargs["additional_headers"]
        assert "host" not in fwd
        assert "connection" not in fwd

    async def test_session_required_without_token_closes_4008(self):
        cfg = _make_config("svc")
        ws = _make_ws(auth_required=True, registry=ComponentRegistry([cfg]))

        with patch.object(router_mod, "ws_proxy", new=AsyncMock()) as mock:
            await gateway_ws(ws, "svc/x")

        ws.close.assert_awaited_once_with(code=4008)
        ws.accept.assert_not_awaited()
        mock.assert_not_awaited()

    async def test_session_required_with_valid_token_proceeds(self):
        cfg = _make_config("svc", container_name="svc-ctr")
        ws = _make_ws(
            auth_required=True,
            registry=ComponentRegistry([cfg]),
            token="good",
            valid=True,
        )

        with patch.object(router_mod, "ws_proxy", new=AsyncMock()) as mock:
            await gateway_ws(ws, "svc/x")

        ws.accept.assert_awaited_once()
        mock.assert_awaited_once()

    async def test_unknown_component_closes_4004(self):
        ws = _make_ws(auth_required=False, registry=ComponentRegistry([]))

        with patch.object(router_mod, "ws_proxy", new=AsyncMock()) as mock:
            await gateway_ws(ws, "ghost/x")

        ws.close.assert_awaited_once_with(code=4004)
        mock.assert_not_awaited()

    async def test_component_without_ports_closes_4011(self):
        cfg = _make_config("noports", ports=[])
        ws = _make_ws(auth_required=False, registry=ComponentRegistry([cfg]))

        with patch.object(router_mod, "ws_proxy", new=AsyncMock()) as mock:
            await gateway_ws(ws, "noports/x")

        ws.close.assert_awaited_once_with(code=4011)
        mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# _extract_subdomain_name unit tests
# ---------------------------------------------------------------------------


class TestExtractSubdomainName:
    def _app(self, base_domain: str) -> SimpleNamespace:
        return SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(gateway_base_domain=base_domain)
            )
        )

    def test_returns_none_when_base_domain_empty(self):
        assert (
            _extract_subdomain_name({"host": "mail.deploy.example"}, self._app(""))
            is None
        )

    def test_returns_name_for_matching_host(self):
        assert (
            _extract_subdomain_name(
                {"host": "mail.deploy.example"}, self._app("deploy.example")
            )
            == "mail"
        )

    def test_returns_none_for_base_domain_itself(self):
        assert (
            _extract_subdomain_name(
                {"host": "deploy.example"}, self._app("deploy.example")
            )
            is None
        )

    def test_strips_port_from_host(self):
        assert (
            _extract_subdomain_name(
                {"host": "mail.deploy.example:443"}, self._app("deploy.example")
            )
            == "mail"
        )

    def test_returns_none_for_unrelated_host(self):
        assert (
            _extract_subdomain_name(
                {"host": "other.example.com"}, self._app("deploy.example")
            )
            is None
        )

    def test_returns_none_when_no_host_header(self):
        assert _extract_subdomain_name({}, self._app("deploy.example")) is None


# ---------------------------------------------------------------------------
# Subdomain routing integration tests (ASGI)
# ---------------------------------------------------------------------------


def _build_app_with_subdomain(
    *configs: ComponentConfig, base_domain: str = "test.example"
) -> FastAPI:
    app = FastAPI()
    app.include_router(gateway_router)
    app.state.config = SimpleNamespace(
        auth_required=False, gateway_base_domain=base_domain
    )
    app.state.registry = ComponentRegistry(list(configs))
    return app


class TestSubdomainRoutingHttp:
    async def test_root_path_proxied_with_no_prefix(self):
        app = _build_app_with_subdomain(_make_config("svc", container_name="svc-ctr"))

        async def _fp(req, target_base, path, *, prefix=""):
            return StreamingResponse(iter([b"ok"]), status_code=200)

        with patch.object(router_mod, "http_proxy", side_effect=_fp) as mock:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Host": "svc.test.example"},
            ) as c:
                resp = await c.get("/")
        assert resp.status_code == 200
        _, target_base, path = mock.call_args.args
        assert target_base == "http://svc-ctr:9000"
        assert path == ""
        assert mock.call_args.kwargs.get("prefix", "") == ""

    async def test_sub_path_proxied_with_no_prefix(self):
        app = _build_app_with_subdomain(_make_config("svc", container_name="svc-ctr"))

        async def _fp(req, target_base, path, *, prefix=""):
            return StreamingResponse(iter([b"ok"]), status_code=200)

        with patch.object(router_mod, "http_proxy", side_effect=_fp) as mock:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Host": "svc.test.example"},
            ) as c:
                resp = await c.get("/static/board.css")
        assert resp.status_code == 200
        _, _, path = mock.call_args.args
        assert path == "static/board.css"
        assert mock.call_args.kwargs.get("prefix", "") == ""

    async def test_unknown_subdomain_component_returns_404(self):
        app = _build_app_with_subdomain(_make_config("svc"))
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Host": "ghost.test.example"},
        ) as c:
            resp = await c.get("/board")
        assert resp.status_code == 404

    async def test_non_subdomain_host_falls_back_to_path_prefix(self):
        app = _build_app_with_subdomain(_make_config("svc", container_name="svc-ctr"))

        async def _fp(req, target_base, path, *, prefix=""):
            return StreamingResponse(iter([b"ok"]), status_code=200)

        with patch.object(router_mod, "http_proxy", side_effect=_fp) as mock:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Host": "test.example"},  # base domain, no subdomain
            ) as c:
                resp = await c.get("/svc/api")
        assert resp.status_code == 200
        _, _, path = mock.call_args.args
        assert path == "api"
        assert mock.call_args.kwargs["prefix"] == "/svc"


# ---------------------------------------------------------------------------
# Location rewrite tests (added to TestHttpProxy)
# ---------------------------------------------------------------------------


class TestLocationRewrite:
    async def test_location_not_rewritten_when_prefix_empty(self):
        """Subdomain routing (prefix='') must NOT rewrite upstream Location."""
        upstream = _make_upstream(status=302, extra_headers={"location": "/board"})
        client = _make_client(upstream)
        request = _make_request()
        with patch.object(proxy_mod.httpx, "AsyncClient", return_value=client):
            response = await http_proxy(
                request, "http://backend:9000", "move", prefix=""
            )
        await _drain(response)
        assert response.headers.get("location") == "/board"

    async def test_location_rewritten_when_prefix_set(self):
        """Path-prefix routing — upstream absolute Location receives the prefix."""
        upstream = _make_upstream(status=302, extra_headers={"location": "/board"})
        client = _make_client(upstream)
        request = _make_request(headers={"host": "deploy.example"})
        with patch.object(proxy_mod.httpx, "AsyncClient", return_value=client):
            response = await http_proxy(
                request, "http://backend:9000", "", prefix="/mail"
            )
        await _drain(response)
        assert response.headers.get("location") == "/mail/board"
