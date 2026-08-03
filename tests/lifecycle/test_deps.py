"""Tests for the deps module — state accessors and utility functions."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import robotsix_central_deploy.lifecycle.deps as deps_mod
from robotsix_central_deploy.lifecycle.config import VirtualComponentEntry
from robotsix_central_deploy.lifecycle.deps.background import (
    _refresh_claude_credentials,
)
from robotsix_central_deploy.lifecycle.models import ServiceRecord
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import ComponentConfig
from robotsix_http import ExternalHTTPError


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

    async def test_refresh_loop_records_missing_refresh_token(self) -> None:
        """The exact state that took the fleet down: a credential with an empty
        refreshToken. The loop cannot renew it, but it must not skip in silence
        — that left the panel reporting a stale error while the access token
        quietly ran out and every component started 401ing."""
        backend = MagicMock()
        backend.check_claude_auth = AsyncMock(return_value={"status": "authenticated"})
        backend.read_claude_credentials = AsyncMock(
            return_value={
                "claudeAiOauth": {
                    "accessToken": "at",
                    "refreshToken": "",
                    "expiresAt": int(time.time() * 1000) + 3_600_000,
                    "scopes": ["user:inference"],
                }
            }
        )

        with patch.object(asyncio, "sleep", side_effect=[None, asyncio.CancelledError]):
            await deps_mod._claude_auth_refresh_loop(backend, 1)

        state = deps_mod.get_claude_auth_refresh_state()
        assert state["last_refresh"] is not None
        assert "no refresh token" in state["last_error"]
        # No grant is possible, so nothing may be written over the credential.
        backend.write_claude_credentials.assert_not_called()

    async def test_refresh_loop_success_path(self) -> None:
        """Full success path: authenticated, expiring soon, refresh succeeds."""
        from contextlib import asynccontextmanager
        from collections.abc import AsyncIterator

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

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=fake_response)

        @asynccontextmanager
        async def _mock_retry_ctx(*, timeout: float = 30.0, **kwargs) -> AsyncIterator:  # type: ignore[misc]
            yield mock_client

        deps_mod_path = "robotsix_central_deploy.lifecycle.deps.background"
        with (
            patch.object(asyncio, "sleep", side_effect=[None, asyncio.CancelledError]),
            patch(f"{deps_mod_path}.retry_client_context", _mock_retry_ctx),
        ):
            await deps_mod._claude_auth_refresh_loop(backend, 1)

        # Refresh state should be updated to success.
        state = deps_mod.get_claude_auth_refresh_state()
        assert state["last_refresh"] is not None
        assert state["last_error"] is None
        backend.write_claude_credentials.assert_awaited_once()


class TestRefreshClaudeCredentials:
    """Direct unit tests for ``_refresh_claude_credentials`` covering each
    failure branch: ExternalHTTPError, generic connection error, invalid
    JSON response, missing ``access_token``, and backend write failure."""

    DEFAULT_OAUTH: dict[str, str] = {
        "accessToken": "old-at",
        "refreshToken": "old-rt",
        "expiresAt": "1000000",
        "scopes": "user:inference",
    }

    @staticmethod
    def _mock_retry_ctx(mock_client):
        """Async context manager that yields *mock_client*."""
        from collections.abc import AsyncIterator
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _ctx(*, timeout: float = 30.0, **kwargs) -> AsyncIterator:  # type: ignore[misc]
            yield mock_client

        return _ctx

    # -- helpers -----------------------------------------------------------

    def _make_backend(self, *, write_side_effect=None):
        """Return a MagicMock backend with a working write_claude_credentials."""
        backend = MagicMock()
        backend.write_claude_credentials = AsyncMock(side_effect=write_side_effect)
        return backend

    def _patch_and_call(self, mock_client, backend, oauth=None):
        """Patch retry_client_context, call _refresh_claude_credentials, and
        return the result tuple."""
        deps_bg = "robotsix_central_deploy.lifecycle.deps.background"
        with patch(
            f"{deps_bg}.retry_client_context", self._mock_retry_ctx(mock_client)
        ):
            return asyncio.run(
                _refresh_claude_credentials(backend, oauth or self.DEFAULT_OAUTH)
            )

    # -- failure branches --------------------------------------------------

    def test_external_http_error_with_json_body(self):
        """ExternalHTTPError whose response carries a JSON error object."""
        fake_resp = MagicMock()
        fake_resp.json.return_value = {
            "error": {"message": "invalid_grant"},
        }
        fake_resp.text = '{"error":{"message":"invalid_grant"}}'
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=ExternalHTTPError(
                "bad request",
                status_code=400,
                response=fake_resp,
            )
        )

        success, error = self._patch_and_call(mock_client, backend)

        assert success is False
        assert error is not None
        assert "400" in error
        assert "invalid_grant" in error
        backend.write_claude_credentials.assert_not_called()

    def test_generic_connection_error(self):
        """A plain Exception with no ``.response`` attribute — treated as
        'Token endpoint unreachable'."""
        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=ConnectionRefusedError("connection refused")
        )

        success, error = self._patch_and_call(mock_client, backend)

        assert success is False
        assert error is not None
        assert "Token endpoint unreachable" in error
        assert "connection refused" in error
        backend.write_claude_credentials.assert_not_called()

    def test_generic_exception_with_http_response(self):
        """An Exception that *does* carry a ``.response`` (like httpx's
        HTTPStatusError) — reported with status code, not 'unreachable'."""
        fake_resp = MagicMock()
        fake_resp.status_code = 400
        fake_resp.text = "Bad Request"
        exc = RuntimeError("boom")
        exc.response = fake_resp

        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=exc)

        success, error = self._patch_and_call(mock_client, backend)

        assert success is False
        assert error is not None
        assert "Refresh rejected" in error
        assert "400" in error
        backend.write_claude_credentials.assert_not_called()

    def test_invalid_json_response(self):
        """200 response whose ``.json()`` raises — 'Invalid JSON in refresh
        response'."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.side_effect = ValueError("Expecting value: line 1")

        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=fake_resp)

        success, error = self._patch_and_call(mock_client, backend)

        assert success is False
        assert error is not None
        assert "Invalid JSON" in error
        assert "Expecting value" in error
        backend.write_claude_credentials.assert_not_called()

    def test_missing_access_token(self):
        """200 + valid JSON but no ``access_token`` key in the payload."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "refresh_token": "new-rt",
            "expires_in": 3600,
        }

        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=fake_resp)

        success, error = self._patch_and_call(mock_client, backend)

        assert success is False
        assert error is not None
        assert "No access_token" in error
        backend.write_claude_credentials.assert_not_called()

    def test_backend_write_failure(self):
        """Full success path until the write — then backend raises."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "access_token": "new-at",
            "refresh_token": "new-rt",
            "expires_in": 3600,
        }

        backend = self._make_backend(write_side_effect=OSError("disk full"))
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=fake_resp)

        success, error = self._patch_and_call(mock_client, backend)

        assert success is False
        assert error is not None
        assert "Failed to write refreshed credentials" in error
        assert "disk full" in error
        backend.write_claude_credentials.assert_awaited_once()

    def test_success_path(self):
        """Full success: 200, valid JSON with access_token, write succeeds."""
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "access_token": "new-at",
            "refresh_token": "new-rt",
            "expires_in": 3600,
        }

        backend = self._make_backend()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=fake_resp)

        success, error = self._patch_and_call(mock_client, backend)

        assert success is True
        assert error is None
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

    async def test_pre_is_virtual_persisted_config_is_backfilled(
        self, tmp_path
    ) -> None:
        """Regression test: a config persisted by the original (buggy)
        virtual-component seeding — before ``is_virtual`` existed, so it
        loads back with ``is_virtual=False`` — must still be recognised as
        virtual by matching its id against the current
        ``virtual_components`` config, both backfilling the stored flag
        and deleting its stale ServiceRecord."""
        store = InMemoryStore()
        config_store = ComponentConfigStore(tmp_path / "config_store.json")
        registry = ComponentRegistry([])

        config_store.register(
            ComponentConfig(id="langfuse", image="", container_name="langfuse")
        )
        await store.put(
            ServiceRecord(name="langfuse", container_name="langfuse", image="")
        )
        virtual_components = [
            VirtualComponentEntry(
                id="langfuse", chat_base_url="https://langfuse.example"
            )
        ]

        await deps_mod._seed_component_registry(
            store, config_store, registry, virtual_components
        )

        assert config_store.get("langfuse").is_virtual is True
        assert await store.get("langfuse") is None
