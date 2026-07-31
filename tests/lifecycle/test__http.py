"""Direct unit tests for ``robotsix_central_deploy._http``.

Covers ``retry_client_context`` (context-manager exit + config wiring)
and ``wrap_retry_client`` (delegation for default and custom configs),
which previously had no dedicated test coverage.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from robotsix_http import DEFAULT_CONFIG, RetryClient, RetryConfig

from robotsix_central_deploy._http import retry_client_context, wrap_retry_client


# ---------------------------------------------------------------------------
# retry_client_context
# ---------------------------------------------------------------------------


class TestRetryClientContext:
    async def test_yields_retry_client_with_default_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no *config* is passed, DEFAULT_CONFIG is forwarded to RetryClient."""
        fake_raw = MagicMock(spec=httpx.AsyncClient)
        fake_raw.__aenter__ = AsyncMock(return_value=fake_raw)
        fake_raw.__aexit__ = AsyncMock(return_value=None)

        captured_config: RetryConfig | None = None

        def fake_retry_client(
            raw: httpx.AsyncClient, config: RetryConfig
        ) -> RetryClient:
            nonlocal captured_config
            captured_config = config
            return MagicMock(spec=RetryClient)

        monkeypatch.setattr(
            "robotsix_central_deploy._http.RetryClient",
            fake_retry_client,
        )
        monkeypatch.setattr(
            "robotsix_central_deploy._http.httpx.AsyncClient",
            lambda *a, **kw: fake_raw,
        )

        async with retry_client_context() as _:
            assert captured_config is DEFAULT_CONFIG

        fake_raw.__aexit__.assert_awaited_once()

    async def test_yields_retry_client_with_custom_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When *config* is passed, it is forwarded instead of DEFAULT_CONFIG."""
        fake_raw = MagicMock(spec=httpx.AsyncClient)
        fake_raw.__aenter__ = AsyncMock(return_value=fake_raw)
        fake_raw.__aexit__ = AsyncMock(return_value=None)

        custom = RetryConfig(
            max_retries=1, backoff_base=1.0, backoff_cap=5.0, jitter_factor=0.1
        )

        captured_config: RetryConfig | None = None

        def fake_retry_client(
            raw: httpx.AsyncClient, config: RetryConfig
        ) -> RetryClient:
            nonlocal captured_config
            captured_config = config
            return MagicMock(spec=RetryClient)

        monkeypatch.setattr(
            "robotsix_central_deploy._http.RetryClient",
            fake_retry_client,
        )
        monkeypatch.setattr(
            "robotsix_central_deploy._http.httpx.AsyncClient",
            lambda *a, **kw: fake_raw,
        )

        async with retry_client_context(config=custom) as _:
            assert captured_config is custom

        fake_raw.__aexit__.assert_awaited_once()

    async def test_extra_kwargs_forwarded_to_async_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Extra **kwargs are forwarded to the httpx.AsyncClient constructor."""
        fake_raw = MagicMock(spec=httpx.AsyncClient)
        fake_raw.__aenter__ = AsyncMock(return_value=fake_raw)
        fake_raw.__aexit__ = AsyncMock(return_value=None)

        captured_kwargs: dict[str, object] = {}

        def fake_async_client(*args: object, **kwargs: object) -> MagicMock:
            captured_kwargs.update(kwargs)
            return fake_raw

        monkeypatch.setattr(
            "robotsix_central_deploy._http.httpx.AsyncClient",
            fake_async_client,
        )

        async with retry_client_context(timeout=5.0, follow_redirects=True):
            pass

        assert captured_kwargs.get("timeout") == 5.0
        assert captured_kwargs.get("follow_redirects") is True

    async def test_underlying_client_closed_on_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The underlying httpx.AsyncClient is closed when the context exits."""
        fake_raw = MagicMock(spec=httpx.AsyncClient)
        fake_raw.__aenter__ = AsyncMock(return_value=fake_raw)
        fake_raw.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(
            "robotsix_central_deploy._http.httpx.AsyncClient",
            lambda *a, **kw: fake_raw,
        )

        async with retry_client_context():
            pass

        fake_raw.__aexit__.assert_awaited_once()

    async def test_custom_timeout_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The *timeout* parameter defaults to 30.0."""
        fake_raw = MagicMock(spec=httpx.AsyncClient)
        fake_raw.__aenter__ = AsyncMock(return_value=fake_raw)
        fake_raw.__aexit__ = AsyncMock(return_value=None)

        captured_timeout: float | None = None

        def fake_async_client(*args: object, **kwargs: object) -> MagicMock:
            nonlocal captured_timeout
            captured_timeout = kwargs.get("timeout", None)  # type: ignore[assignment]
            return fake_raw

        monkeypatch.setattr(
            "robotsix_central_deploy._http.httpx.AsyncClient",
            fake_async_client,
        )

        async with retry_client_context():
            pass

        assert captured_timeout == 30.0


# ---------------------------------------------------------------------------
# wrap_retry_client
# ---------------------------------------------------------------------------


class TestWrapRetryClient:
    def test_uses_default_config_when_none_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no *config* is passed, DEFAULT_CONFIG is forwarded to RetryClient."""
        raw = MagicMock(spec=httpx.AsyncClient)
        captured_config: RetryConfig | None = None

        def fake_retry_client(
            raw: httpx.AsyncClient, config: RetryConfig
        ) -> RetryClient:
            nonlocal captured_config
            captured_config = config
            return MagicMock(spec=RetryClient)

        monkeypatch.setattr(
            "robotsix_central_deploy._http.RetryClient",
            fake_retry_client,
        )

        wrap_retry_client(raw)
        assert captured_config is DEFAULT_CONFIG

    def test_uses_custom_config_when_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When *config* is passed, it is forwarded instead of DEFAULT_CONFIG."""
        raw = MagicMock(spec=httpx.AsyncClient)
        custom = RetryConfig(
            max_retries=2, backoff_base=3.0, backoff_cap=10.0, jitter_factor=0.2
        )

        captured_config: RetryConfig | None = None

        def fake_retry_client(
            raw: httpx.AsyncClient, config: RetryConfig
        ) -> RetryClient:
            nonlocal captured_config
            captured_config = config
            return MagicMock(spec=RetryClient)

        monkeypatch.setattr(
            "robotsix_central_deploy._http.RetryClient",
            fake_retry_client,
        )

        wrap_retry_client(raw, config=custom)
        assert captured_config is custom

    def test_returns_retry_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """wrap_retry_client returns a RetryClient instance."""
        raw = MagicMock(spec=httpx.AsyncClient)

        result = wrap_retry_client(raw)
        assert isinstance(result, RetryClient)

    def test_does_not_close_raw_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """wrap_retry_client does NOT close the raw client — the caller owns it."""
        raw = MagicMock(spec=httpx.AsyncClient)

        _result = wrap_retry_client(raw)
        raw.aclose.assert_not_called()
