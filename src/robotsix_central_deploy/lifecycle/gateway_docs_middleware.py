"""Gateway-aware docs middleware.

Intercepts ``/docs`` and ``/openapi.json`` on component subdomains and
proxies to the component container, so that visiting e.g.
``invest.deploy.robotsix.net/docs`` serves the *component's* own API docs
instead of central-deploy's Lifecycle API docs (which expose
mutating/destructive endpoints under component vhosts).
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from ..gateway.router import RESERVED_NAMES, _extract_subdomain_name, _resolve
from ..registry.models import ComponentConfig

logger = logging.getLogger(__name__)

#: Paths intercepted when the request targets a component subdomain.
_INTERCEPT_PATHS: frozenset[str] = frozenset({"/docs", "/openapi.json"})

#: Upstream response headers to strip before returning to the client.
_RESPONSE_STRIP: frozenset[str] = frozenset(
    {"connection", "keep-alive", "transfer-encoding"}
)


class GatewayAwareDocsMiddleware(BaseHTTPMiddleware):
    """Intercept ``/docs`` and ``/openapi.json`` on component subdomains.

    When a request arrives at a component subdomain
    (e.g. ``invest.deploy.robotsix.net``) and targets ``/docs`` or
    ``/openapi.json``, this middleware proxies the request to the
    component's own container so the component's docs are served.

    Requests that are **not** targeting a known component subdomain — or
    that target any path other than ``/docs`` / ``/openapi.json`` — pass
    through unchanged.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # -- Only intercept docs / openapi paths ------------------------
        if request.url.path not in _INTERCEPT_PATHS:
            return await call_next(request)

        # -- Check for a component subdomain ---------------------------
        name = _extract_subdomain_name(request.headers, request.app)
        if name is None or name in RESERVED_NAMES:
            return await call_next(request)

        # -- Resolve the component -------------------------------------
        config, err_status = _resolve(request.app, name)
        if err_status is not None or config is None:
            logger.debug(
                "GatewayAwareDocsMiddleware: component %r not resolvable "
                "(status=%s); passing through",
                name,
                err_status,
            )
            return await call_next(request)

        # -- Proxy to the component container --------------------------
        target_base = (
            f"http://{config.container_name}:{config.ports[0].container}"
        )
        url = f"{target_base}{request.url.path}"
        if request.url.query:
            url += f"?{request.url.query}"

        logger.debug(
            "GatewayAwareDocsMiddleware: proxying %s to %s",
            request.url.path,
            url,
        )
        return await self._proxy(request, url)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _proxy(self, request: Request, url: str) -> Response:
        """Forward *request* to *url* and stream the response back."""
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower()
            not in {"host", "connection", "keep-alive", "transfer-encoding"}
        }
        headers["x-forwarded-for"] = (
            request.client.host if request.client else "unknown"
        )
        headers["x-forwarded-proto"] = request.url.scheme
        headers["x-forwarded-host"] = request.headers.get("host", "")

        client = httpx.AsyncClient(timeout=30.0)

        try:
            upstream_resp = await client.send(
                client.build_request(
                    method=request.method,
                    url=url,
                    headers=headers,
                    content=request.stream(),
                ),
                stream=True,
            )
        except httpx.ConnectError:
            await client.aclose()
            logger.warning(
                "GatewayAwareDocsMiddleware: upstream unreachable at %s", url
            )
            return JSONResponse(
                status_code=502,
                content={"detail": "Bad Gateway — upstream unreachable"},
            )
        except httpx.TimeoutException:
            await client.aclose()
            return JSONResponse(
                status_code=504, content={"detail": "Gateway Timeout"}
            )
        except httpx.HTTPError as exc:
            await client.aclose()
            return JSONResponse(
                status_code=502, content={"detail": f"Bad Gateway — {exc}"}
            )

        resp_headers = {
            k: v
            for k, v in upstream_resp.headers.items()
            if k.lower() not in _RESPONSE_STRIP
        }

        async def _stream_and_close() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
            finally:
                await client.aclose()

        return StreamingResponse(
            _stream_and_close(),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )
