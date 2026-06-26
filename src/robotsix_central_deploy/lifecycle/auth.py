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

    # Try HTTP Basic Auth
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, _, password = decoded.partition(":")
        except Exception:
            pass
        else:
            if (
                config.auth_username
                and config.auth_password
                and _safe_compare(username, config.auth_username)
                and _safe_compare(password, config.auth_password)
            ):
                return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="Central Deploy"'},
    )


def _safe_compare(a: str, b: str) -> bool:
    """Compare two strings in (mostly) constant time."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0
