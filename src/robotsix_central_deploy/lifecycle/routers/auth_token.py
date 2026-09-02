"""Mobile token-exchange endpoints for the fleet edge.

``GET  /auth/token``            — exchange an SSO session for a mobile bearer token.
``GET  /auth/login``            — start the mobile deep-link login handoff.
``GET  /auth/login/complete``   — finish the handoff: issue a token, redirect to the app.
``POST /chat/auth/mobile-token`` — validate a stored token; returns the TokenResponse shape.
``GET  /auth/validate``         — Traefik ForwardAuth: validate a bearer token.
``DELETE /auth/token/{token_id}`` — revoke a single token.
``POST  /auth/revoke-user``     — revoke all tokens for a user.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..auth import verify_auth
from ..deps import _get_token_store
from ..token_store import TokenStore

router = APIRouter(tags=["auth-token"])

logger = logging.getLogger(__name__)

#: Cookie carrying the app's deep-link target across the SSO round-trip.
#: tinyauth (v5.1.3) rebuilds its post-login ``redirect_uri`` from the
#: original request's scheme+host+PATH only — the query string is dropped —
#: so a ``?redirect_to=...`` parameter does not survive a cold login.  The
#: public ``/auth/login`` endpoint stashes the target in this cookie and
#: bounces to the bare, SSO-gated ``/auth/login/complete`` path instead.
_LOGIN_REDIRECT_COOKIE = "robotsix_login_redirect"
_LOGIN_REDIRECT_MAX_AGE = 600  # seconds — one login round-trip

_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*")

#: Schemes never allowed as a deep-link target: a web or script scheme would
#: turn the token handoff into an open redirector that leaks bearer tokens.
_FORBIDDEN_SCHEMES = frozenset(
    {"http", "https", "javascript", "data", "file", "vbscript", "blob"}
)


def _validate_deep_link(redirect_to: str) -> str:
    """Validate a mobile deep-link redirect target.

    Only absolute URIs with a custom (non-web) scheme are accepted —
    e.g. ``robotsixchat://auth/callback``.  Raises ``HTTPException(400)``
    otherwise.
    """
    if any(ord(c) < 0x21 for c in redirect_to):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="redirect_to must not contain whitespace or control characters.",
        )
    scheme = urlsplit(redirect_to).scheme.lower()
    if not _SCHEME_RE.fullmatch(scheme) or scheme in _FORBIDDEN_SCHEMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "redirect_to must be an absolute URI using the mobile app's "
                "custom URI scheme (web/script schemes are not allowed)."
            ),
        )
    return redirect_to


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TokenResponse(BaseModel):
    """Bearer token returned to the mobile app after SSO exchange."""

    access_token: str = Field(..., description="Bearer token for mobile API calls.")
    token_type: str = Field("Bearer", description="Always 'Bearer'.")
    scope: str = Field("chat", description="Token scope — chat access only.")
    expires_in: int = Field(
        ...,
        description="Seconds until expiry (approximate).",
    )


class RevokeUserRequest(BaseModel):
    """Body for the per-user revocation endpoint."""

    user: str = Field(..., description="User identity (email) whose tokens to revoke.")


# ---------------------------------------------------------------------------
# GET /auth/token — exchange SSO session for a mobile token
# ---------------------------------------------------------------------------


@router.get("/auth/token", response_model=TokenResponse)
async def exchange_token(
    request: Request,
    token_store: TokenStore = Depends(_get_token_store),  # noqa: B008
) -> TokenResponse:
    """Exchange the current SSO session for a long-lived mobile bearer token.

    This endpoint is **only** reachable through the tinyauth SSO gate.
    The ``Remote-User`` header (set by tinyauth after successful SSO
    authentication) identifies the authenticated user.  The endpoint
    issues a self-contained Fernet-signed bearer token that the mobile
    app stores and sends as ``Authorization: Bearer <token>`` on
    subsequent API calls.

    The token is scoped to ``chat`` access only and expires after the
    configured TTL (default 90 days).
    """
    remote_user = request.headers.get("Remote-User", "").strip()
    if not remote_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "No Remote-User header. This endpoint must be reached "
                "through the fleet SSO gate (tinyauth)."
            ),
        )

    token = token_store.issue(sub=remote_user)

    # Decode to get the actual expiry for the response.
    payload = token_store.validate(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Token issuance failed — could not validate newly issued token.",
        )

    import time

    expires_in = max(0, int(payload["exp"] - time.time()))

    logger.info(
        "Issued mobile token for user %r (expires in %d seconds)",
        remote_user,
        expires_in,
    )
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
    )


# ---------------------------------------------------------------------------
# GET /auth/login + /auth/login/complete — mobile deep-link login handoff
# ---------------------------------------------------------------------------


@router.get("/auth/login", include_in_schema=True)
async def start_mobile_login(
    redirect_to: str = Query(
        ...,
        description="The app's deep-link callback, e.g. robotsixchat://auth/callback",
    ),
) -> RedirectResponse:
    """Start the mobile login handoff (reachable WITHOUT an SSO session).

    Stashes the validated deep-link target in a short-lived cookie and
    redirects to the SSO-gated ``/auth/login/complete``.  A browser without
    a fleet SSO session is bounced through the tinyauth login page on that
    request and returns to the same bare path afterwards — the cookie, not
    the query string, carries the target across the round-trip (tinyauth
    drops query strings when rebuilding its post-login redirect).
    """
    target = _validate_deep_link(redirect_to)
    response = RedirectResponse(
        url="/auth/login/complete", status_code=status.HTTP_303_SEE_OTHER
    )
    response.set_cookie(
        _LOGIN_REDIRECT_COOKIE,
        target,
        max_age=_LOGIN_REDIRECT_MAX_AGE,
        path="/auth",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/auth/login/complete")
async def complete_mobile_login(
    request: Request,
    token_store: TokenStore = Depends(_get_token_store),  # noqa: B008
) -> RedirectResponse:
    """Finish the mobile login handoff (only reachable through the SSO gate).

    Issues a mobile bearer token for the SSO-authenticated ``Remote-User``
    and redirects the browser to the deep-link target stored by
    ``/auth/login``, appending ``?token=<bearer>``.  The OS hands the
    deep link to the app, which persists the token.
    """
    remote_user = request.headers.get("Remote-User", "").strip()
    if not remote_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "No Remote-User header. This endpoint must be reached "
                "through the fleet SSO gate (tinyauth)."
            ),
        )

    target = request.cookies.get(_LOGIN_REDIRECT_COOKIE, "")
    if not target:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Login handoff cookie missing or expired — restart the "
                "login from the app (open /auth/login?redirect_to=...)."
            ),
        )
    # Re-validate: the cookie is client-controlled between the two requests.
    target = _validate_deep_link(target)

    token = token_store.issue(sub=remote_user)
    logger.info("Issued mobile token via login handoff for user %r", remote_user)

    separator = "&" if "?" in target else "?"
    response = RedirectResponse(
        url=f"{target}{separator}token={quote(token, safe='')}",
        status_code=status.HTTP_302_FOUND,
    )
    response.delete_cookie(_LOGIN_REDIRECT_COOKIE, path="/auth")
    return response


# ---------------------------------------------------------------------------
# POST /chat/auth/mobile-token — validate a stored token (app exchange step)
# ---------------------------------------------------------------------------


class MobileTokenExchangeRequest(BaseModel):
    """Body of the mobile app's token-exchange call."""

    token: str = Field(..., description="Bearer token obtained via the login handoff.")


