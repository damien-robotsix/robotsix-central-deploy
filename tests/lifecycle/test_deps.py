"""Tests for the deps module — state accessors and utility functions."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import robotsix_central_deploy.lifecycle.deps as deps_mod
from robotsix_central_deploy.lifecycle.config import VirtualComponentEntry
from robotsix_central_deploy.lifecycle.models import ServiceRecord
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import ComponentConfig


class TestClaudeAuthRefreshState:
    """Tests for ``get_claude_auth_refresh_state``."""

    @pytest.fixture(autouse=True)
    def _reset_state(self) -> None:
        """Reset module-level refresh state before each test."""
        deps_mod._claude_auth_refresh_state = {
            "last_refresh": None,
            "last_error": None,
        }

    def test_returns_snapshot_copy(self) -> None:
        """The returned dict is a copy, not a reference to module state."""
        s1 = deps_mod.get_claude_auth_refresh_state()
        s2 = deps_mod.get_claude_auth_refresh_state()
        assert s1 is not s2
        assert s1 == s2

    def test_default_state(self) -> None:
        """Default state has last_refresh and last_error as None."""
        state = deps_mod.get_claude_auth_refresh_state()
        assert "last_refresh" in state
        assert "last_error" in state
        assert state["last_refresh"] is None
        assert state["last_error"] is None


class TestClaudeAuthRefreshLoop:
    """Tests for ``_claude_auth_refresh_loop`` — one-iteration scenarios."""

    @pytest.fixture(autouse=True)
    def _reset_state(self) -> None:
        """Reset module-level refresh state before each test."""
        deps_mod._claude_auth_refresh_state = {
            "last_refresh": None,
            "last_error": None,
        }

    # sleep side-effect: succeed once, then cancel.
    _sleep_once_then_cancel = [None, asyncio.CancelledError]

    async def test_refresh_loop_not_implemented_returns_early(self) -> None:
        """When the backend raises NotImplementedError, the loop exits."""
        backend = MagicMock()
        backend.check_claude_auth = AsyncMock(side_effect=NotImplementedError)

        with patch.object(asyncio, "sleep", side_effect=[None, asyncio.CancelledError]):
            await deps_mod._claude_auth_refresh_loop(backend, 1)

    async def test_refresh_loop_not_authenticated_skips(self) -> None:
        """When status is not 'authenticated', the loop continues."""
        backend = MagicMock()
        backend.check_claude_auth = AsyncMock(
            return_value={"status": "not-authenticated"}
        )

        with patch.object(asyncio, "sleep", side_effect=[None, asyncio.CancelledError]):
            await deps_mod._claude_auth_refresh_loop(backend, 1)

    async def test_refresh_loop_check_fails_continues(self) -> None:
        """When check_claude_auth raises a generic Exception, loop continues."""
        backend = MagicMock()
        backend.check_claude_auth = AsyncMock(side_effect=RuntimeError("boom"))

        with patch.object(asyncio, "sleep", side_effect=[None, asyncio.CancelledError]):
            await deps_mod._claude_auth_refresh_loop(backend, 1)

    async def test_refresh_loop_success_path(self) -> None:
        """Full success path: authenticated, expiring soon, refresh succeeds."""
        backend = MagicMock()
        backend.check_claude_auth = AsyncMock(return_value={"status": "authenticated"})
        # Credentials with an access token expiring now (triggers refresh).
        backend.read_claude_credentials = AsyncMock(
            return_value={
                "claudeAiOauth": {
                    "accessToken": "old-at",
                    "refreshToken": "old-rt",
                    "expiresAt": int(time.time() * 1000) - 1,  # already expired
                    "scopes": ["user:inference"],
                }
            }
        )
        backend.write_claude_credentials = AsyncMock(
            return_value={"status": "authenticated"}
        )

        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "access_token": "new-at",
            "refresh_token": "new-rt",
            "expires_in": 3600,
        }

        client_instance = MagicMock()
        client_instance.post = AsyncMock(return_value=fake_response)
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=None)

        with (
            patch.object(asyncio, "sleep", side_effect=[None, asyncio.CancelledError]),
            patch.object(deps_mod.httpx, "AsyncClient", return_value=client_instance),
        ):
            await deps_mod._claude_auth_refresh_loop(backend, 1)

        # Refresh state should be updated to success.
        state = deps_mod.get_claude_auth_refresh_state()
        assert state["last_refresh"] is not None
        assert state["last_error"] is None
        backend.write_claude_credentials.assert_awaited_once()


class TestSeedComponentRegistry:
    """Tests for ``_seed_component_registry`` — virtual (non-Docker)
    components must never surface as tracked ``ServiceRecord``s."""

    async def test_virtual_component_gets_no_service_record(self, tmp_path) -> None:
        """A newly-seeded virtual component is registered but has no
        ServiceRecord, so it never shows up as a tracked dashboard row."""
        store = InMemoryStore()
        config_store = ComponentConfigStore(tmp_path / "config_store.json")
        registry = ComponentRegistry([])
        virtual_components = [
            VirtualComponentEntry(
                id="langfuse", chat_base_url="https://langfuse.example"
            )
        ]

        await deps_mod._seed_component_registry(
            store, config_store, registry, virtual_components
        )

        assert config_store.get("langfuse") is not None
        assert registry.get("langfuse") is not None
        assert await store.get("langfuse") is None

    async def test_real_component_still_gets_service_record(self, tmp_path) -> None:
        """A regular Docker-backed component config still gets a
        ServiceRecord seeded, as before."""
        store = InMemoryStore()
        config_store = ComponentConfigStore(tmp_path / "config_store.json")
        registry = ComponentRegistry([])
        config_store.register(
            ComponentConfig(id="mail", image="mail:latest", container_name="mail")
        )

        await deps_mod._seed_component_registry(store, config_store, registry, [])

        record = await store.get("mail")
        assert record is not None
        assert record.container_name == "mail"

    async def test_stale_service_record_for_virtual_component_is_removed(
        self, tmp_path
    ) -> None:
        """Regression test: on a restart after a virtual component was
        already persisted to the config store, a bogus ServiceRecord that
        leaked in (e.g. from before this guard existed) must be deleted —
        not left to render as an 'unknown'-status dashboard row."""
        store = InMemoryStore()
        config_store = ComponentConfigStore(tmp_path / "config_store.json")
        registry = ComponentRegistry([])

        # Simulate the config store already holding a previously-seeded
        # virtual component (as it would after one prior restart)...
        config_store.register(
            ComponentConfig(
                id="deploy", image="", container_name="deploy", is_virtual=True
            )
        )
        # ...and a bogus ServiceRecord that leaked in for it.
        await store.put(ServiceRecord(name="deploy", container_name="deploy", image=""))

        await deps_mod._seed_component_registry(store, config_store, registry, [])

        assert await store.get("deploy") is None
