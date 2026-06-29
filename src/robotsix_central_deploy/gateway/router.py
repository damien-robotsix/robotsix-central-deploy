"""Gateway routes — reverse-proxy ``deploy.robotsix.net/<name>/...`` → managed containers.

Registered LAST on the FastAPI app so that built-in routes (``/health``,
``/services``, …) match before the catch-all ``/{name}`` prefix.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ..registry.models import ComponentConfig
from .proxy import filter_hop_by_hop, http_proxy, ws_proxy
from ..lifecycle.auth import verify_session

logger = logging.getLogger(__name__)

gateway_router = APIRouter(tags=["gateway"])

#: Names that MUST NOT be used as component slugs — they would shadow
#: central-deploy's own endpoints.
RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "ui",
        "health",
        "services",
        "onboard",
        "docs",
        "openapi.json",
        "redoc",
        "disk",  # GET /disk
        "settings",  # GET/PUT /settings
        "help",  # GET /help/deploy-contract
    }
)


# ---------------------------------------------------------------------------
# Internal helper — resolve name → ComponentConfig
# ---------------------------------------------------------------------------


def _resolve(
    app: Any,  # FastAPI / Starlette app
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
async def gateway_index_redirect(
    name: str, _auth: None = Depends(verify_session)
) -> RedirectResponse:
    """Redirect bare ``/<name>`` → ``/<name>/`` so relative asset paths resolve."""
    return RedirectResponse(url=f"/{name}/", status_code=307)


# ---------------------------------------------------------------------------
# WebSocket: /<name>/<path>
# ---------------------------------------------------------------------------


@gateway_router.websocket("/{name}/{path:path}")
async def gateway_ws(websocket: WebSocket, name: str, path: str) -> None:
    # --- Session auth ---
    app_cfg = websocket.app.state.config
    if app_cfg.auth_required:
        session_token = websocket.cookies.get("session_token")
        session_store = websocket.app.state.session_store
        if not session_token or not session_store.validate(session_token):
            await websocket.close(code=4008)  # RFC 6455 policy violation
            return

    # --- Component resolution (unchanged) ---
    config, err_status = _resolve(websocket.app, name)
    if err_status is not None:
        # Map HTTP-style status to a valid WebSocket close code (RFC 6455).
        # 4004 = application-defined "not found"; 4011 = "service unavailable".
        ws_code: int = 4004 if err_status == 404 else 4011
        await websocket.close(code=ws_code)
        return
    assert config is not None
    # Build target WebSocket URL — Docker container name resolves via the
    # proxy network to the container's internal IP.
    target = f"ws://{config.container_name}:{config.ports[0].container}/{path}"

    # Forward non-hop-by-hop handshake headers
    fwd_headers = filter_hop_by_hop(dict(websocket.headers))
    fwd_headers["x-forwarded-prefix"] = f"/{name}"

    await websocket.accept()
    await ws_proxy(websocket, target, additional_headers=fwd_headers)


# ---------------------------------------------------------------------------
# HTTP catch-all: /<name>/<path>
# ---------------------------------------------------------------------------


@gateway_router.api_route(
    "/{name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def gateway_http(
    request: Request,
    name: str,
    path: str,
    _auth: None = Depends(verify_session),
) -> Response:
    config, err_status = _resolve(request.app, name)
    if err_status:
        raise HTTPException(status_code=err_status)
    assert config is not None
    target_base = f"http://{config.container_name}:{config.ports[0].container}"
    return await http_proxy(request, target_base, path, prefix=f"/{name}")
