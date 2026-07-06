"""Unit tests for ``rate_limiter`` — ``RateLimitStore``, helpers, and middleware."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from robotsix_central_deploy.lifecycle.rate_limiter import (
    RateLimitMiddleware,
    RateLimitStore,
    _client_ip,
    _is_api_path,
    _is_gateway_host,
)


# ---------------------------------------------------------------------------
# _is_api_path
# ---------------------------------------------------------------------------


class TestIsApiPath:
    def test_services_path(self):
        assert _is_api_path("/services/svc-a/logs")

    def test_settings_path(self):
        assert _is_api_path("/settings")

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

    def test_logout_path(self):
        assert _is_api_path("/logout")

    def test_non_api_path_returns_false(self):
        assert not _is_api_path("/health")
        assert not _is_api_path("/login")
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
# _is_gateway_host
# ---------------------------------------------------------------------------


def _make_gateway_request(
    host: str, gateway_base_domain: str = "deploy.robotsix.net"
) -> Request:
    app = MagicMock()
    app.state.config = MagicMock()
    app.state.config.gateway_base_domain = gateway_base_domain
    scope: dict = {
        "type": "http",
        "app": app,
        "client": ("1.2.3.4", 12345),
        "headers": [(b"host", host.encode())],
    }
    return Request(scope)


class TestIsGatewayHost:
    def test_component_subdomain_is_gateway(self):
        req = _make_gateway_request("mill.deploy.robotsix.net")
        assert _is_gateway_host(req) is True

    def test_naked_domain_is_not_gateway(self):
        req = _make_gateway_request("deploy.robotsix.net")
        assert _is_gateway_host(req) is False

    def test_no_base_domain_configured(self):
        req = _make_gateway_request("mill.deploy.robotsix.net", gateway_base_domain="")
        assert _is_gateway_host(req) is False

    def test_missing_config(self):
        app = MagicMock()
        app.state = MagicMock()
        app.state.config = None
        scope: dict = {
            "type": "http",
            "app": app,
            "client": ("1.2.3.4", 12345),
            "headers": [(b"host", b"mill.deploy.robotsix.net")],
        }
        req = Request(scope)
        assert _is_gateway_host(req) is False

    def test_host_with_port(self):
        req = _make_gateway_request("mill.deploy.robotsix.net:8080")
        assert _is_gateway_host(req) is True

    def test_case_insensitive(self):
        req = _make_gateway_request("MILL.Deploy.Robotsix.NET")
        assert _is_gateway_host(req) is True

    def test_non_matching_subdomain(self):
        req = _make_gateway_request("other.example.com")
        assert _is_gateway_host(req) is False


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


class TestLoginRateLimit:
    @pytest.mark.asyncio
    async def test_allows_within_limit(self):
        store = RateLimitStore()
        for _ in range(5):
            assert await store.check_login_rate("1.2.3.4", limit=10, window=60.0)

    @pytest.mark.asyncio
    async def test_blocks_when_limit_exceeded(self):
        store = RateLimitStore()
        # Exhaust budget
        for i in range(10):
            result = await store.check_login_rate("1.2.3.4", limit=10, window=60.0)
            assert result is (i < 10)
        # 11th request blocked
        assert not await store.check_login_rate("1.2.3.4", limit=10, window=60.0)

    @pytest.mark.asyncio
    async def test_different_ips_independent(self):
        store = RateLimitStore()
        for _ in range(10):
            assert await store.check_login_rate("1.2.3.4", limit=10, window=60.0)
        # IP A exhausted
        assert not await store.check_login_rate("1.2.3.4", limit=10, window=60.0)
        # IP B still allowed
        assert await store.check_login_rate("5.6.7.8", limit=10, window=60.0)

    @pytest.mark.asyncio
    async def test_limit_zero_blocks_immediately(self):
        store = RateLimitStore()
        assert not await store.check_login_rate("1.2.3.4", limit=0, window=60.0)

    @pytest.mark.asyncio
    async def test_expired_entries_dont_count(self):
        """Old timestamps beyond the window shouldn't block new requests."""
        store = RateLimitStore()
        now = time.time()
        # Inject old timestamps directly into the internal dict
        store._login_requests["1.2.3.4"] = [now - 120.0] * 50  # all outside 60s window
        assert await store.check_login_rate("1.2.3.4", limit=10, window=60.0)


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

    @pytest.mark.asyncio
    async def test_login_and_api_budgets_independent(self):
        store = RateLimitStore()
        for _ in range(5):
            assert await store.check_login_rate("1.2.3.4", limit=5, window=60.0)
        assert not await store.check_login_rate("1.2.3.4", limit=5, window=60.0)
        # API budget untouched
        assert await store.check_api_rate("1.2.3.4", limit=100, window=3600.0)