@router.post("/chat/auth/mobile-token", response_model=TokenResponse)
async def exchange_mobile_token(
    body: MobileTokenExchangeRequest,
    token_store: TokenStore = Depends(_get_token_store),  # noqa: B008
) -> TokenResponse:
    """Validate the app's stored token and return it in TokenResponse shape.

    The mobile app treats the deep-link credential as a subject token and
    exchanges it for an access token before each session.  Fleet mobile
    tokens are already self-contained bearers, so the exchange validates
    the token (expiry + revocation) and echoes it with its remaining
    lifetime.  Reachable WITHOUT an SSO session: it only ever returns a
    token that the caller already holds and that is still valid.
    """
    payload = token_store.validate(body.token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is invalid, expired, or revoked.",
        )

    import time

    return TokenResponse(
        access_token=body.token,
        scope=str(payload.get("scope", "chat")),
        expires_in=max(0, int(payload["exp"] - time.time())),
    )


# ---------------------------------------------------------------------------
# GET /auth/validate — Traefik ForwardAuth validation
# ---------------------------------------------------------------------------


@router.api_route(
    "/auth/validate",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def validate_token(
    request: Request,
) -> Response:
    """Validate a mobile bearer token for the Traefik ForwardAuth middleware.

    Called by Traefik's ``mobile-token`` ForwardAuth on every request that
    carries an ``Authorization: Bearer`` header.  Traefik ForwardAuth
    preserves the original request method, so this endpoint accepts *any*
    HTTP method — a ``POST /chat`` from the mobile app arrives here as
    ``POST /auth/validate``.

    Returns 200 with a ``Remote-User`` response header (copied upstream by
    Traefik) when the token is valid.  Returns 401 when the token is
    missing, expired, or revoked.  No JSON body is returned — the
    ForwardAuth hot path returns only the status and header.

    This endpoint is **not** protected by ``verify_auth`` — it is an
    internal endpoint reachable only by Traefik on the Docker network.
    """
    from ..auth import verify_bearer_token

    payload = verify_bearer_token(request)
    return Response(
        status_code=status.HTTP_200_OK,
        headers={"Remote-User": payload["sub"]},
    )


# ---------------------------------------------------------------------------
# DELETE /auth/token/{token_id} — revoke a single token
# ---------------------------------------------------------------------------


@router.delete("/auth/token/{token_id}", status_code=204)
async def revoke_token(
    token_id: str,
    token_store: TokenStore = Depends(_get_token_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> None:
    """Revoke a single mobile token by its ``jti`` (token id).

    The token id is embedded in the token payload and can be extracted
    by decoding the bearer token.  Revoked tokens are rejected by the
    ForwardAuth gate on the next request.
    """
    revoked = await token_store.revoke_one(token_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Token '{token_id}' not found or already revoked.",
        )
    logger.info(
        "Revoked mobile token %r",
        token_id.replace("\n", "\\n").replace("\r", "\\r"),
    )


# ---------------------------------------------------------------------------
# POST /auth/revoke-user — revoke all tokens for a user
# ---------------------------------------------------------------------------


@router.post("/auth/revoke-user", status_code=204)
async def revoke_user_tokens(
    body: RevokeUserRequest,
    token_store: TokenStore = Depends(_get_token_store),  # noqa: B008
    _auth: None = Depends(verify_auth),
) -> None:
    """Revoke every mobile token issued for *user* before this moment.

    Tokens issued *after* this call are unaffected — only tokens with an
    ``iat`` (issued-at) timestamp earlier than now are rejected.
    """
    await token_store.revoke_user(body.user)
    logger.info(
        "Revoked all mobile tokens for user %r",
        body.user.replace("\n", "\\n").replace("\r", "\\r"),
    )
