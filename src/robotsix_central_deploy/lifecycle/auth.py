"""Auth guard — validates mobile bearer tokens.

Component-level authentication was removed (see auth-removal epic):
the fleet edge (Traefik + tinyauth) is the only auth gate, so every
request arriving here is already authenticated.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status


async def verify_auth(_request: Request) -> None:
    """FastAPI dependency stub — always returns ``None``.

    Component-level authentication has been removed.  This stub remains
    only until every ``Depends(verify_auth)`` call site is deleted (see
    sibling ticket in the auth-removal epic).
    """


def verify_bearer_token(request: Request) -> dict[str, Any]:
    """Validate a mobile bearer token from the ``Authorization`` header.

    Intended as a FastAPI dependency on the ForwardAuth endpoint
    (``GET /auth/validate``) that Traefik calls for every request bearing
    an ``Authorization: Bearer <token>`` header.  Returns the decoded
    token payload so the endpoint can forward the ``Remote-User`` header.

    Raises ``HTTPException(401)`` when the token is missing, malformed,
    expired, or revoked.
    """
    token_store = request.app.state.token_store
    if token_store is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token store not available",
        )

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_header[7:]  # len("Bearer ") == 7
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = token_store.validate(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload  # type: ignore[no-any-return]  # token_store attached via app.state
