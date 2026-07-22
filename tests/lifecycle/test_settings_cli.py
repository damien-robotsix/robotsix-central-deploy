"""Tests for the system-settings store and the CLI.

- ``SystemSettingsStore`` — file-backed save/load/round-trip/overlay/corruption.
- ``cli.main`` — argument parsing + uvicorn launch (mocked, nothing serves).
- Lifespan first-boot seed behaviour (contract + env-var based).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from robotsix_central_deploy.lifecycle import cli
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.registry.settings_store import (
    SystemSettings,
    SystemSettingsStore,
)


# ---------------------------------------------------------------------------
# Helper: build a LifecycleConfig from ROBOTSIX_LIFECYCLE_* env vars
# ---------------------------------------------------------------------------


def _make_lifecycle_config_from_env() -> LifecycleConfig:
    """Return a ``LifecycleConfig`` populated from current env vars.

    Used by ``TestSettingsFirstBoot`` so the mock ``robotsix_config`` can
    supply a config that reflects the env vars the test set — the real
    ``robotsix_config.load_config`` reads only a JSON file and does **not**
    overlay environment variables.
    """
    env_map: dict[str, str] = {
        "ROBOTSIX_LIFECYCLE_HOST": "host",
        "ROBOTSIX_LIFECYCLE_PORT": "port",
        "ROBOTSIX_LIFECYCLE_API_KEY": "api_key",
        "ROBOTSIX_LIFECYCLE_AUTH_USERNAME": "auth_username",
        "ROBOTSIX_LIFECYCLE_AUTH_PASSWORD": "auth_password",
        "ROBOTSIX_LIFECYCLE_STORE_BACKEND": "store_backend",
        "ROBOTSIX_LIFECYCLE_STORE_PATH": "store_path",
        "ROBOTSIX_LIFECYCLE_EXECUTION_BACKEND": "execution_backend",
        "ROBOTSIX_LIFECYCLE_COMPONENT_CONFIG_STORE_PATH": "component_config_store_path",
        "ROBOTSIX_LIFECYCLE_DOCKER_SOCKET_URL": "docker_socket_url",
        "ROBOTSIX_LIFECYCLE_DOCKER_SDK_TIMEOUT": "docker_sdk_timeout",
        "ROBOTSIX_LIFECYCLE_DISK_PATH": "disk_path",
        "ROBOTSIX_LIFECYCLE_DISK_WARN_PCT": "disk_warn_pct",
        "ROBOTSIX_LIFECYCLE_ENV_STORE_PATH": "env_store_path",
        "ROBOTSIX_LIFECYCLE_SECRET_KEY_PATH": "secret_key_path",
        "ROBOTSIX_LIFECYCLE_CONFIG_YAML_STORE_PATH": "config_yaml_store_path",
        "ROBOTSIX_LIFECYCLE_SELF_UPDATE_WATCHTOWER_IMAGE": "self_update_watchtower_image",
        "ROBOTSIX_LIFECYCLE_SELF_UPDATE_DOCKER_API_VERSION": "self_update_docker_api_version",
        "ROBOTSIX_LIFECYCLE_REGISTRY_CHECK_TTL": "registry_check_ttl",
        "ROBOTSIX_LIFECYCLE_REGISTRY_CHECK_INTERVAL": "registry_check_interval",
        "ROBOTSIX_LIFECYCLE_SYSTEM_SETTINGS_PATH": "system_settings_path",
        "ROBOTSIX_LIFECYCLE_LOG_LEVEL": "log_level",
        "ROBOTSIX_LIFECYCLE_GATEWAY_BASE_DOMAIN": "gateway_base_domain",
        "ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_ENABLED": "volume_audit_enabled",
        "ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_INTERVAL_SECONDS": "volume_audit_interval_seconds",
        "ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_SNAPSHOT_PATH": "volume_audit_snapshot_path",
        "ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_FINDINGS_PATH": "volume_audit_findings_path",
        "ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_GROWTH_THRESHOLD_PCT": "volume_audit_growth_threshold_pct",
        "ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_MIN_DELTA_BYTES": "volume_audit_min_delta_bytes",
        "ROBOTSIX_LIFECYCLE_BOARD_API_URL": "board_api_url",
        "ROBOTSIX_LIFECYCLE_BOARD_API_TOKEN": "board_api_token",
        "ROBOTSIX_LIFECYCLE_BOARD_REPO_ID": "board_repo_id",
        "ROBOTSIX_LIFECYCLE_CARETAKER_ENABLED": "caretaker_enabled",
        "ROBOTSIX_LIFECYCLE_CARETAKER_INTERVAL_HOURS": "caretaker_interval_hours",
    }
    kwargs: dict[str, object] = {}
    for env_name, field_name in env_map.items():
        val = os.environ.get(env_name)
        if val is not None:
            kwargs[field_name] = val
    return LifecycleConfig(**kwargs)  # type: ignore[arg-type]


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
            auth_username="op",
            auth_password="pw",
            disk_warn_pct=15.0,
            registry_check_interval=42,
            log_level="WARNING",
            gateway_base_domain="deploy.example.net",
            caretaker_enabled=True,
            caretaker_interval_hours=12,
        )
        await store.put(original)

        assert path.exists()
        loaded = await store.get()
        assert loaded == original

    async def test_put_creates_parent_directory(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "settings.json"
        store = SystemSettingsStore(path)
        await store.put(SystemSettings())
        assert path.exists()

    async def test_get_corrupt_json_returns_defaults(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("{ this is not json", encoding="utf-8")
        store = SystemSettingsStore(path)
        loaded = await store.get()
        assert loaded == SystemSettings()

    async def test_overlay_missing_file_returns_config_unchanged(self, tmp_path):
        store = SystemSettingsStore(tmp_path / "missing.json")
        cfg = LifecycleConfig(log_level="ERROR")  # type: ignore[call-arg]
        result = store.overlay(cfg)
        assert result is cfg
        assert result.log_level == "ERROR"

    async def test_overlay_existing_file_takes_precedence(self, tmp_path):
        path = tmp_path / "settings.json"
        store = SystemSettingsStore(path)
        await store.put(
            SystemSettings(
                auth_username="stored-user",
                auth_password="stored-pw",
                disk_warn_pct=15.0,
                registry_check_interval=60,
                log_level="DEBUG",
                gateway_base_domain="stored.example.net",
                caretaker_enabled=True,
                caretaker_interval_hours=12,
            )
        )

        cfg = LifecycleConfig(  # type: ignore[call-arg]
            log_level="ERROR",
            gateway_base_domain="env.example.net",
        )
        result = store.overlay(cfg)

        # A copy, not the original.
        assert result is not cfg
        assert result.auth_username == "stored-user"
        assert result.auth_password.get_secret_value() == "stored-pw"
        assert result.disk_warn_pct == 15.0
        assert result.registry_check_interval == 60
        assert result.log_level == "DEBUG"
        assert result.gateway_base_domain == "stored.example.net"
        assert result.caretaker_enabled is True
        assert result.caretaker_interval_hours == 12
        # Original untouched.
        assert cfg.log_level == "ERROR"


# ---------------------------------------------------------------------------
# Lifespan first-boot seed behaviour
# ---------------------------------------------------------------------------


class TestSettingsFirstBoot:
    async def test_lifespan_seeds_default_username_when_no_env_and_no_file(
        self, tmp_path, monkeypatch
    ):
        """First-boot: lifespan writes auth_username='admin' when nothing is configured."""
        settings_path = tmp_path / "settings.json"
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_SYSTEM_SETTINGS_PATH", str(settings_path)
        )
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_EXECUTION_BACKEND", "noop")
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_SECRET_KEY_PATH", str(tmp_path / "secrets.key")
        )
        monkeypatch.delenv("ROBOTSIX_LIFECYCLE_AUTH_USERNAME", raising=False)
        monkeypatch.delenv("ROBOTSIX_LIFECYCLE_AUTH_PASSWORD", raising=False)

        from robotsix_central_deploy.lifecycle.app import app
        from robotsix_central_deploy.lifecycle.deps import lifespan

        mock_rc = MagicMock()
        mock_rc.load_config = MagicMock(return_value=_make_lifecycle_config_from_env())
        with patch.dict("sys.modules", {"robotsix_config": mock_rc}):
            async with lifespan(app):
                stored = await app.state.settings_store.get()
                assert stored.auth_username == "admin"
                assert stored.auth_password == ""
                # Effective config should also reflect the seeded username.
                assert app.state.config.auth_username == "admin"

    async def test_lifespan_seeds_env_username_when_set(self, tmp_path, monkeypatch):
        """First-boot: lifespan uses env-var username instead of 'admin' fallback."""
        settings_path = tmp_path / "settings.json"
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_SYSTEM_SETTINGS_PATH", str(settings_path)
        )
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_EXECUTION_BACKEND", "noop")
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_SECRET_KEY_PATH", str(tmp_path / "secrets.key")
        )
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_AUTH_USERNAME", "operator")
        monkeypatch.delenv("ROBOTSIX_LIFECYCLE_AUTH_PASSWORD", raising=False)

        from robotsix_central_deploy.lifecycle.app import app
        from robotsix_central_deploy.lifecycle.deps import lifespan

        mock_rc = MagicMock()
        mock_rc.load_config = MagicMock(return_value=_make_lifecycle_config_from_env())
        with patch.dict("sys.modules", {"robotsix_config": mock_rc}):
            async with lifespan(app):
                stored = await app.state.settings_store.get()
                assert stored.auth_username == "operator"

    async def test_lifespan_does_not_overwrite_existing_settings_file(
        self, tmp_path, monkeypatch
    ):
        """When a settings file already exists, lifespan must not reseed it."""
        settings_path = tmp_path / "settings.json"
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_SYSTEM_SETTINGS_PATH", str(settings_path)
        )
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_EXECUTION_BACKEND", "noop")
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_SECRET_KEY_PATH", str(tmp_path / "secrets.key")
        )
        monkeypatch.delenv("ROBOTSIX_LIFECYCLE_AUTH_USERNAME", raising=False)

        from robotsix_central_deploy.registry.settings_store import (
            SystemSettings,
            SystemSettingsStore,
        )

        # Pre-write a file simulating a previous operator save.
        store = SystemSettingsStore(settings_path)
        await store.put(
            SystemSettings(auth_username="custom-op", auth_password="secret")
        )

        from robotsix_central_deploy.lifecycle.app import app
        from robotsix_central_deploy.lifecycle.deps import lifespan

        mock_rc = MagicMock()
        mock_rc.load_config = MagicMock(return_value=_make_lifecycle_config_from_env())
        with patch.dict("sys.modules", {"robotsix_config": mock_rc}):
            async with lifespan(app):
                stored = await app.state.settings_store.get()
                assert (
                    stored.auth_username == "custom-op"
                )  # not overwritten with 'admin'
                assert stored.auth_password == "secret"

    async def test_lifespan_seeds_caretaker_defaults_when_no_env(
        self, tmp_path, monkeypatch
    ):
        """First-boot: caretaker fields seed to defaults when no env vars set."""
        settings_path = tmp_path / "settings.json"
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_SYSTEM_SETTINGS_PATH", str(settings_path)
        )
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_EXECUTION_BACKEND", "noop")
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_SECRET_KEY_PATH", str(tmp_path / "secrets.key")
        )
        monkeypatch.delenv("ROBOTSIX_LIFECYCLE_CARETAKER_ENABLED", raising=False)
        monkeypatch.delenv("ROBOTSIX_LIFECYCLE_CARETAKER_INTERVAL_HOURS", raising=False)

        from robotsix_central_deploy.lifecycle.app import app
        from robotsix_central_deploy.lifecycle.deps import lifespan

        mock_rc = MagicMock()
        mock_rc.load_config = MagicMock(return_value=_make_lifecycle_config_from_env())
        with patch.dict("sys.modules", {"robotsix_config": mock_rc}):
            async with lifespan(app):
                stored = await app.state.settings_store.get()
                assert stored.caretaker_enabled is False
                assert stored.caretaker_interval_hours == 24

    async def test_lifespan_seeds_caretaker_from_env(self, tmp_path, monkeypatch):
        """First-boot: caretaker fields are seeded from env vars."""
        settings_path = tmp_path / "settings.json"
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_SYSTEM_SETTINGS_PATH", str(settings_path)
        )
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_EXECUTION_BACKEND", "noop")
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_SECRET_KEY_PATH", str(tmp_path / "secrets.key")
        )
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_CARETAKER_ENABLED", "true")
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_CARETAKER_INTERVAL_HOURS", "6")

        from robotsix_central_deploy.lifecycle.app import app
        from robotsix_central_deploy.lifecycle.deps import lifespan

        mock_rc = MagicMock()
        mock_rc.load_config = MagicMock(return_value=_make_lifecycle_config_from_env())
        with patch.dict("sys.modules", {"robotsix_config": mock_rc}):
            async with lifespan(app):
                stored = await app.state.settings_store.get()
                assert stored.caretaker_enabled is True
                assert stored.caretaker_interval_hours == 6
                # Effective config also reflects the seeded values.
                assert app.state.config.caretaker_enabled is True
                assert app.state.config.caretaker_interval_hours == 6

    async def test_lifespan_builds_backend_after_settings_overlay(
        self, tmp_path, monkeypatch
    ):
        """Backend is constructed from the overlaid config, not the raw config.

        When ``system_settings.json`` overrides a setting (e.g.
        ``gateway_base_domain``), the ``DockerSdkBackend`` (or whichever
        backend is selected) must receive the *overlaid* value, not the
        raw env-var default.
        """
        settings_path = tmp_path / "settings.json"
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_SYSTEM_SETTINGS_PATH", str(settings_path)
        )
        monkeypatch.setenv("ROBOTSIX_LIFECYCLE_EXECUTION_BACKEND", "noop")
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_SECRET_KEY_PATH", str(tmp_path / "secrets.key")
        )
        # Raw config default — different from what the overlay will supply.
        monkeypatch.setenv(
            "ROBOTSIX_LIFECYCLE_GATEWAY_BASE_DOMAIN", "env-default.example.com"
        )
        monkeypatch.delenv("ROBOTSIX_LIFECYCLE_AUTH_USERNAME", raising=False)

        from robotsix_central_deploy.registry.settings_store import (
            SystemSettings,
            SystemSettingsStore,
        )

        # Pre-write a settings file that overrides the env-var default.
        store = SystemSettingsStore(settings_path)
        await store.put(SystemSettings(gateway_base_domain="overlaid.example.com"))

        from robotsix_central_deploy.lifecycle.app import app
        from robotsix_central_deploy.lifecycle.deps import lifespan
        from robotsix_central_deploy.lifecycle import deps

        mock_rc = MagicMock()
        mock_rc.load_config = MagicMock(return_value=_make_lifecycle_config_from_env())

        with patch.object(
            deps, "_build_backend", wraps=deps._build_backend
        ) as mock_build:
            with patch.dict("sys.modules", {"robotsix_config": mock_rc}):
                async with lifespan(app):
                    # _build_backend must have been called at least once.
                    mock_build.assert_called_once()
                    # The config passed to _build_backend must carry the
                    # overlaid gateway_base_domain, not the raw env-var
                    # default.
                    called_cfg = mock_build.call_args[0][0]
                    assert called_cfg.gateway_base_domain == "overlaid.example.com"


# ---------------------------------------------------------------------------
# cli.main — argument parsing + uvicorn launch (mocked)
# ---------------------------------------------------------------------------


class TestCli:
    def test_main_defaults_invokes_uvicorn(self):
        fake_uvicorn = MagicMock()
        fake_robotsix_config = MagicMock()
        fake_robotsix_config.load_config = MagicMock(return_value=LifecycleConfig())
        with patch.dict(
            "sys.modules",
            {"uvicorn": fake_uvicorn, "robotsix_config": fake_robotsix_config},
        ):
            cli.main([])
        fake_uvicorn.run.assert_called_once()
        _, kwargs = fake_uvicorn.run.call_args
        assert kwargs["host"] == "0.0.0.0"
        assert kwargs["port"] == 8100
        assert kwargs["reload"] is False

    def test_main_overrides_applied(self):
        fake_uvicorn = MagicMock()
        fake_robotsix_config = MagicMock()
        fake_robotsix_config.load_config = MagicMock(return_value=LifecycleConfig())
        with patch.dict(
            "sys.modules",
            {"uvicorn": fake_uvicorn, "robotsix_config": fake_robotsix_config},
        ):
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
        fake_robotsix_config = MagicMock()
        fake_robotsix_config.load_config = MagicMock(return_value=LifecycleConfig())
        with patch.dict(
            "sys.modules",
            {"uvicorn": fake_uvicorn, "robotsix_config": fake_robotsix_config},
        ):
            cli.main(["--port", "9000"])
        _, kwargs = fake_uvicorn.run.call_args
        assert kwargs["port"] == 9000
        assert kwargs["host"] == "0.0.0.0"