class TestLockout:
    @pytest.mark.asyncio
    async def test_not_locked_out_initially(self):
        store = RateLimitStore()
        assert not await store.is_locked_out("1.2.3.4", max_attempts=5)

    @pytest.mark.asyncio
    async def test_not_locked_out_below_max_attempts(self):
        store = RateLimitStore()
        for _ in range(4):
            await store.record_login_failure(
                "1.2.3.4", max_attempts=5, lockout_seconds=300
            )
        assert not await store.is_locked_out("1.2.3.4", max_attempts=5)

    @pytest.mark.asyncio
    async def test_locked_out_when_max_attempts_reached(self):
        store = RateLimitStore()
        for _ in range(5):
            await store.record_login_failure(
                "1.2.3.4", max_attempts=5, lockout_seconds=300
            )
        assert await store.is_locked_out("1.2.3.4", max_attempts=5)

    @pytest.mark.asyncio
    async def test_lockout_expires(self):
        store = RateLimitStore()
        for _ in range(3):
            await store.record_login_failure(
                "1.2.3.4", max_attempts=3, lockout_seconds=0
            )
        # lockout_seconds=0 means lockout_until = now, so it should have expired
        assert not await store.is_locked_out("1.2.3.4", max_attempts=3)

    @pytest.mark.asyncio
    async def test_record_login_success_clears_failures(self):
        store = RateLimitStore()
        await store.record_login_failure("1.2.3.4", max_attempts=5, lockout_seconds=300)
        await store.record_login_failure("1.2.3.4", max_attempts=5, lockout_seconds=300)
        await store.record_login_success("1.2.3.4")
        # After success, should not be locked out
        assert not await store.is_locked_out("1.2.3.4", max_attempts=5)
        # And counter should be reset (0 failures)
        # Verify by checking that 4 more failures don't trigger lockout (since counter was reset)
        for _ in range(4):
            await store.record_login_failure(
                "1.2.3.4", max_attempts=5, lockout_seconds=300
            )
        assert not await store.is_locked_out("1.2.3.4", max_attempts=5)

    @pytest.mark.asyncio
    async def test_lockout_per_ip_isolation(self):
        store = RateLimitStore()
        for _ in range(5):
            await store.record_login_failure(
                "1.2.3.4", max_attempts=5, lockout_seconds=300
            )
        assert await store.is_locked_out("1.2.3.4", max_attempts=5)
        assert not await store.is_locked_out("5.6.7.8", max_attempts=5)


