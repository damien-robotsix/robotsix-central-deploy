"""Gateway-aware middleware that routes /docs and /openapi.json to the
correct component on subdomain requests.

FastAPI's built-in ``/docs`` and ``/openapi.json`` endpoints are
registered at the app level and match *before* the gateway catch-all —
regardless of the ``Host`` header.  Without this middleware, a request
to ``invest.deploy.robotsix.net/docs`` renders central-deploy's own
Swagger UI instead of the invest component's API docs.

This middleware intercepts ``/docs`` and ``/openapi.json`` (and
``/redoc``) on component-subdomain requests and proxies them to the
target component's container, bypassing the built-in handlers.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

# Paths that FastAPI handles as built-in docs endpoints — these must be
# intercepted for subdomain requests.
_BUILTIN_DOCS_PATHS: frozenset[str] = frozenset({"/docs", "/openapi.json", "/redoc"})


class GatewayAwareDocsMiddleware:
    """Proxy /docs, /openapi.json, and /redoc to the target component on
    subdomain requests.

    On bare-domain requests (no subdomain), passes through to FastAPI's
    built-in handlers so central-deploy's own API docs are served normally.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path not in _BUILTIN_DOCS_PATHS:
            await self._app(scope, receive, send)
            return

        # Lazy import: gateway.router pulls in lifecycle modules, and this
        # module is imported during lifecycle.app start-up.
        from ..gateway.router import _extract_subdomain_name, _resolve

        headers = Headers(scope=scope)
        name = _extract_subdomain_name(headers, scope.get("app"))
        if name is None:
            # Bare domain — let FastAPI serve its own docs.
            await self._app(scope, receive, send)
            return

        config, err_status = _resolve(scope.get("app"), name)
        if err_status is not None:
            response = Response(status_code=err_status)
            await response(scope, receive, send)
            return
        assert config is not None

        target_base = f"http://{config.container_name}:{config.ports[0].container}"

        from ..gateway.proxy import http_proxy

        request = Request(scope, receive)
        proxy_response = await http_proxy(request, target_base, path)
        await proxy_response(scope, receive, send)
