"""HTTP and WebSocket proxy helpers for the central-deploy gateway.

All managed services are reachable at ``deploy.robotsix.net/<name>/...``.
This module contains the low-level relay logic — the FastAPI routes live
in ``gateway/router.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, AsyncIterator

import httpx
from fastapi import HTTPException, WebSocket
from fastapi.responses import StreamingResponse
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

#: User-defined bridge network that central-deploy and every managed
#: container join so that the gateway can reach them by container name.
PROXY_NETWORK: str = "central-deploy-proxy"

#: Request headers that MUST NOT be forwarded upstream (hop-by-hop).
_HOP_BY_HOP: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",  # rewritten to the target host
    }
)

#: Response headers to strip from upstream before returning to the client.
_RESPONSE_STRIP: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "content-length",  # stale — every response is streamed
    }
)


def filter_hop_by_hop(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of *headers* with hop-by-hop entries removed."""
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


# ---------------------------------------------------------------------------
# HTTP proxy
# ---------------------------------------------------------------------------


async def http_proxy(
    request: Request,
    target_base_url: str,
    path: str,
    *,
    prefix: str = "",
) -> Response:
    """Forward an HTTP request to *target_base_url/path* and stream the response.

    Handles SSE transparently (content-type ``text/event-stream`` is
    forwarded chunk-by-chunk).  All other responses are also streamed to
    avoid buffering large payloads.
    """
    # Build target URL — include query string so e.g. ?page=2 is forwarded
    url = f"{target_base_url}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    # -- Build forwarded headers --------------------------------------------
    headers = filter_hop_by_hop(dict(request.headers))

    headers["x-forwarded-for"] = request.client.host if request.client else "unknown"
    headers["x-forwarded-proto"] = request.url.scheme
    headers["x-forwarded-host"] = request.headers.get("host", "")
    if prefix:
        headers["x-forwarded-prefix"] = prefix

    # -- Send to upstream ---------------------------------------------------
    client = httpx.AsyncClient(timeout=300.0)

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
        raise HTTPException(
            status_code=502, detail="Bad Gateway — upstream unreachable"
        )
    except httpx.TimeoutException:
        await client.aclose()
        raise HTTPException(status_code=504, detail="Gateway Timeout")
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Bad Gateway — {exc}")

    # -- Filter response headers --------------------------------------------
    resp_headers: dict[str, str] = {}
    for key, value in upstream_resp.headers.items():
        if key.lower() not in _RESPONSE_STRIP:
            resp_headers[key] = value

    # Rewrite absolute-path redirect Locations to keep the gateway prefix.
    # An app served at /<name>/ that redirects to e.g. "/board" would drop the
    # prefix and the browser would hit /board -> 404.  Prepend /<name> so
    # /mail/ -> Location:/board becomes /mail/board.
    if 300 <= upstream_resp.status_code < 400:
        _name = request.url.path.lstrip("/").split("/", 1)[0]
        if _name:
            _prefix = "/" + _name
            for _hk in list(resp_headers):
                if _hk.lower() == "location":
                    _loc = resp_headers[_hk]
                    if (
                        _loc.startswith("/")
                        and _loc != _prefix
                        and not _loc.startswith(_prefix + "/")
                    ):
                        resp_headers[_hk] = _prefix + _loc

    content_type: str = upstream_resp.headers.get("content-type", "")

    # -- Stream response body (and close client when done) ------------------
    async def _stream_and_close() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_resp.aiter_bytes():
                yield chunk
        finally:
            await client.aclose()

    if content_type.startswith("text/event-stream"):
        return StreamingResponse(
            _stream_and_close(),
            status_code=upstream_resp.status_code,
            media_type="text/event-stream",
            headers=resp_headers,
        )

    return StreamingResponse(
        _stream_and_close(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
    )


# ---------------------------------------------------------------------------
# WebSocket proxy
# ---------------------------------------------------------------------------


async def ws_proxy(
    client_ws: WebSocket,
    target_ws_url: str,
    *,
    additional_headers: Optional[dict[str, str]] = None,
) -> None:
    """Bidirectional relay between *client_ws* and a backend WebSocket at *target_ws_url*.

    Two asyncio tasks ferry bytes in each direction; both are cancelled as
    soon as either side disconnects.
    """
    import websockets

    async with websockets.connect(
        target_ws_url,
        additional_headers=additional_headers,
    ) as backend_ws:

        async def _client_to_backend() -> None:
            try:
                while True:
                    data = await client_ws.receive()
                    if data["type"] == "websocket.disconnect":
                        break
                    if "bytes" in data:
                        await backend_ws.send(data["bytes"])
                    elif "text" in data:
                        await backend_ws.send(data["text"])
            except websockets.exceptions.ConnectionClosed:
                pass
            except Exception:
                logger.debug("client→backend task exiting", exc_info=True)

        async def _backend_to_client() -> None:
            try:
                async for message in backend_ws:
                    if isinstance(message, bytes):
                        await client_ws.send_bytes(message)
                    else:
                        await client_ws.send_text(message)
            except websockets.exceptions.ConnectionClosed:
                pass
            except Exception:
                logger.debug("backend→client task exiting", exc_info=True)

        task_a = asyncio.create_task(_client_to_backend())
        task_b = asyncio.create_task(_backend_to_client())

        done, pending = await asyncio.wait(
            {task_a, task_b},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel whichever task is still running so the endpoint never hangs
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None and not isinstance(
                exc, websockets.exceptions.ConnectionClosed
            ):
                logger.warning("ws_proxy task error: %s", exc)