class TestConcurrentAccess:
    @pytest.mark.asyncio
    async def test_concurrent_login_rate_limits_stay_consistent(self):
        """Many concurrent login-rate checks should not exceed the limit."""
        store = RateLimitStore()

        async def check() -> bool:
            return await store.check_login_rate("1.2.3.4", limit=100, window=60.0)

        tasks = [asyncio.create_task(check()) for _ in range(200)]
        results = await asyncio.gather(*tasks)

        allowed = sum(results)
        # At most 100 should be allowed (the lock serialises the checks)
        assert allowed <= 100

    @pytest.mark.asyncio
    async def test_concurrent_failures_trigger_lockout_correctly(self):
        store = RateLimitStore()

        async def fail():
            await store.record_login_failure(
                "1.2.3.4", max_attempts=10, lockout_seconds=300
            )

        tasks = [asyncio.create_task(fail()) for _ in range(10)]
        await asyncio.gather(*tasks)

        assert await store.is_locked_out("1.2.3.4", max_attempts=10)


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
    login_per_minute: int = 10,
    login_max_attempts: int = 20,
    login_lockout_seconds: int = 300,
) -> Request:
    app = MagicMock()
    app.state.config = MagicMock()
    app.state.config.gateway_base_domain = gateway_base_domain
    app.state.config.rate_limit_login_per_minute = login_per_minute
    app.state.config.rate_limit_api_per_hour = api_per_hour
    app.state.config.rate_limit_login_max_attempts = login_max_attempts
    app.state.config.rate_limit_login_lockout_seconds = login_lockout_seconds
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
    async def test_gateway_host_passes_through(self, middleware: RateLimitMiddleware):
        req = _make_middleware_request(
            path="/login",
            method="POST",
            host="mill.deploy.robotsix.net",
        )
        resp = await self._call(middleware, req)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_store_passes_through(self, middleware: RateLimitMiddleware):
        req = _make_middleware_request(
            path="/login",
            method="POST",
            rate_limit_store=None,
        )
        resp = await self._call(middleware, req)
        assert resp.status_code == 200

    # -- Login rate limit ------------------------------------------------

    @pytest.mark.asyncio
    async def test_login_allowed_within_limit(self, middleware: RateLimitMiddleware):
        store = RateLimitStore()
        for _ in range(5):
            req = _make_middleware_request(
                path="/login", method="POST", rate_limit_store=store
            )
            resp = await self._call(middleware, req)
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_login_blocked_when_limit_exceeded(
        self, middleware: RateLimitMiddleware
    ):
        store = RateLimitStore()
        for _ in range(10):
            req = _make_middleware_request(
                path="/login", method="POST", rate_limit_store=store
            )
            await self._call(middleware, req)
        # 11th blocked
        req = _make_middleware_request(
            path="/login", method="POST", rate_limit_store=store
        )
        resp = await self._call(middleware, req)
        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_login_locked_out_returns_429(self, middleware: RateLimitMiddleware):
        store = RateLimitStore()
        for _ in range(20):
            await store.record_login_failure(
                "1.2.3.4", max_attempts=20, lockout_seconds=300
            )

        req = _make_middleware_request(
            path="/login", method="POST", rate_limit_store=store
        )
        resp = await self._call(middleware, req)
        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_login_failure_increments_failure_counter(
        self, middleware: RateLimitMiddleware
    ):
        store = RateLimitStore()
        req = _make_middleware_request(
            path="/login", method="POST", rate_limit_store=store
        )

        async def call_next_401(req: Request) -> Response:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        await middleware.dispatch(req, call_next_401)
        # After one 401, failure count should be 1, not locked out yet
        assert not await store.is_locked_out("1.2.3.4", max_attempts=20)

    @pytest.mark.asyncio
    async def test_login_success_clears_failure_counter(
        self, middleware: RateLimitMiddleware
    ):
        store = RateLimitStore()
        # Pre-populate failures
        await store.record_login_failure(
            "1.2.3.4", max_attempts=20, lockout_seconds=300
        )
        await store.record_login_failure(
            "1.2.3.4", max_attempts=20, lockout_seconds=300
        )

        req = _make_middleware_request(
            path="/login", method="POST", rate_limit_store=store
        )

        async def call_next_303(req: Request) -> Response:
            return Response(b"", status_code=303)

        await middleware.dispatch(req, call_next_303)
        assert not await store.is_locked_out("1.2.3.4", max_attempts=20)

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
