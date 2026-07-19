"""Chat-agent Langfuse component: server-side auth-injecting proxy.

The chat container never holds Langfuse credentials.  Instead, the deploy
server proxies Langfuse public-API read requests and injects HTTP Basic
Auth server-side, mirroring the auth-injection pattern already used for
the ``github`` virtual component (where the server mints GitHub App
tokens).

Exposes:
- ``GET /chat/langfuse/projects`` — list configured project aliases
- ``GET /chat/langfuse/{project}/traces`` — proxy to Langfuse ``GET /api/public/traces``
- ``GET /chat/langfuse/{project}/traces/{trace_id}`` — single trace detail
- ``GET /chat/langfuse/{project}/observations`` — proxy to Langfuse ``GET /api/public/observations``
- ``GET /chat/langfuse/{project}/observations/{observation_id}`` — single observation

Project aliases and credentials are configured via
``LifecycleConfig.langfuse_projects`` (dict of alias → {public_key,
secret_key}).  A legacy fallback reads the six per-project config fields
(``langfuse_chat_public_key``, …) for backward compatibility with
existing deployments that haven't migrated to the dict form yet.
"""

from __future__ import annotations

import base64
import urllib.parse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import Response

from ..auth import verify_auth
from ..config import LangfuseProjectCreds, LifecycleConfig
from ..deps import _get_config

logger = __import__("logging").getLogger(__name__)

router = APIRouter(tags=["chat-langfuse"])

# ---------------------------------------------------------------------------
# Project → config-key resolution
# ---------------------------------------------------------------------------


def _build_project_creds(config: LifecycleConfig) -> dict[str, LangfuseProjectCreds]:
    """Build the full project-alias → credentials map from config.

    Reads ``langfuse_projects`` first (the data-driven form).  Falls back
    to the six legacy per-project config fields for backward compatibility
    with existing deployments that haven't migrated yet.
    """
    result: dict[str, LangfuseProjectCreds] = dict(config.langfuse_projects)

    # Legacy fallback: per-project fields for robotsix-chat, cognee, robotsix-mill.
    _LEGACY_MAP: dict[str, tuple[str, str]] = {
        "robotsix-chat": ("langfuse_chat_public_key", "langfuse_chat_secret_key"),
        "cognee": ("langfuse_cognee_public_key", "langfuse_cognee_secret_key"),
        "robotsix-mill": ("langfuse_mill_public_key", "langfuse_mill_secret_key"),
    }
    for alias, (pk_attr, sk_attr) in _LEGACY_MAP.items():
        if alias in result:
            continue  # new-style entry takes precedence
        pk: str = getattr(config, pk_attr, "")
        sk: str = getattr(config, sk_attr, "")
        result[alias] = LangfuseProjectCreds(public_key=pk, secret_key=sk)

    return result


def _resolve_project_keys(config: LifecycleConfig, project: str) -> tuple[str, str]:
    """Return ``(public_key, secret_key)`` for *project*.

    Raises:
        HTTPException(404): unknown project alias.
        HTTPException(503): known alias but keys are not configured.
    """
    creds = _build_project_creds(config).get(project)
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown Langfuse project alias '{project}'.",
        )
    if not creds.public_key or not creds.secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Langfuse credentials for project '{project}' are not configured.",
        )
    return creds.public_key, creds.secret_key


def _basic_auth_header(username: str, password: str) -> str:
    """Return an ``Authorization: Basic ...`` header value for *username*/*password*."""
    raw = f"{username}:{password}"
    encoded = base64.b64encode(raw.encode()).decode()
    return f"Basic {encoded}"


# ---------------------------------------------------------------------------
# Shared proxy helper
# ---------------------------------------------------------------------------


