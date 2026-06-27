"""Gateway routes — reverse-proxy ``deploy.robotsix.net/<name>/...`` → managed containers.

Registered LAST on the FastAPI app so that built-in routes (``/health``,
``/services``, …) match before the catch-all ``/{name}`` prefix.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ..registry.models import ComponentConfig
from .proxy import filter_hop_by_hop, http_proxy, ws_proxy

logger = logging.getLogger(__name__)

gateway_router = APIRouter(tags=["gateway"])

#: Names that MUST NOT be used as component slugs — they would shadow
#: central-deploy's own endpoints.
RESERVED_NAMES: frozenset[str] = frozenset({
    "ui", "health", "services", "onboard",
    "docs", "openapi.json", "redoc",
    "disk",  # GET /disk
})


# ---------------------------------------------------------------------------
# Internal helper — resolve name → ComponentConfig
# ---------------------------------------------------------------------------


def _resolve(
    app,  # FastAPI / Starlette app
    name: str,
) -> tuple[Optional[ComponentConfig], Optional[int]]:
    """Look up *name* in the component registry.

    Returns ``(config, None)`` on success, ``(None, http_status)`` on failure.
    """
    if name in RESERVED_NAMES:
        return None, 404

    registry = getattr(app.state, "registry", None)
    if registry is None:
        return None, 503

    config: Optional[ComponentConfig] = registry.get(name)
    if config is None:
        return None, 404

    if not config.ports:
        return None, 503

    return config, None


# ---------------------------------------------------------------------------
# HTTP redirect: /<name>  →  /<name>/
# ---------------------------------------------------------------------------


@gateway_router.get("/{name}")
async def gateway_index_redirect(name: str) -> RedirectResponse:
    """Redirect bare ``/<name>`` → ``/<name>/`` so relative asset paths resolve."""
    return RedirectResponse(url=f"/{name}/", status_code=307)


# ---------------------------------------------------------------------------
# WebSocket: /<name>/<path>
# ---------------------------------------------------------------------------


@gateway_router.websocket("/{name}/{path:path}")
async def gateway_ws(websocket: WebSocket, name: str, path: str) -> None:
    config, err_status = _resolve(websocket.app, name)
    if err_status is not None:
        # Map HTTP-style status to a valid WebSocket close code (RFC 6455).
        # 4004 = application-defined "not found"; 4011 = "service unavailable".
        ws_code: int = 4004 if err_status == 404 else 4011
        await websocket.close(code=ws_code)
        return

    # Build target WebSocket URL — Docker container name resolves via the
    # proxy network to the container's internal IP.
    target = f"ws://{config.container_name}:{config.ports[0].container}/{path}"

    # Forward non-hop-by-hop handshake headers
    fwd_headers = filter_hop_by_hop(dict(websocket.headers))

    await websocket.accept()
    await ws_proxy(websocket, target, additional_headers=fwd_headers)


# ---------------------------------------------------------------------------
# HTTP catch-all: /<name>/<path>
# ---------------------------------------------------------------------------


@gateway_router.api_route(
    "/{name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def gateway_http(request: Request, name: str, path: str) -> Response:
    config, err_status = _resolve(request.app, name)
    if err_status:
        raise HTTPException(status_code=err_status)
    target_base = f"http://{config.container_name}:{config.ports[0].container}"
    return await http_proxy(request, target_base, path)
