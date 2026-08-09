"""In-memory rate limiter with per-IP sliding-window tracking.

Provides a ``RateLimitStore`` for tracking request counts and a
``RateLimitMiddleware`` (ASGI) that applies a configurable rate limit to
authenticated API paths.

There is no login tier: central-deploy has no login of its own — the fleet's
Traefik edge authenticates every request through tinyauth before it reaches
this app, and brute-force protection belongs there.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# ---------------------------------------------------------------------------
# Paths that receive the "API" (broad) rate limit
# ---------------------------------------------------------------------------

_API_PATH_PREFIXES: tuple[str, ...] = (
    "/services",
    "/system",
    "/volumes",
    "/onboard",
    "/disk",
    "/chat",
    "/caretaker",
)


def _is_api_path(path: str) -> bool:
    """Return True when *path* should receive the API rate limit."""
    return path.startswith(_API_PATH_PREFIXES)


def _client_ip(request: Request) -> str:
    """Best-effort client IP — respects reverse-proxy headers."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# RateLimitStore
# ---------------------------------------------------------------------------


class RateLimitStore:
    """Thread-safe in-memory rate-limiter state.

    Tracks per-IP request timestamps in a sliding window.
    """

    def __init__(self) -> None:
        # ip → sorted list of UTC timestamps (oldest first)
        self._api_requests: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    # -- Sliding-window helpers ------------------------------------------

    @staticmethod
    def _prune(timestamps: list[float], window: float, now: float) -> int:
        """Remove entries older than *window*; return count remaining."""
        cutoff = now - window
        while timestamps and timestamps[0] <= cutoff:
            timestamps.pop(0)
        return len(timestamps)

    # -- Public API ------------------------------------------------------

    async def check_api_rate(self, ip: str, limit: int, window: float) -> bool:
        """Return True when the request is within the API rate limit."""
        now = time.time()
        async with self._lock:
            timestamps = self._api_requests[ip]
            count = self._prune(timestamps, window, now)
            if count >= limit:
                return False
            timestamps.append(now)
            return True


# ---------------------------------------------------------------------------
# RateLimitMiddleware
# ---------------------------------------------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that enforces per-IP rate limits.

    - Authenticated API paths (``/services``, …) get a per-hour limit.
    - All other paths pass through untouched.
    """

    async def dispatch(self, request: Request, call_next: object) -> Response:
        """Enforce per-IP rate limits on the incoming request.

        API paths (``/services``, …) receive a per-hour limit.

        Non-matching paths pass through untouched. Only central-deploy's own
        traffic reaches this middleware — component traffic is served by the
        Traefik edge and never enters this app.

        Returns the upstream ``Response``, or a ``429 JSONResponse`` when
        limits are exceeded.
        """
        path = request.url.path
        ip = _client_ip(request)

        store: RateLimitStore | None = getattr(
            request.app.state, "rate_limit_store", None
        )
        if store is None:
            return await call_next(request)  # type: ignore[no-any-return, operator]

        cfg = request.app.state.config

        # -- API paths: per-hour limit -----------------------------------
        if _is_api_path(path) and not await store.check_api_rate(
            ip,
            cfg.rate_limit_api_per_hour,
            3600.0,
        ):
            return JSONResponse(
                {"detail": "API rate limit exceeded — slow down."},
                status_code=429,
            )

        return await call_next(request)  # type: ignore[no-any-return, operator]
