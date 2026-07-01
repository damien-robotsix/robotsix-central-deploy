"""Unit tests for the /settings GET and PUT endpoints."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.registry.settings_store import SystemSettings


class TestGetSettings:
    @pytest.mark.asyncio
    async def test_returns_masked_response(self, client, monkeypatch):
        """Secrets are returned as '***' when set."""
        mock_store = MagicMock()
        mock_store.overlay.return_value = LifecycleConfig(
            auth_username="admin",
            auth_password="s3cret",
            disk_warn_pct=15.0,
            registry_check_interval=60,
            log_level="DEBUG",
            gateway_base_domain="example.com",
            claude_host_mount_path="/home/op/.claude",
        )
        server_mod.app.state.__setattr__("settings_store", mock_store)

        resp = await client.get("/settings", headers={"X-API-Key": "test-key"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_username"] == "admin"
        assert data["auth_password"] == "***"
        assert data["disk_warn_pct"] == 15.0
        assert data["registry_check_interval"] == 60
        assert data["log_level"] == "DEBUG"
        assert data["gateway_base_domain"] == "example.com"
        assert data["claude_host_mount_path"] == "/home/op/.claude"

    @pytest.mark.asyncio
    async def test_empty_password_returns_empty_string(self, client, monkeypatch):
        """An empty password is returned as empty, not '***'."""
        mock_store = MagicMock()
        mock_store.overlay.return_value = LifecycleConfig(
            auth_password="",
        )
        server_mod.app.state.__setattr__("settings_store", mock_store)

        resp = await client.get("/settings", headers={"X-API-Key": "test-key"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_password"] == ""

    @pytest.mark.asyncio
    async def test_uses_overlay_with_request_config(self, client, monkeypatch):
        """GET calls settings_store.overlay() with the request's app.state.config."""
        mock_store = MagicMock()
        mock_store.overlay.return_value = server_mod.app.state.config.model_copy()
        server_mod.app.state.__setattr__("settings_store", mock_store)

        await client.get("/settings", headers={"X-API-Key": "test-key"})

        mock_store.overlay.assert_called_once_with(server_mod.app.state.config)


