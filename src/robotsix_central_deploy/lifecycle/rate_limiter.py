"""In-memory rate limiter with per-IP sliding-window tracking.

Provides a ``RateLimitStore`` for tracking request counts and login
failures, and a ``RateLimitMiddleware`` (ASGI) that applies configurable
rate limits to the login endpoint and authenticated API paths.
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
    "/settings",
    "/system",
    "/volumes",
    "/onboard",
    "/disk",
    "/chat",
    "/caretaker",
    "/logout",
)


def _is_api_path(path: str) -> bool:
    """Return True when *path* should receive the API rate limit."""
    return path.startswith(_API_PATH_PREFIXES)


def _is_gateway_host(request: Request) -> bool:
    """Return True when the request targets a component subdomain.

    Gateway-proxied traffic (``<name>.<gateway_base_domain>``) belongs to
    the target component, not to central-deploy's own API — a component's
    ``POST /chat`` or ``/services`` must not consume (or be blocked by)
    central-deploy's rate budget. Mirrors the subdomain matching in
    ``gateway.router._extract_subdomain_name``.
    """
    base_domain: str = getattr(
        getattr(getattr(request.app, "state", None), "config", None),
        "gateway_base_domain",
        "",
    )
    if not base_domain:
        return False
    host = request.headers.get("host", "").split(":")[0].lower()
    return host.endswith("." + base_domain.lower())


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

    Tracks per-IP request timestamps (sliding window) and login-failure
    counts for lockout.
    """

    def __init__(self) -> None:
        # ip → sorted list of UTC timestamps (oldest first)
        self._login_requests: dict[str, list[float]] = defaultdict(list)
        self._api_requests: dict[str, list[float]] = defaultdict(list)
        # ip → (failure_count, lockout_until_ts)
        self._login_failures: dict[str, tuple[int, float]] = {}
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

    async def check_login_rate(self, ip: str, limit: int, window: float) -> bool:
        """Return True when the request is within the login rate limit."""
        now = time.time()
        async with self._lock:
            timestamps = self._login_requests[ip]
            count = self._prune(timestamps, window, now)
            if count >= limit:
                return False
            timestamps.append(now)
            return True

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

    async def is_locked_out(self, ip: str, max_attempts: int) -> bool:
        """Return True when *ip* is in a login lockout period."""
        async with self._lock:
            entry = self._login_failures.get(ip)
            if entry is None:
                return False
            failures, lockout_until = entry
            if failures < max_attempts:
                return False
            if time.time() >= lockout_until:
                del self._login_failures[ip]
                return False
            return True

    async def record_login_failure(
        self, ip: str, max_attempts: int, lockout_seconds: int
    ) -> None:
        """Increment the failure counter for *ip* and lock out when
        *max_attempts* is reached."""
        now = time.time()
        async with self._lock:
            entry = self._login_failures.get(ip)
            if entry is None:
                self._login_failures[ip] = (1, 0.0)
                return
            failures, _ = entry
            failures += 1
            if failures >= max_attempts:
                lockout_until = now + lockout_seconds
            else:
                lockout_until = 0.0
            self._login_failures[ip] = (failures, lockout_until)

    async def record_login_success(self, ip: str) -> None:
        """Clear the failure counter after a successful login."""
        async with self._lock:
            self._login_failures.pop(ip, None)


# ---------------------------------------------------------------------------
# RateLimitMiddleware
# ---------------------------------------------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that enforces per-IP rate limits.

    - ``POST /login`` gets a strict per-minute limit *plus* lockout after
      too many failed attempts.
    - Authenticated API paths (``/services``, ``/settings``, …) get a
      broader per-hour limit.
    - All other paths pass through untouched.
    """

    async def dispatch(self, request: Request, call_next: object) -> Response:
        """Enforce per-IP rate limits on the incoming request.

        The middleware applies two tiers of rate limiting:

        - **Login POST** (``/login``): strict per-minute limit + lockout after
          ``rate_limit_login_max_attempts`` consecutive failures.
        - **API paths** (``/services``, ``/settings``, …): broader per-hour limit.

        Requests proxied through the gateway for a component subdomain are
        excluded from all limits (``_is_gateway_host`` check). Non-matching
        paths pass through untouched.

        Returns the upstream ``Response``, or a ``429 JSONResponse`` when
        limits are exceeded.
        """
        # Gateway-proxied component traffic is out of scope: the limiter
        # protects central-deploy's own login/API, and a component's
        # ``/login`` or ``/chat`` path colliding with these prefixes must
        # not be throttled (or feed the lockout counter).
        if _is_gateway_host(request):
            return await call_next(request)  # type: ignore[no-any-return, operator]

        path = request.url.path
        method = request.method
        ip = _client_ip(request)

        store: RateLimitStore | None = getattr(
            request.app.state, "rate_limit_store", None
        )
        if store is None:
            return await call_next(request)  # type: ignore[no-any-return, operator]

        cfg = request.app.state.config

        # -- Login POST: strict limit + lockout --------------------------
        if path == "/login" and method == "POST":
            # Lockout check first — more severe than rate-limit
            if await store.is_locked_out(ip, cfg.rate_limit_login_max_attempts):
                return JSONResponse(
                    {"detail": "Too many login attempts — try again later."},
                    status_code=429,
                )
            if not await store.check_login_rate(
                ip,
                cfg.rate_limit_login_per_minute,
                60.0,
            ):
                return JSONResponse(
                    {"detail": "Login rate limit exceeded — slow down."},
                    status_code=429,
                )
            response: Response = await call_next(request)  # type: ignore[operator]
            # Record failures post-response so lockout works
            if response.status_code == 401:
                await store.record_login_failure(
                    ip,
                    cfg.rate_limit_login_max_attempts,
                    cfg.rate_limit_login_lockout_seconds,
                )
            elif response.status_code == 303:
                # Successful login redirect
                await store.record_login_success(ip)
            return response

        # -- API paths: broader limit ------------------------------------
        if _is_api_path(path):
            if not await store.check_api_rate(
                ip,
                cfg.rate_limit_api_per_hour,
                3600.0,
            ):
                return JSONResponse(
                    {"detail": "API rate limit exceeded — slow down."},
                    status_code=429,
                )

        return await call_next(request)  # type: ignore[no-any-return, operator]
