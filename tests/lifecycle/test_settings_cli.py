"""Tests for the system-settings store, the settings API router, and the CLI.

- ``SystemSettingsStore`` — file-backed save/load/round-trip/overlay/corruption.
- ``settings_router`` — GET/PUT /settings (masking, secret preservation, 422).
- ``cli.main`` — argument parsing + uvicorn launch (mocked, nothing serves).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from robotsix_central_deploy.lifecycle import cli
from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.registry.settings_store import (
    SystemSettings,
    SystemSettingsStore,
)


# ---------------------------------------------------------------------------
# SystemSettings model
# ---------------------------------------------------------------------------


class TestSystemSettingsModel:
    def test_log_level_normalised_to_upper(self):
        s = SystemSettings(log_level="debug")
        assert s.log_level == "DEBUG"

    def test_invalid_log_level_raises(self):
        with pytest.raises(ValueError, match="Unknown log level"):
            SystemSettings(log_level="LOUD")


# ---------------------------------------------------------------------------
# SystemSettingsStore — file-backed persistence
# ---------------------------------------------------------------------------


class TestSystemSettingsStore:
    async def test_get_missing_file_returns_defaults(self, tmp_path):
        store = SystemSettingsStore(tmp_path / "missing.json")
        loaded = await store.get()
        assert loaded == SystemSettings()
        assert loaded.log_level == "INFO"

    async def test_put_then_get_round_trip(self, tmp_path):
        path = tmp_path / "settings.json"
        store = SystemSettingsStore(path)
        original = SystemSettings(
            ghcr_token="tok",
            auth_username="op",
            auth_password="pw",
            disk_warn_percent=5.0,
            registry_check_interval=42,
            log_level="WARNING",
            gateway_base_domain="deploy.example.net",
            claude_host_mount_path="/home/op/.claude",
        )
        await store.put(original)

        assert path.exists()
        loaded = await store.get()
        assert loaded == original

    async def test_put_creates_parent_directory(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "settings.json"
        store = SystemSettingsStore(path)
        await store.put(SystemSettings(ghcr_token="x"))
        assert path.exists()

    async def test_get_corrupt_json_returns_defaults(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("{ this is not json", encoding="utf-8")
        store = SystemSettingsStore(path)
        loaded = await store.get()
        assert loaded == SystemSettings()

    async def test_overlay_missing_file_returns_config_unchanged(self, tmp_path):
        store = SystemSettingsStore(tmp_path / "missing.json")
        cfg = LifecycleConfig(ghcr_token="env-token", log_level="ERROR")  # type: ignore[call-arg]
        result = store.overlay(cfg)
        assert result is cfg
        assert result.ghcr_token == "env-token"
        assert result.log_level == "ERROR"

    async def test_overlay_existing_file_takes_precedence(self, tmp_path):
        path = tmp_path / "settings.json"
        store = SystemSettingsStore(path)
        await store.put(
            SystemSettings(
                ghcr_token="stored-token",
                auth_username="stored-user",
                auth_password="stored-pw",
                disk_warn_percent=15.0,
                registry_check_interval=60,
                log_level="DEBUG",
                gateway_base_domain="stored.example.net",
                claude_host_mount_path="/stored/.claude",
            )
        )

        cfg = LifecycleConfig(  # type: ignore[call-arg]
            ghcr_token="env-token",
            log_level="ERROR",
            gateway_base_domain="env.example.net",
        )
        result = store.overlay(cfg)

        # A copy, not the original.
        assert result is not cfg
        assert result.ghcr_token == "stored-token"
        assert result.auth_username == "stored-user"
        assert result.auth_password == "stored-pw"
        assert result.disk_warn_percent == 15.0
        assert result.registry_check_interval == 60
        assert result.log_level == "DEBUG"
        assert result.gateway_base_domain == "stored.example.net"
        assert result.claude_host_mount_path == "/stored/.claude"
        # Original untouched.
        assert cfg.ghcr_token == "env-token"


# ---------------------------------------------------------------------------
# settings_router — GET / PUT /settings
#
# Reuses the lifecycle conftest's ``_reset_globals`` (api_key="test-key")
# and ``client`` fixtures, and additionally wires app.state.settings_store.
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_store(tmp_path):
    store = SystemSettingsStore(tmp_path / "system_settings.json")
    server_mod.app.state.settings_store = store
    return store


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


class TestSettingsRouter:
    async def test_get_settings_masks_secrets(
        self, client: AsyncClient, auth_headers, settings_store
    ):
        await settings_store.put(
            SystemSettings(
                ghcr_token="real-token",
                auth_username="operator",
                auth_password="real-pw",
                gateway_base_domain="deploy.example.net",
            )
        )

        resp = await client.get("/settings", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ghcr_token"] == "***"
        assert data["auth_password"] == "***"
        assert data["auth_username"] == "operator"
        assert data["gateway_base_domain"] == "deploy.example.net"

    async def test_get_settings_empty_secrets_unmasked(
        self, client: AsyncClient, auth_headers, settings_store
    ):
        resp = await client.get("/settings", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ghcr_token"] == ""
        assert data["auth_password"] == ""

    async def test_get_settings_reflects_env_vars_when_no_store_file(
        self, client: AsyncClient, auth_headers, settings_store
    ):
        """When no settings file exists, GET /settings must reflect env-var
        credentials from app.state.config rather than returning empty defaults."""
        # Simulate env-var credentials in the running config.
        original_config = server_mod.app.state.config
        server_mod.app.state.config = original_config.model_copy(
            update={"auth_username": "env-user", "auth_password": "env-pass"}
        )
        try:
            # No settings file written — settings_store is empty.
            resp = await client.get("/settings", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["auth_username"] == "env-user"
            assert data["auth_password"] == "***"  # non-empty → masked
        finally:
            server_mod.app.state.config = original_config

    async def test_get_settings_requires_auth(
        self, client: AsyncClient, settings_store
    ):
        resp = await client.get("/settings")
        assert resp.status_code == 401

    async def test_put_settings_persists_and_hot_applies(
        self, client: AsyncClient, auth_headers, settings_store
    ):
        resp = await client.put(
            "/settings",
            json={
                "ghcr_token": "new-token",
                "auth_username": "newop",
                "auth_password": "new-pw",
                "disk_warn_percent": 7.7,
                "registry_check_interval": 120,
                "log_level": "warning",
                "gateway_base_domain": "new.example.net",
                "claude_host_mount_path": "/new/.claude",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        # Secrets masked in the response.
        assert data["ghcr_token"] == "***"
        assert data["auth_password"] == "***"
        assert data["auth_username"] == "newop"
        assert data["log_level"] == "WARNING"

        # Persisted to the store.
        stored = await settings_store.get()
        assert stored.ghcr_token == "new-token"
        assert stored.auth_password == "new-pw"
        assert stored.disk_warn_percent == 7.7

        # Hot-applied into the running config.
        assert server_mod.app.state.config.ghcr_token == "new-token"
        assert server_mod.app.state.config.gateway_base_domain == "new.example.net"

        # registry_checker.set_ghcr_token called with the new token.
        server_mod.app.state.registry_checker.set_ghcr_token.assert_called_with(
            "new-token"
        )

    async def test_put_settings_masked_secret_preserves_existing(
        self, client: AsyncClient, auth_headers, settings_store
    ):
        await settings_store.put(
            SystemSettings(ghcr_token="existing-token", auth_password="existing-pw")
        )

        resp = await client.put(
            "/settings",
            json={
                "ghcr_token": "***",
                "auth_username": "op",
                "auth_password": "***",
                "log_level": "INFO",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200

        stored = await settings_store.get()
        assert stored.ghcr_token == "existing-token"
        assert stored.auth_password == "existing-pw"

    async def test_put_settings_invalid_log_level_returns_422(
        self, client: AsyncClient, auth_headers, settings_store
    ):
        resp = await client.put(
            "/settings",
            json={"log_level": "VERBOSE"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_put_settings_no_registry_checker_ok(
        self, client: AsyncClient, auth_headers, settings_store
    ):
        # Drop the registry_checker so the getattr(...) branch hits None.
        server_mod.app.state.registry_checker = None
        resp = await client.put(
            "/settings",
            json={"ghcr_token": "tok", "log_level": "INFO"},
            headers=auth_headers,
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# cli.main — argument parsing + uvicorn launch (mocked)
# ---------------------------------------------------------------------------


class TestCli:
    def test_main_defaults_invokes_uvicorn(self):
        fake_uvicorn = MagicMock()
        with patch.dict("sys.modules", {"uvicorn": fake_uvicorn}):
            cli.main([])
        fake_uvicorn.run.assert_called_once()
        _, kwargs = fake_uvicorn.run.call_args
        assert kwargs["host"] == "0.0.0.0"
        assert kwargs["port"] == 8100
        assert kwargs["reload"] is False

    def test_main_overrides_applied(self):
        fake_uvicorn = MagicMock()
        with patch.dict("sys.modules", {"uvicorn": fake_uvicorn}):
            cli.main(
                [
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8200",
                    "--store-backend",
                    "file",
                    "--execution-backend",
                    "noop",
                    "--api-key",
                    "secret",
                ]
            )
        fake_uvicorn.run.assert_called_once()
        _, kwargs = fake_uvicorn.run.call_args
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8200

    def test_main_partial_override(self):
        fake_uvicorn = MagicMock()
        with patch.dict("sys.modules", {"uvicorn": fake_uvicorn}):
            cli.main(["--port", "9000"])
        _, kwargs = fake_uvicorn.run.call_args
        assert kwargs["port"] == 9000
        assert kwargs["host"] == "0.0.0.0"
