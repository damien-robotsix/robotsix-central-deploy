"""Mobile token-exchange endpoints for the fleet edge.

``GET  /auth/token``            — exchange an SSO session for a mobile bearer token.
``GET  /auth/validate``         — Traefik ForwardAuth: validate a bearer token.
``DELETE /auth/token/{token_id}`` — revoke a single token.
``POST  /auth/revoke-user``     — revoke all tokens for a user.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..auth import verify_auth
from ..deps import _get_token_store
from ..token_store import TokenStore

router = APIRouter(tags=["auth-token"])

logger = logging.getLogger(__name__)

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
