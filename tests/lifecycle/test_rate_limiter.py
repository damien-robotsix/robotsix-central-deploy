"""Unit tests for ``rate_limiter`` — ``RateLimitStore``, helpers, and middleware."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request
from starlette.responses import Response

from robotsix_central_deploy.lifecycle.rate_limiter import (
    RateLimitMiddleware,
    RateLimitStore,
    _client_ip,
    _is_api_path,
)

# ---------------------------------------------------------------------------
# _is_api_path
# ---------------------------------------------------------------------------


class TestIsApiPath:
    def test_services_path(self):
        assert _is_api_path("/services/svc-a/logs")

    def test_system_path(self):
        assert _is_api_path("/system/update")

    def test_onboard_path(self):
        assert _is_api_path("/onboard/preflight")

    def test_volumes_path(self):
        assert _is_api_path("/volumes")

    def test_disk_path(self):
        assert _is_api_path("/disk")

    def test_chat_path(self):
        assert _is_api_path("/chat/agents")

    def test_non_api_path_returns_false(self):
        assert not _is_api_path("/health")
        assert not _is_api_path("/ui")
        assert not _is_api_path("/help/deploy-contract")

    def test_root_path(self):
        assert not _is_api_path("/")


# ---------------------------------------------------------------------------
# _client_ip
# ---------------------------------------------------------------------------


def _make_mock_request(
    client_host: str = "1.2.3.4",
    x_forwarded_for: str | None = None,
    x_real_ip: str | None = None,
) -> Request:
    scope: dict = {
        "type": "http",
        "client": (client_host, 12345),
        "headers": [],
    }
    if x_forwarded_for:
        scope["headers"].append((b"x-forwarded-for", x_forwarded_for.encode()))
    if x_real_ip:
        scope["headers"].append((b"x-real-ip", x_real_ip.encode()))
    return Request(scope)


class TestClientIp:
    def test_returns_client_host(self):
        req = _make_mock_request(client_host="10.0.0.1")
        assert _client_ip(req) == "10.0.0.1"

    def test_x_forwarded_for_takes_precedence(self):
        req = _make_mock_request(
            client_host="10.0.0.1",
            x_forwarded_for="5.6.7.8, 9.10.11.12",
        )
        assert _client_ip(req) == "5.6.7.8"

    def test_x_real_ip(self):
        req = _make_mock_request(
            client_host="10.0.0.1",
            x_real_ip="3.3.3.3",
        )
        assert _client_ip(req) == "3.3.3.3"

    def test_x_forwarded_for_beats_x_real_ip(self):
        req = _make_mock_request(
            client_host="10.0.0.1",
            x_forwarded_for="5.5.5.5",
            x_real_ip="3.3.3.3",
        )
        assert _client_ip(req) == "5.5.5.5"

    def test_no_client_returns_unknown(self):
        scope: dict = {"type": "http", "client": None, "headers": []}
        req = Request(scope)
        assert _client_ip(req) == "unknown"


# ---------------------------------------------------------------------------
# RateLimitStore
# ---------------------------------------------------------------------------


class TestPrune:
    def test_prune_removes_old_entries(self):
        now = 1000.0
        timestamps = [500.0, 600.0, 700.0, 850.0, 950.0]
        count = RateLimitStore._prune(timestamps, window=200.0, now=now)
        # Cutoff is 800.0 — entries <= 800 are removed
        assert count == 2
        assert timestamps == [850.0, 950.0]

    def test_prune_all_entries(self):
        now = 1000.0
        timestamps = [100.0, 200.0]
        count = RateLimitStore._prune(timestamps, window=100.0, now=now)
        assert count == 0
        assert timestamps == []

    def test_prune_nothing_when_all_recent(self):
        now = 1000.0
        timestamps = [900.0, 950.0, 980.0]
        count = RateLimitStore._prune(timestamps, window=200.0, now=now)
        assert count == 3
        assert timestamps == [900.0, 950.0, 980.0]

    def test_prune_empty_list(self):
        count = RateLimitStore._prune([], window=60.0, now=1000.0)
        assert count == 0

    def test_prune_exactly_at_cutoff(self):
        now = 1000.0
        # cutoff = 800.0; 800.0 <= 800.0, so it should be removed
        timestamps = [800.0, 900.0]
        count = RateLimitStore._prune(timestamps, window=200.0, now=now)
        assert count == 1
        assert timestamps == [900.0]


class TestApiRateLimit:
    @pytest.mark.asyncio
    async def test_allows_within_limit(self):
        store = RateLimitStore()
        for _ in range(100):
            assert await store.check_api_rate("1.2.3.4", limit=200, window=3600.0)

    @pytest.mark.asyncio
    async def test_blocks_when_limit_exceeded(self):
        store = RateLimitStore()
        for _ in range(10):
            assert await store.check_api_rate("1.2.3.4", limit=10, window=3600.0)
        assert not await store.check_api_rate("1.2.3.4", limit=10, window=3600.0)


class TestConcurrentAccess:
    @pytest.mark.asyncio
    async def test_concurrent_api_rate_checks_stay_consistent(self):
        """Concurrent checks must not let more than *limit* through.

        The store is shared across every in-flight request, so without the
        lock two coroutines can both read a count below the limit and both
        append.
        """
        store = RateLimitStore()

        async def check() -> bool:
            return await store.check_api_rate("1.2.3.4", limit=100, window=3600.0)

        results = await asyncio.gather(
            *[asyncio.create_task(check()) for _ in range(200)]
        )
        assert sum(results) == 100


# ---------------------------------------------------------------------------
# RateLimitMiddleware
# ---------------------------------------------------------------------------


def _make_middleware_request(
    *,
    method: str = "GET",
    path: str = "/health",
    client_host: str = "1.2.3.4",
    x_forwarded_for: str | None = None,
    host: str = "deploy.robotsix.net",
    gateway_base_domain: str = "deploy.robotsix.net",
    rate_limit_store: RateLimitStore | None = None,
    api_per_hour: int = 20000,
) -> Request:
    app = MagicMock()
    app.state.config = MagicMock()
    app.state.config.gateway_base_domain = gateway_base_domain
    app.state.config.rate_limit_api_per_hour = api_per_hour
    app.state.rate_limit_store = rate_limit_store

    scope: dict = {
        "type": "http",
        "app": app,
        "method": method,
        "path": path,
        "client": (client_host, 12345),
        "headers": [
            (b"host", host.encode()),
        ],
    }
    if x_forwarded_for:
        scope["headers"].append((b"x-forwarded-for", x_forwarded_for.encode()))

    return Request(scope)


class TestRateLimitMiddlewareDispatch:
    @pytest.fixture
    def middleware(self) -> RateLimitMiddleware:
        app = MagicMock()
        return RateLimitMiddleware(app)

    async def _call(
        self, middleware: RateLimitMiddleware, request: Request
    ) -> Response:
        """Helper to invoke dispatch with a trivial call_next."""

        async def call_next(req: Request) -> Response:
            return Response(b"ok", status_code=200)

        return await middleware.dispatch(request, call_next)

    # -- Passthrough -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_health_passes_through(self, middleware: RateLimitMiddleware):
        req = _make_middleware_request(path="/health")
        resp = await self._call(middleware, req)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_ui_passes_through(self, middleware: RateLimitMiddleware):
        req = _make_middleware_request(path="/ui")
        resp = await self._call(middleware, req)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_store_passes_through(self, middleware: RateLimitMiddleware):
        req = _make_middleware_request(
            path="/services/svc-a/logs",
            rate_limit_store=None,
        )
        resp = await self._call(middleware, req)
        assert resp.status_code == 200

    # -- API rate limit --------------------------------------------------

    @pytest.mark.asyncio
    async def test_api_path_allowed_within_limit(self, middleware: RateLimitMiddleware):
        store = RateLimitStore()
        req = _make_middleware_request(
            path="/services/svc-a/logs", rate_limit_store=store
        )
        resp = await self._call(middleware, req)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_api_path_blocked_when_limit_exceeded(
        self, middleware: RateLimitMiddleware
    ):
        store = RateLimitStore()
        # Use a very low limit so a few requests trigger blocking
        low_limit = 3
        for _ in range(low_limit):
            req = _make_middleware_request(
                path="/services/svc-a/logs",
                rate_limit_store=store,
                api_per_hour=low_limit,
            )
            await self._call(middleware, req)

        # Next request should be blocked
        req = _make_middleware_request(
            path="/services/svc-a/logs",
            rate_limit_store=store,
            api_per_hour=low_limit,
        )
        resp = await self._call(middleware, req)
        assert resp.status_code == 429
