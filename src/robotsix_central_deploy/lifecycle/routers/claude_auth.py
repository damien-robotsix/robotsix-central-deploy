"""Claude auth endpoints — credential management for the ``claude-auth`` volume.

``GET  /claude-auth/status``           — check whether valid credentials exist.
``POST /claude-auth/login``            — start a PKCE OAuth login; returns the authorize URL.
``POST /claude-auth/login/complete``   — exchange the pasted authorization code for tokens.
``POST /claude-auth/login/cancel``     — discard an in-progress login session.
``POST /claude-auth/credentials``      — paste credentials JSON directly.

The login flow runs entirely inside central-deploy (no helper container):
the server generates a PKCE challenge, the operator authorizes in the
browser, Anthropic's callback page displays an authorization code, the
operator pastes it back, and the server exchanges it for OAuth tokens and
writes ``.credentials.json`` into the ``claude-auth`` volume.  A redirect
back to this dashboard is not possible — the OAuth client only whitelists
Anthropic's own callback page.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ..auth import verify_auth
from ..backends import ExecutionBackend
from ..deps import _get_backend
from ..schemas import (
    ClaudeAuthStatusResponse,
    ClaudeAuthLoginResponse,
    ClaudeAuthCancelRequest,
    ClaudeAuthCompleteRequest,
    ClaudeAuthCompleteResponse,
    ClaudeAuthCredentialsRequest,
    ClaudeAuthCredentialsResponse,
)

router = APIRouter(tags=["claude-auth"])

CLAUDE_AUTH_VOLUME = "claude-auth"

# OAuth parameters mirroring what `claude setup-token` (Claude Code CLI)
# uses for its device authorization flow.
OAUTH_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"  # noqa: S105 — URL, not a password
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # gitleaks:allow — public OAuth client id, not a secret
OAUTH_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
OAUTH_SCOPE = "user:inference"

LOGIN_SESSION_TTL_SECONDS = 600

# In-flight PKCE login sessions: login_id (= OAuth state) → (verifier, expiry).
_login_sessions: dict[str, tuple[str, float]] = {}

logger = logging.getLogger(__name__)


def _prune_login_sessions() -> None:
    now = time.monotonic()
    for key in [k for k, (_, exp) in _login_sessions.items() if exp < now]:
        _login_sessions.pop(key, None)


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
# POST /claude-auth/login — start PKCE OAuth login
# ---------------------------------------------------------------------------


@router.post("/claude-auth/login", response_model=ClaudeAuthLoginResponse)
async def start_claude_login(
    _auth: None = Depends(verify_auth),
) -> ClaudeAuthLoginResponse:
    """Generate a PKCE challenge and return the OAuth authorization URL.

    The operator visits the URL, authorizes, and pastes the code shown on
    Anthropic's callback page into ``POST /claude-auth/login/complete``.
    """
    from urllib.parse import urlencode

    _prune_login_sessions()

    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    state = secrets.token_urlsafe(32)
    _login_sessions[state] = (
        verifier,
        time.monotonic() + LOGIN_SESSION_TTL_SECONDS,
    )

    oauth_url = (
        OAUTH_AUTHORIZE_URL
        + "?"
        + urlencode(
            {
                "code": "true",
                "client_id": OAUTH_CLIENT_ID,
                "response_type": "code",
                "redirect_uri": OAUTH_REDIRECT_URI,
                "scope": OAUTH_SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
    )
    return ClaudeAuthLoginResponse(login_id=state, oauth_url=oauth_url)


# ---------------------------------------------------------------------------
# POST /claude-auth/login/complete — exchange authorization code
# ---------------------------------------------------------------------------


async def _exchange_code(auth_code: str, state: str, verifier: str) -> dict[str, Any]:
    """Exchange *auth_code* for OAuth tokens at the token endpoint.

    Returns the token payload dict.  Raises ``HTTPException`` on failure.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            OAUTH_TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": OAUTH_CLIENT_ID,
                "code": auth_code,
                "state": state,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
    if resp.status_code != 200:
        detail = resp.text[:500]
        try:
            detail = resp.json().get("error", {}).get("message", detail)
        except Exception:  # noqa: S110 — non-JSON error body; keep raw text
            pass
        raise HTTPException(
            status_code=400,
            detail=f"Token exchange failed ({resp.status_code}): {detail}",
        )
    payload: dict[str, Any] = resp.json()
    return payload


@router.post("/claude-auth/login/complete", response_model=ClaudeAuthCompleteResponse)
async def complete_claude_login(
    body: ClaudeAuthCompleteRequest,
    backend: ExecutionBackend = Depends(_get_backend),
    _auth: None = Depends(verify_auth),
) -> ClaudeAuthCompleteResponse:
    """Exchange the pasted authorization code for tokens and persist them.

    On success ``.credentials.json`` is written into the ``claude-auth``
    volume with ownership ``1000:1000`` and mode ``0600``.
    """
    _prune_login_sessions()
    session = _login_sessions.get(body.login_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail="Login session not found or expired. Start a new login.",
        )
    verifier, _ = session

    # The callback page displays the code as "<code>#<state>".
    auth_code = body.auth_code.strip()
    if "#" in auth_code:
        auth_code = auth_code.split("#", 1)[0]
    if not auth_code:
        raise HTTPException(status_code=400, detail="Authorization code is empty.")

    payload = await _exchange_code(auth_code, body.login_id, verifier)

    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token", "")
    expires_in = payload.get("expires_in")
    if not access_token:
        raise HTTPException(
            status_code=400,
            detail="Token exchange succeeded but no access token was returned.",
        )

    scopes = str(payload.get("scope", OAUTH_SCOPE)).split()
    credentials: dict[str, object] = {
        "claudeAiOauth": {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresAt": int((time.time() + float(expires_in or 0)) * 1000),
            "scopes": scopes,
        }
    }
    # Pass through optional account metadata when present.
    for src_key, dst_key in (
        ("subscription_type", "subscriptionType"),
        ("rate_limit_tier", "rateLimitTier"),
    ):
        if payload.get(src_key):
            credentials["claudeAiOauth"][dst_key] = payload[src_key]  # type: ignore[index]

    try:
        result = await backend.write_claude_credentials(
            CLAUDE_AUTH_VOLUME, json.dumps(credentials, indent=2)
        )
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="Claude auth not supported by this backend. Use the Docker SDK backend.",
        )
    if result.get("status") != "authenticated":
        return ClaudeAuthCompleteResponse(
            status="error",
            error=str(result.get("error", "Failed to write credentials.")),
        )

    _login_sessions.pop(body.login_id, None)
    return ClaudeAuthCompleteResponse(status="authenticated")


# ---------------------------------------------------------------------------
# POST /claude-auth/login/cancel — discard in-progress login
# ---------------------------------------------------------------------------


@router.post("/claude-auth/login/cancel", status_code=204)
async def cancel_claude_login(
    body: ClaudeAuthCancelRequest,
    _auth: None = Depends(verify_auth),
) -> None:
    """Discard an in-progress PKCE login session."""
    _login_sessions.pop(body.login_id, None)


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
