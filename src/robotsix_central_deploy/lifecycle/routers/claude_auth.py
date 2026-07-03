"""Claude auth endpoints — credential management for the ``claude-auth`` volume.

``GET  /claude-auth/status``           — check whether valid credentials exist.
``POST /claude-auth/login``            — start interactive OAuth login; returns OAuth URL.
``POST /claude-auth/login/complete``   — submit the authorization code to complete login.
``POST /claude-auth/login/cancel``     — cancel an in-progress login (kill helper).
``POST /claude-auth/credentials``      — paste credentials JSON directly.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import _get_backend, _get_config
from ..config import LifecycleConfig
from ...registry.settings_store import SystemSettingsStore
from ..schemas import (
    ClaudeAuthStatusResponse,
    ClaudeAuthLoginResponse,
    ClaudeAuthCompleteRequest,
    ClaudeAuthCompleteResponse,
    ClaudeAuthCredentialsRequest,
    ClaudeAuthCredentialsResponse,
)

router = APIRouter(tags=["claude-auth"])

CLAUDE_AUTH_VOLUME = "claude-auth"
DEFAULT_HELPER_IMAGE = "ghcr.io/damien-robotsix/robotsix-chat:main"

logger = logging.getLogger(__name__)


def _resolve_helper_image(
    settings_store: SystemSettingsStore, config: LifecycleConfig
) -> str:
    """Return the configured Claude auth helper image, falling back to the default."""
    effective = settings_store.overlay(config)
    helper = getattr(effective, "claude_auth_helper_image", "") or ""
    return helper.strip() or DEFAULT_HELPER_IMAGE


# ---------------------------------------------------------------------------
# GET /claude-auth/status
# ---------------------------------------------------------------------------


@router.get("/claude-auth/status", response_model=ClaudeAuthStatusResponse)
async def get_claude_auth_status(
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ClaudeAuthStatusResponse:
    """Return the current Claude authentication status."""
    try:
        result = await backend.check_claude_auth(CLAUDE_AUTH_VOLUME)
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Claude auth not supported by this backend. Use the Docker SDK backend.",
        )
    return ClaudeAuthStatusResponse(**result)


# ---------------------------------------------------------------------------
# POST /claude-auth/login — start interactive OAuth login
# ---------------------------------------------------------------------------


@router.post("/claude-auth/login", response_model=ClaudeAuthLoginResponse)
async def start_claude_login(
    request: Request,
    backend: ExecutionBackend = Depends(_get_backend),
    config: LifecycleConfig = Depends(_get_config),
    _auth: None = Depends(verify_auth),
) -> ClaudeAuthLoginResponse:
    """Spawn a helper container that runs ``claude login`` and returns the
    OAuth authorization URL.  The operator must visit that URL to authorize,
    then paste the authorization code into ``POST /claude-auth/login/complete``.
    """
    settings_store: SystemSettingsStore = request.app.state.settings_store
    helper_image = _resolve_helper_image(settings_store, config)

    try:
        result = await backend.start_claude_login(CLAUDE_AUTH_VOLUME, helper_image)
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Claude auth not supported by this backend. Use the Docker SDK backend.",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return ClaudeAuthLoginResponse(**result)


# ---------------------------------------------------------------------------
# POST /claude-auth/login/complete — submit authorization code
# ---------------------------------------------------------------------------


@router.post("/claude-auth/login/complete", response_model=ClaudeAuthCompleteResponse)
async def complete_claude_login(
    body: ClaudeAuthCompleteRequest,
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ClaudeAuthCompleteResponse:
    """Feed the authorization code to the waiting helper container.

    The helper writes ``.credentials.json`` into the ``claude-auth`` volume
    on success and is removed afterward.
    """
    try:
        result = await backend.complete_claude_login(
            CLAUDE_AUTH_VOLUME, body.container_id, body.auth_code
        )
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Claude auth not supported by this backend. Use the Docker SDK backend.",
        )

    return ClaudeAuthCompleteResponse(**result)


# ---------------------------------------------------------------------------
# POST /claude-auth/login/cancel — cancel in-progress login
# ---------------------------------------------------------------------------


@router.post("/claude-auth/login/cancel", status_code=204)
async def cancel_claude_login(
    body: ClaudeAuthCompleteRequest,
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> None:
    """Kill and remove a running claude-login helper container."""
    try:
        await backend.cancel_claude_login(CLAUDE_AUTH_VOLUME, body.container_id)
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Claude auth not supported by this backend. Use the Docker SDK backend.",
        )


# ---------------------------------------------------------------------------
# POST /claude-auth/credentials — paste credentials JSON directly
# ---------------------------------------------------------------------------


@router.post("/claude-auth/credentials", response_model=ClaudeAuthCredentialsResponse)
async def write_claude_credentials(
    body: ClaudeAuthCredentialsRequest,
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ClaudeAuthCredentialsResponse:
    """Write credentials JSON directly into the ``claude-auth`` volume.

    The JSON is written to ``.credentials.json`` with ownership ``1000:1000``
    and mode ``0600``.
    """
    try:
        result = await backend.write_claude_credentials(
            CLAUDE_AUTH_VOLUME, body.credentials_json
        )
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Claude auth not supported by this backend. Use the Docker SDK backend.",
        )

    return ClaudeAuthCredentialsResponse(**result)
