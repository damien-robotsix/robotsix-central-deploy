"""Auth guard — validates X-API-Key, HTTP Basic Auth, or mobile bearer tokens."""

from __future__ import annotations

import base64
import hmac
from typing import Any

from fastapi import HTTPException, Request, status


async def verify_auth(request: Request) -> None:
    """FastAPI dependency that rejects requests missing valid credentials.

    Accepts either an ``X-API-Key`` header or ``Authorization: Basic``.
    The credentials are read from the app-scoped ``LifecycleConfig`` stash
    set up during server startup.
    """
    config = request.app.state.config
    if not config.auth_required:
        return  # No credentials configured — allow all (dev mode).

    # Try X-API-Key (only when api_key is configured).
    api_key = request.headers.get("X-API-Key")
    if (
        api_key
        and config.api_key.get_secret_value()
        and _safe_compare(api_key, config.api_key.get_secret_value())
    ):
        return

    # Try HTTP Basic Auth.
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        username, password = _decode_basic_auth(auth_header)
        if config.auth_username and config.auth_password.get_secret_value():
            # Username+password mode — both fields must match.
            if (
                username
                and password
                and _safe_compare(username, config.auth_username)
                and _safe_compare(password, config.auth_password.get_secret_value())
            ):
                return
        elif (
            config.api_key.get_secret_value()
            and password
            and _safe_compare(password, config.api_key.get_secret_value())
        ):
            # Legacy api_key mode — username is ignored, password == api_key.
            return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="Robotsix Central Deploy"'},
    )


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


def _decode_basic_auth(header: str) -> tuple[str, str]:
    """Decode an HTTP Basic Auth header; returns (username, password).

    Returns ("", "") on malformed input — never raises.
    """
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
        return username, password
    except Exception:  # noqa: BLE001
        return "", ""


def _safe_compare(a: str, b: str) -> bool:
    """Compare two strings in constant time (no length side-channel)."""
    return hmac.compare_digest(a, b)