class TestPutSettings:
    @pytest.mark.asyncio
    async def test_updates_and_returns_masked_response(self, client, monkeypatch):
        """Happy path: PUT persists settings and returns masked response."""
        mock_store = MagicMock()
        mock_store.get = AsyncMock(
            return_value=SystemSettings(
                auth_username="old",
                auth_password="oldpass",
            )
        )
        mock_store.put = AsyncMock()
        mock_store.overlay.return_value = server_mod.app.state.config.model_copy(
            update={
                "auth_username": "newadmin",
                "auth_password": "newpass",
                "disk_warn_pct": 5.0,
                "registry_check_interval": 120,
                "log_level": "WARNING",
                "gateway_base_domain": "new.example.com",
                "claude_host_mount_path": "/tmp/.claude",
            }
        )
        server_mod.app.state.__setattr__("settings_store", mock_store)

        resp = await client.put(
            "/settings",
            json={
                "auth_username": "newadmin",
                "auth_password": "newpass",
                "disk_warn_pct": 5.0,
                "registry_check_interval": 120,
                "log_level": "WARNING",
                "gateway_base_domain": "new.example.com",
                "claude_host_mount_path": "/tmp/.claude",
            },
            headers={"X-API-Key": "test-key"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_username"] == "newadmin"
        assert data["auth_password"] == "***"  # masked in response
        assert data["disk_warn_pct"] == 5.0
        assert data["registry_check_interval"] == 120
        assert data["log_level"] == "WARNING"
        assert data["gateway_base_domain"] == "new.example.com"
        assert data["claude_host_mount_path"] == "/tmp/.claude"

        # Verify persistence
        mock_store.put.assert_awaited_once()
        put_arg: SystemSettings = mock_store.put.call_args[0][0]
        assert put_arg.auth_username == "newadmin"
        assert put_arg.auth_password == "newpass"

    @pytest.mark.asyncio
    async def test_preserves_existing_password_when_mask_sent(
        self, client, monkeypatch
    ):
        """When the body sends '***' as auth_password, the stored value is kept."""
        mock_store = MagicMock()
        mock_store.get = AsyncMock(
            return_value=SystemSettings(
                auth_password="existing-secret",
            )
        )
        mock_store.put = AsyncMock()
        mock_store.overlay.return_value = server_mod.app.state.config.model_copy(
            update={"auth_password": "existing-secret"}
        )
        server_mod.app.state.__setattr__("settings_store", mock_store)

        resp = await client.put(
            "/settings",
            json={"auth_password": "***"},
            headers={"X-API-Key": "test-key"},
        )

        assert resp.status_code == 200
        # The response masks it again
        assert resp.json()["auth_password"] == "***"

        # But the stored value should be the original secret
        mock_store.put.assert_awaited_once()
        put_arg: SystemSettings = mock_store.put.call_args[0][0]
        assert put_arg.auth_password == "existing-secret"

    @pytest.mark.asyncio
    async def test_rejects_invalid_log_level_with_422(self, client, monkeypatch):
        """An unknown log_level triggers the Pydantic validator → 422."""
        mock_store = MagicMock()
        mock_store.get = AsyncMock(return_value=SystemSettings())
        mock_store.put = AsyncMock()
        mock_store.overlay.return_value = server_mod.app.state.config.model_copy()
        server_mod.app.state.__setattr__("settings_store", mock_store)

        resp = await client.put(
            "/settings",
            json={"log_level": "BOGUS"},
            headers={"X-API-Key": "test-key"},
        )

        assert resp.status_code == 422
        mock_store.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_normalises_log_level_case(self, client, monkeypatch):
        """log_level is uppercased by the validator (e.g. 'debug' → 'DEBUG')."""
        mock_store = MagicMock()
        mock_store.get = AsyncMock(return_value=SystemSettings())
        mock_store.put = AsyncMock()
        mock_store.overlay.return_value = server_mod.app.state.config.model_copy(
            update={"log_level": "DEBUG"}
        )
        server_mod.app.state.__setattr__("settings_store", mock_store)

        resp = await client.put(
            "/settings",
            json={"log_level": "debug"},
            headers={"X-API-Key": "test-key"},
        )

        assert resp.status_code == 200
        assert resp.json()["log_level"] == "DEBUG"
        put_arg: SystemSettings = mock_store.put.call_args[0][0]
        assert put_arg.log_level == "DEBUG"

    @pytest.mark.asyncio
    async def test_hot_applies_log_level(self, client, monkeypatch):
        """PUT /settings calls logging.getLogger().setLevel() with the new level."""
        mock_store = MagicMock()
        mock_store.get = AsyncMock(return_value=SystemSettings())
        mock_store.put = AsyncMock()
        mock_store.overlay.return_value = server_mod.app.state.config.model_copy(
            update={"log_level": "ERROR"}
        )
        server_mod.app.state.__setattr__("settings_store", mock_store)

        with monkeypatch.context() as m:
            mock_set_level = MagicMock()
            m.setattr(logging.getLogger(), "setLevel", mock_set_level)

            resp = await client.put(
                "/settings",
                json={"log_level": "ERROR"},
                headers={"X-API-Key": "test-key"},
            )

        assert resp.status_code == 200
        mock_set_level.assert_called_once_with("ERROR")

    @pytest.mark.asyncio
    async def test_hot_applies_config_to_app_state(self, client, monkeypatch):
        """PUT /settings updates request.app.state.config with the overlaid config."""
        mock_store = MagicMock()
        mock_store.get = AsyncMock(return_value=SystemSettings())
        mock_store.put = AsyncMock()
        overlaid = server_mod.app.state.config.model_copy(
            update={"disk_warn_pct": 42.0}
        )
        mock_store.overlay.return_value = overlaid
        server_mod.app.state.__setattr__("settings_store", mock_store)

        resp = await client.put(
            "/settings",
            json={"disk_warn_pct": 42.0},
            headers={"X-API-Key": "test-key"},
        )

        assert resp.status_code == 200
        assert server_mod.app.state.config.disk_warn_pct == 42.0
