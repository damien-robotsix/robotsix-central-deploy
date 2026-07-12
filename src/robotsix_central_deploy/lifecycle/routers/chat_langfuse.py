"""Chat-agent Langfuse component: server-side auth-injecting proxy.

The chat container never holds Langfuse credentials.  Instead, the deploy
server proxies Langfuse public-API read requests and injects HTTP Basic
Auth server-side, mirroring the auth-injection pattern already used for
the ``github`` virtual component (where the server mints GitHub App
tokens).

Exposes:
- ``GET /chat/langfuse/api/public/{path:path}`` — proxy to Langfuse with
  Basic Auth injected from server-side config.

Two Langfuse trace projects are supported — ``robotsix-chat`` (default)
and ``cognee`` — selected by the query parameter ``?project=``.
"""

from __future__ import annotations

import base64
import urllib.parse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response

from ..auth import verify_auth
from ..config import LifecycleConfig
from ..deps import _get_config

logger = __import__("logging").getLogger(__name__)

router = APIRouter(prefix="/chat/langfuse", tags=["chat-langfuse"])


def _basic_auth_header(username: str, password: str) -> str:
    """Return an ``Authorization: Basic ...`` header value for *username*/*password*."""
    raw = f"{username}:{password}"
    encoded = base64.b64encode(raw.encode()).decode()
    return f"Basic {encoded}"


@router.api_route(
    "/api/public/{path:path}",
    methods=["GET"],
    summary="Proxy a Langfuse public-API request with server-side auth",
)
async def langfuse_proxy(
    request: Request,
    path: str,
    project: str = Query(
        "robotsix-chat",
        description="Langfuse project to query: 'robotsix-chat' or 'cognee'.",
    ),
    _auth: None = Depends(verify_auth),
    config: LifecycleConfig = Depends(_get_config),
) -> Response:
    """Forward *path* to the configured Langfuse instance with Basic Auth.

    The ``?project=`` query parameter is consumed by this proxy and is
    **not** forwarded upstream — it selects which key pair to inject.
    All other query parameters are forwarded to Langfuse as-is.
    """
    # -- Choose credentials -------------------------------------------------
    if project == "cognee":
        username = config.langfuse_cognee_public_key
        password = config.langfuse_cognee_secret_key
    else:
        username = config.langfuse_chat_public_key
        password = config.langfuse_chat_secret_key

    if not username or not password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Langfuse credentials for project '{project}' are not configured.",
        )

    # -- Build target URL — preserve query string minus our ?project= param --
    base = config.langfuse_base_url.rstrip("/")

    # Sanitize the user-provided path to prevent URL injection (SSRF).
    _safe_path = path.lstrip("/")
    if ".." in _safe_path.split("/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path"
        )
    _safe_path = urllib.parse.quote(_safe_path, safe="/")
    target_url = urllib.parse.urljoin(base + "/", f"api/public/{_safe_path}")

    # Forward every query param except "project" (which is ours).
    params: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key != "project":
            params[key] = value
    if params:
        target_url += f"?{urllib.parse.urlencode(params)}"

    # -- Inject auth and forward --------------------------------------------
    headers: dict[str, str] = {}
    # Only forward safe headers from the client — never pass hop-by-hop or
    # auth-related headers through to the upstream.
    _SAFE_REQUEST_HEADERS: frozenset[str] = frozenset(
        {"accept", "accept-encoding", "accept-language", "user-agent"}
    )
    for key, value in request.headers.items():
        if key.lower() in _SAFE_REQUEST_HEADERS:
            headers[key] = value
    headers["authorization"] = _basic_auth_header(username, password)
    headers["host"] = base.split("://", 1)[1].split("/", 1)[0]

    logger.debug("langfuse proxy: %s → %s", request.url, target_url)

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            upstream = await client.get(target_url, headers=headers)
        except httpx.ConnectError:
            raise HTTPException(
                status_code=502, detail="Bad Gateway — Langfuse unreachable"
            )
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Gateway Timeout — Langfuse")
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Bad Gateway — {exc}")

        # -- Build response headers (strip hop-by-hop) ------------------
        resp_headers: dict[str, str] = {}
        _STRIP: frozenset[str] = frozenset(
            {"connection", "keep-alive", "transfer-encoding", "content-length"}
        )
        for key, value in upstream.headers.items():
            if key.lower() not in _STRIP:
                resp_headers[key] = value

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=resp_headers,
        )
