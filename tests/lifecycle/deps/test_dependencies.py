"""Direct tests for dependency providers in lifecycle.deps.dependencies."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request, status

from robotsix_central_deploy.lifecycle.deps.dependencies import (
    _compute_overall_health,
    _get_backend,
    _get_config,
    _get_or_create_record,
    _get_sibling_pairs,
    _get_store,
)
from robotsix_central_deploy.lifecycle.models import HealthStatus


# ---------------------------------------------------------------------------
# Helper to build a minimal mock Request
# ---------------------------------------------------------------------------


def _make_request(**state_attrs) -> MagicMock:
    """Return a MagicMock-spec Request whose ``app.state`` holds the given attrs."""
    req = MagicMock(spec=Request)
    req.app.state = SimpleNamespace(**state_attrs)
    return req


# ===========================================================================
# Category 1 — Assertion guards
# ===========================================================================


class TestGetStore:
    """Tests for ``_get_store`` — asserts store is not None."""

    async def test_raises_assertion_error_when_store_none(self) -> None:
        """If app.state.store is None, an AssertionError is raised."""
        req = _make_request(store=None)
        with pytest.raises(AssertionError, match="store not initialised"):
            await _get_store(req)

    async def test_returns_store_when_present(self) -> None:
        """If app.state.store is set, it is returned as-is."""
        store = object()
        req = _make_request(store=store)
        assert await _get_store(req) is store


class TestGetBackend:
    """Tests for ``_get_backend`` — asserts backend is not None."""

    async def test_raises_assertion_error_when_backend_none(self) -> None:
        """If app.state.backend is None, an AssertionError is raised."""
        req = _make_request(backend=None)
        with pytest.raises(AssertionError, match="backend not initialised"):
            await _get_backend(req)

    async def test_returns_backend_when_present(self) -> None:
        """If app.state.backend is set, it is returned as-is."""
        backend = object()
        req = _make_request(backend=backend)
        assert await _get_backend(req) is backend


class TestGetConfig:
    """Tests for ``_get_config`` — asserts config is not None."""

    async def test_raises_assertion_error_when_config_none(self) -> None:
        """If app.state.config is None, an AssertionError is raised."""
        req = _make_request(config=None)
        with pytest.raises(AssertionError, match="config not initialised"):
            await _get_config(req)

    async def test_returns_config_when_present(self) -> None:
        """If app.state.config is set, it is returned as-is."""
        config = object()
        req = _make_request(config=config)
        assert await _get_config(req) is config


# ===========================================================================
# Category 2 — Business logic with HTTPException
# ===========================================================================


class TestGetOrCreateRecord:
    """Tests for ``_get_or_create_record`` — fetches record or raises 404."""

    @pytest.fixture
    def store(self) -> AsyncMock:
        """A mock ServiceStore whose ``get()`` is awaitable."""
        store = AsyncMock()
        store.get = AsyncMock()
        return store

    async def test_raises_404_when_record_absent(self, store: AsyncMock) -> None:
        """When store.get returns None, HTTP 404 is raised."""
        store.get.return_value = None
        with pytest.raises(HTTPException) as exc_info:
            await _get_or_create_record("missing-svc", store)
        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "missing-svc" in exc_info.value.detail

    async def test_returns_record_when_present(self, store: AsyncMock) -> None:
        """When store.get returns a record, it is returned directly."""
        record = object()
        store.get.return_value = record
        assert await _get_or_create_record("present-svc", store) is record


# ===========================================================================
# Category 3 — Pure logic
# ===========================================================================


class TestGetSiblingPairs:
    """Tests for ``_get_sibling_pairs`` — best-effort sibling lookup."""

    @pytest.fixture
    def store(self) -> AsyncMock:
        """A mock ServiceStore whose ``get()`` is awaitable."""
        s = AsyncMock()
        s.get = AsyncMock()
        return s

    @staticmethod
    def _sibling(service_key: str) -> MagicMock:
        """Create a minimal mock ServiceConfig with just service_key."""
        sib = MagicMock()
        sib.service_key = service_key
        return sib

    async def test_empty_siblings_returns_empty(
        self,
        store: AsyncMock,
    ) -> None:
        """When config has no siblings, an empty list is returned."""
        config = MagicMock()
        config.siblings = []
        result = await _get_sibling_pairs("primary", config, store)
        assert result == []

    async def test_happy_path_returns_pairs(
        self,
        store: AsyncMock,
    ) -> None:
        """When all sibling records exist, each produces a (config, record) pair."""
        sib_a = self._sibling("a")
        sib_b = self._sibling("b")
        config = MagicMock()
        config.siblings = [sib_a, sib_b]

        rec_a = object()
        rec_b = object()
        store.get.side_effect = [rec_a, rec_b]

        result = await _get_sibling_pairs("primary", config, store)
        assert result == [(sib_a, rec_a), (sib_b, rec_b)]

    async def test_missing_sibling_logged_and_skipped(
        self,
        store: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When a sibling record is missing, a warning is logged and it is skipped."""
        sib_a = self._sibling("a")
        sib_b = self._sibling("b")
        config = MagicMock()
        config.siblings = [sib_a, sib_b]

        rec_a = object()
        store.get.side_effect = [rec_a, None]

        with caplog.at_level(logging.WARNING):
            result = await _get_sibling_pairs("primary", config, store)

        assert result == [(sib_a, rec_a)]
        assert "primary-b" in caplog.text
        assert "not found" in caplog.text