async def _proxy_to_langfuse(
    request: Request,
    config: LifecycleConfig,
    project: str,
    api_path: str,
    extra_params: dict[str, str] | None = None,
) -> Response:
    """Forward *api_path* to Langfuse with server-side Basic Auth.

    *api_path* is appended to the configured ``langfuse_base_url`` (e.g.
    ``/api/public/traces``).  Query parameters from the original request
    are forwarded as-is.  *extra_params* are merged in (and override
    client-supplied params when keys collide).

    The ``limit`` query parameter is capped at 100 server-side.
    """
    public_key, secret_key = _resolve_project_keys(config, project)

    if not config.langfuse_base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="langfuse_base_url is not configured",
        )

    # -- Build target URL ---------------------------------------------------
    base_url = httpx.URL(config.langfuse_base_url.rstrip("/"))

    # Forward every query param from the original request, capping limit.
    params: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key == "limit":
            try:
                ival = int(value)
            except ValueError, TypeError:
                params[key] = value
            else:
                params[key] = str(min(ival, 100))
        else:
            params[key] = value

    # Merge extra params (caller-supplied, e.g. for path-param extraction).
    if extra_params:
        params.update(extra_params)

    # Sanitize the api_path against path traversal.
    _safe_path = api_path.lstrip("/")
    if ".." in _safe_path.split("/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path"
        )
    _safe_path = urllib.parse.quote(_safe_path, safe="/")

    target_url = base_url.copy_with(
        path=f"/api/public/{_safe_path}",
        params=params if params else None,
    )

    # -- Inject auth and forward --------------------------------------------
    headers: dict[str, str] = {}
    _SAFE_REQUEST_HEADERS: frozenset[str] = frozenset(
        {"accept", "accept-encoding", "accept-language", "user-agent"}
    )
    for key, value in request.headers.items():
        if key.lower() in _SAFE_REQUEST_HEADERS:
            headers[key] = value
    headers["authorization"] = _basic_auth_header(public_key, secret_key)
    headers["host"] = base_url.host

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


# ---------------------------------------------------------------------------
# GET /chat/langfuse/projects
# ---------------------------------------------------------------------------


@router.get(
    "/chat/langfuse/projects",
    summary="List configured Langfuse project aliases",
    responses={401: {"description": "Unauthorized"}},
)
async def list_projects(
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> list[str]:
    """Return the Langfuse project aliases whose key pairs are configured.

    Only projects with both a public key and a secret key set are listed.
    """
    projects = _build_project_creds(config)
    return [
        alias
        for alias, creds in projects.items()
        if creds.public_key and creds.secret_key
    ]


# ---------------------------------------------------------------------------
# GET /chat/langfuse/{project}/traces
# ---------------------------------------------------------------------------


@router.get(
    "/chat/langfuse/{project}/traces",
    summary="List Langfuse traces for a project",
    responses={
        404: {"description": "Unknown project alias"},
        503: {"description": "Project keys not configured"},
    },
)
async def list_traces(
    request: Request,
    project: str = Path(..., description="Langfuse project alias"),
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> Response:
    """Proxy ``GET /api/public/traces`` to Langfuse.

    Common query parameters (``limit``, ``page``, ``tags``, ``name``,
    ``sessionId``, ``fromTimestamp``) are forwarded.  ``limit`` is capped
    at 100 server-side.
    """
    return await _proxy_to_langfuse(request, config, project, "traces")


# ---------------------------------------------------------------------------
# GET /chat/langfuse/{project}/traces/{trace_id}
# ---------------------------------------------------------------------------


@router.get(
    "/chat/langfuse/{project}/traces/{trace_id}",
    summary="Get a single Langfuse trace",
    responses={
        404: {"description": "Unknown project alias or trace not found"},
        503: {"description": "Project keys not configured"},
    },
)
async def get_trace(
    request: Request,
    project: str = Path(..., description="Langfuse project alias"),
    trace_id: str = Path(..., description="Langfuse trace ID"),
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> Response:
    """Proxy ``GET /api/public/traces/{trace_id}`` to Langfuse."""
    return await _proxy_to_langfuse(request, config, project, f"traces/{trace_id}")


# ---------------------------------------------------------------------------
# GET /chat/langfuse/{project}/observations
# ---------------------------------------------------------------------------


@router.get(
    "/chat/langfuse/{project}/observations",
    summary="List Langfuse observations for a project",
    responses={
        404: {"description": "Unknown project alias"},
        503: {"description": "Project keys not configured"},
    },
)
async def list_observations(
    request: Request,
    project: str = Path(..., description="Langfuse project alias"),
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> Response:
    """Proxy ``GET /api/public/observations`` to Langfuse.

    Common query parameters (``limit``, ``page``, ``name``, ``type``,
    ``traceId``, ``fromStartTime``) are forwarded.  ``limit`` is capped
    at 100 server-side.
    """
    return await _proxy_to_langfuse(request, config, project, "observations")


# ---------------------------------------------------------------------------
# GET /chat/langfuse/{project}/observations/{observation_id}
# ---------------------------------------------------------------------------


@router.get(
    "/chat/langfuse/{project}/observations/{observation_id}",
    summary="Get a single Langfuse observation",
    responses={
        404: {"description": "Unknown project alias or observation not found"},
        503: {"description": "Project keys not configured"},
    },
)
async def get_observation(
    request: Request,
    project: str = Path(..., description="Langfuse project alias"),
    observation_id: str = Path(..., description="Langfuse observation ID"),
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> Response:
    """Proxy ``GET /api/public/observations/{observation_id}`` to Langfuse."""
    return await _proxy_to_langfuse(
        request, config, project, f"observations/{observation_id}"
    )
