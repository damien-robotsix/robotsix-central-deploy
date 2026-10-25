"""Tests for the system-settings store and the CLI.

- ``SystemSettingsStore`` — file-backed save/load/round-trip/overlay/corruption.
- ``cli.main`` — argument parsing + uvicorn launch (mocked, nothing serves).
- Lifespan first-boot seed behaviour (contract + config-file based).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robotsix_central_deploy.lifecycle import cli
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.registry.settings_store import (
    SystemSettings,
    SystemSettingsStore,
)

# ---------------------------------------------------------------------------
# Helper: the config a mocked robotsix_config.load_config should return
# ---------------------------------------------------------------------------


def _lifecycle_config(**overrides: object) -> LifecycleConfig:
    """Return the ``LifecycleConfig`` a mocked ``load_config`` hands back.

    ``robotsix_config.load_config`` reads one JSON file and applies no
    environment overlay (per the fleet config standard), so these tests build
    the config object directly rather than round-tripping through variables
    nothing reads.
    """
    return LifecycleConfig(execution_backend="noop", **overrides)  # type: ignore[arg-type]


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
    async def test_lifespan_does_not_overwrite_existing_settings_file(self, tmp_path):
        """When a settings file already exists, lifespan must not reseed it."""
        settings_path = tmp_path / "settings.json"
        cfg = _lifecycle_config(
            system_settings_path=str(settings_path),
            secret_key_path=str(tmp_path / "secrets.key"),
        )
        from robotsix_central_deploy.registry.settings_store import (
            SystemSettings,
            SystemSettingsStore,
        )

        # Pre-write a file simulating a previous operator save.
        store = SystemSettingsStore(settings_path)
        await store.put(SystemSettings(gateway_base_domain="custom-op.example.net"))

        from robotsix_central_deploy.lifecycle.app import app
        from robotsix_central_deploy.lifecycle.deps import lifespan

        mock_rc = MagicMock()
        mock_rc.load_config = MagicMock(return_value=cfg)
        with patch.dict("sys.modules", {"robotsix_config": mock_rc}):
            async with lifespan(app):
                stored = await app.state.settings_store.get()
                # not overwritten with the default
                assert stored.gateway_base_domain == "custom-op.example.net"

    async def test_lifespan_seeds_caretaker_defaults(self, tmp_path):
        """First-boot: caretaker fields seed to the model defaults."""
        settings_path = tmp_path / "settings.json"
        cfg = _lifecycle_config(
            system_settings_path=str(settings_path),
            secret_key_path=str(tmp_path / "secrets.key"),
        )

        from robotsix_central_deploy.lifecycle.app import app
        from robotsix_central_deploy.lifecycle.deps import lifespan

        mock_rc = MagicMock()
        mock_rc.load_config = MagicMock(return_value=cfg)
        with patch.dict("sys.modules", {"robotsix_config": mock_rc}):
            async with lifespan(app):
                stored = await app.state.settings_store.get()
                assert stored.caretaker_enabled is False
                assert stored.caretaker_interval_hours == 24

    async def test_lifespan_seeds_caretaker_from_config(self, tmp_path):
        """First-boot: caretaker fields are seeded from the config file."""
        settings_path = tmp_path / "settings.json"
        cfg = _lifecycle_config(
            system_settings_path=str(settings_path),
            secret_key_path=str(tmp_path / "secrets.key"),
            caretaker_enabled=True,
            caretaker_interval_hours=6,
        )

        from robotsix_central_deploy.lifecycle.app import app
        from robotsix_central_deploy.lifecycle.deps import lifespan

        mock_rc = MagicMock()
        mock_rc.load_config = MagicMock(return_value=cfg)
        with patch.dict("sys.modules", {"robotsix_config": mock_rc}):
            async with lifespan(app):
                stored = await app.state.settings_store.get()
                assert stored.caretaker_enabled is True
                assert stored.caretaker_interval_hours == 6
                # Effective config also reflects the seeded values.
                assert app.state.config.caretaker_enabled is True
                assert app.state.config.caretaker_interval_hours == 6

    async def test_lifespan_builds_backend_after_settings_overlay(self, tmp_path):
        """Backend is constructed from the overlaid config, not the raw config.

        When ``system_settings.json`` overrides a setting (e.g.
        ``gateway_base_domain``), the ``DockerSdkBackend`` (or whichever
        backend is selected) must receive the *overlaid* value, not the
        raw config-file value.
        """
        settings_path = tmp_path / "settings.json"
        # Raw config value — different from what the overlay will supply.
        cfg = _lifecycle_config(
            system_settings_path=str(settings_path),
            secret_key_path=str(tmp_path / "secrets.key"),
            gateway_base_domain="file-value.example.com",
        )

        from robotsix_central_deploy.registry.settings_store import (
            SystemSettings,
            SystemSettingsStore,
        )

        # Pre-write a settings file that overrides the config-file value.
        store = SystemSettingsStore(settings_path)
        await store.put(SystemSettings(gateway_base_domain="overlaid.example.com"))

        from robotsix_central_deploy.lifecycle import deps
        from robotsix_central_deploy.lifecycle.app import app
        from robotsix_central_deploy.lifecycle.deps import lifespan

        mock_rc = MagicMock()
        mock_rc.load_config = MagicMock(return_value=cfg)

        with (
            patch.object(
                deps, "_build_backend", wraps=deps._build_backend
            ) as mock_build,
            patch.dict("sys.modules", {"robotsix_config": mock_rc}),
        ):
            async with lifespan(app):
                # _build_backend must have been called at least once.
                mock_build.assert_called_once()
                # The config passed to _build_backend must carry the
                # overlaid gateway_base_domain, not the raw config-file
                # value.
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