class TestComputeOverallHealth:
    """Tests for ``_compute_overall_health`` — health rollup across containers."""

    @staticmethod
    def _summary(health: str, name: str = "c") -> MagicMock:
        """Create a minimal mock ContainerHealthSummary."""
        s = MagicMock()
        s.health = health
        s.name = name
        return s

    def test_no_healthchecks_returns_empty(self) -> None:
        """When primary is '' and no siblings, return ''."""
        assert _compute_overall_health("", []) == ""

    def test_only_neutral_returns_empty(self) -> None:
        """When all containers have health='' (no healthcheck), return ''."""
        siblings = [self._summary(""), self._summary("")]
        assert _compute_overall_health("", siblings) == ""

    def test_unhealthy_takes_priority(self) -> None:
        """When any container is UNHEALTHY, return UNHEALTHY immediately."""
        siblings = [self._summary(HealthStatus.HEALTHY)]
        assert (
            _compute_overall_health(HealthStatus.UNHEALTHY, siblings)
            == HealthStatus.UNHEALTHY
        )

    def test_sibling_unhealthy_takes_priority(self) -> None:
        """When a sibling is UNHEALTHY, return UNHEALTHY even if primary is healthy."""
        siblings = [
            self._summary(HealthStatus.HEALTHY),
            self._summary(HealthStatus.UNHEALTHY),
        ]
        assert (
            _compute_overall_health(HealthStatus.HEALTHY, siblings)
            == HealthStatus.UNHEALTHY
        )

    def test_starting_when_no_unhealthy(self) -> None:
        """When no UNHEALTHY but STARTING present, return STARTING."""
        siblings = [self._summary(HealthStatus.STARTING)]
        assert (
            _compute_overall_health(HealthStatus.HEALTHY, siblings)
            == HealthStatus.STARTING
        )

    def test_all_healthy_returns_healthy(self) -> None:
        """When every checked container is HEALTHY, return HEALTHY."""
        siblings = [
            self._summary(HealthStatus.HEALTHY),
            self._summary(HealthStatus.HEALTHY),
        ]
        assert (
            _compute_overall_health(HealthStatus.HEALTHY, siblings)
            == HealthStatus.HEALTHY
        )

    def test_mixed_no_priority_disease_returns_empty(self) -> None:
        """When checked values exist but none are UNHEALTHY, STARTING, or all-HEALTHY, return ''."""
        siblings = [self._summary("some-unknown-status")]
        assert _compute_overall_health(HealthStatus.HEALTHY, siblings) == ""
