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
# Internal helper — extract component name from Host subdomain
# ---------------------------------------------------------------------------


def _extract_subdomain_name(headers: Any, app: Any) -> Optional[str]:
    """Return the component name encoded in the Host subdomain, or ``None``.

    With ``gateway_base_domain="deploy.robotsix.net"``:

    * ``"mail.deploy.robotsix.net"``      → ``"mail"``
    * ``"mail.deploy.robotsix.net:443"``  → ``"mail"``  (port stripped)
    * ``"deploy.robotsix.net"``           → ``None``    (no subdomain)
    * ``"localhost"``                     → ``None``    (unrelated host)

    ``headers`` may be a plain ``dict`` or any object supporting ``.get(key, default)``.
    """
    base_domain: str = getattr(
        getattr(getattr(app, "state", None), "config", None),
        "gateway_base_domain",
        "",
    )
    if not base_domain:
        return None
    host: str = headers.get("host", "").split(":")[0].lower()
    suffix = "." + base_domain.lower()
    if not host.endswith(suffix):
        return None
    name = host[: -len(suffix)]
    return name or None


# ---------------------------------------------------------------------------
# HTTP root ("/") — subdomain-routed component root
# ---------------------------------------------------------------------------


@gateway_router.api_route(
    "/",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def gateway_http_root(
    request: Request,
    _auth: None = Depends(verify_session),
) -> Response:
    """Handle requests to ``/`` — only active for subdomain-routed components."""
    name = _extract_subdomain_name(request.headers, request.app)
    if not name:
        raise HTTPException(status_code=404)
    config, err_status = _resolve(request.app, name)
    if err_status:
        raise HTTPException(status_code=err_status)
    assert config is not None
    target_base = f"http://{config.container_name}:{config.ports[0].container}"
    return await http_proxy(request, target_base, "", prefix="")


# ---------------------------------------------------------------------------
# WebSocket catch-all: /{path:path}
# ---------------------------------------------------------------------------


@gateway_router.websocket("/{path:path}")
async def gateway_ws(websocket: WebSocket, path: str) -> None:
    # --- Session auth ---
    app_cfg = websocket.app.state.config
    if app_cfg.auth_required:
        session_token = websocket.cookies.get("session_token")
        session_store = websocket.app.state.session_store
        if not session_token or not session_store.validate(session_token):
            await websocket.close(code=4008)
            return

    # --- Component resolution ---
    name = _extract_subdomain_name(websocket.headers, websocket.app)
    is_subdomain = name is not None
    if not is_subdomain:
        # Path-prefix routing: first segment is the component name
        parts = path.split("/", 1)
        name = parts[0]
        path = parts[1] if len(parts) > 1 else ""

    assert name is not None  # narrowed above via is_subdomain / path split
    config, err_status = _resolve(websocket.app, name)
    if err_status is not None:
        ws_code: int = 4004 if err_status == 404 else 4011
        await websocket.close(code=ws_code)
        return
    assert config is not None

    target = f"ws://{config.container_name}:{config.ports[0].container}/{path}"
    fwd_headers = filter_hop_by_hop(dict(websocket.headers))
    if not is_subdomain:
        fwd_headers["x-forwarded-prefix"] = f"/{name}"

    await websocket.accept()
    await ws_proxy(websocket, target, additional_headers=fwd_headers)


# ---------------------------------------------------------------------------
# HTTP catch-all: /{path:path}
# ---------------------------------------------------------------------------


@gateway_router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def gateway_http(
    request: Request,
    path: str,
    _auth: None = Depends(verify_session),
) -> Response:
    # 1. Subdomain routing: Host = <name>.<gateway_base_domain>
    name = _extract_subdomain_name(request.headers, request.app)
    if name:
        config, err_status = _resolve(request.app, name)
        if err_status:
            raise HTTPException(status_code=err_status)
        assert config is not None
        target_base = f"http://{config.container_name}:{config.ports[0].container}"
        return await http_proxy(request, target_base, path, prefix="")

    # 2. Path-prefix routing (legacy): /<name>/...
    parts = path.split("/", 1)
    prefix_name = parts[0]
    rest: Optional[str] = parts[1] if len(parts) > 1 else None

    if rest is None:
        # Redirect bare /<name> → /<name>/ so relative assets resolve correctly
        return RedirectResponse(url=f"/{prefix_name}/", status_code=307)

    config, err_status = _resolve(request.app, prefix_name)
    if err_status:
        raise HTTPException(status_code=err_status)
    assert config is not None
    target_base = f"http://{config.container_name}:{config.ports[0].container}"
    return await http_proxy(request, target_base, rest, prefix=f"/{prefix_name}")
