"""Auth guard — validates the ``X-API-Key`` header on mutating endpoints."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status


async def verify_api_key(request: Request) -> None:
    """FastAPI dependency that rejects requests missing a valid API key.

    The key is read from the app-scoped ``LifecycleConfig`` stash set up
    during server startup.
    """
    config = request.app.state.config
    if not config.auth_required:
        return  # No key configured — allow all (dev mode).

    api_key: str | None = request.headers.get("X-API-Key")
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing X-API-Key header",
        )
    # Constant-time-ish comparison for timing safety.
    if not _safe_compare(api_key, config.api_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )


def _safe_compare(a: str, b: str) -> bool:
    """Compare two strings in (mostly) constant time."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0
