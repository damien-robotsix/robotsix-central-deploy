"""Auth guard — validates X-API-Key or HTTP Basic Auth on protected endpoints."""

from __future__ import annotations

import base64

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

    # Try X-API-Key
    api_key = request.headers.get("X-API-Key")
    if api_key and config.api_key and _safe_compare(api_key, config.api_key):
        return

    # Try HTTP Basic Auth — password must equal config.api_key; username is ignored.
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        password = _decode_basic_auth(auth_header)
        if password and _safe_compare(password, config.api_key):
            return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="Robotsix Central Deploy"'},
    )


def _decode_basic_auth(header: str) -> str:
    """Decode an HTTP Basic Auth header and return the password portion.

    Returns ``""`` on malformed input — never raises.
    """
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        _, _, password = decoded.partition(":")
        return password
    except Exception:
        return ""


def _safe_compare(a: str, b: str) -> bool:
    """Compare two strings in (mostly) constant time."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


