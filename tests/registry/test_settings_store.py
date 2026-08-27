"""Tests for ``SystemSettingsStore`` and ``SystemSettings`` model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.registry.settings_store import (
    SystemSettings,
    SystemSettingsStore,
)


class TestSystemSettingsModel:
    """Validation rules on the ``SystemSettings`` Pydantic model."""

    def test_defaults(self):
        s = SystemSettings()
        assert s.log_level == "INFO"
        assert s.registry_check_interval == 300

    def test_log_level_validation_valid(self):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            s = SystemSettings(log_level=level)
            assert s.log_level == level

    def test_log_level_case_insensitive(self):
        s = SystemSettings(log_level="debug")
        assert s.log_level == "DEBUG"

    def test_log_level_invalid_raises(self):
        with pytest.raises(ValueError, match="Unknown log level"):
            SystemSettings(log_level="TRACE")

    def test_caretaker_interval_must_be_positive(self):
        with pytest.raises(ValueError, match="caretaker_interval_hours must be >= 1"):
            SystemSettings(caretaker_interval_hours=0)

    def test_volume_audit_interval_must_be_positive(self):
        with pytest.raises(
            ValueError, match="volume_audit_interval_seconds must be >= 1"
        ):
            SystemSettings(volume_audit_interval_seconds=0)

    def test_mill_component_id_empty_accepted(self):
        s = SystemSettings(mill_component_id="   ")
        assert s.mill_component_id == ""

    def test_mill_component_id_stripped(self):
        s = SystemSettings(mill_component_id="  foo  ")
        assert s.mill_component_id == "foo"


class TestSystemSettingsStore:
    """Tests for ``SystemSettingsStore`` persistence and overlay."""

    # -- get / put round-trip --------------------------------------------

    @pytest.mark.asyncio
    async def test_get_returns_defaults_when_file_missing(self, tmp_path: Path):
        store = SystemSettingsStore(tmp_path / "settings.json")
        result = await store.get()
        assert isinstance(result, SystemSettings)
        assert result.log_level == "INFO"

    @pytest.mark.asyncio
    async def test_put_and_get_round_trip(self, tmp_path: Path):
        store = SystemSettingsStore(tmp_path / "settings.json")
        settings = SystemSettings(
            log_level="DEBUG",
            registry_check_interval=120,
        )
        await store.put(settings)
        result = await store.get()
        assert result.log_level == "DEBUG"
        assert result.registry_check_interval == 120

    @pytest.mark.asyncio
    async def test_put_overwrites_existing(self, tmp_path: Path):
        store = SystemSettingsStore(tmp_path / "settings.json")
        await store.put(SystemSettings(log_level="DEBUG"))
        await store.put(SystemSettings(log_level="WARNING"))
        result = await store.get()
        assert result.log_level == "WARNING"

    @pytest.mark.asyncio
    async def test_put_persists_to_disk(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        store = SystemSettingsStore(path)
        await store.put(SystemSettings(log_level="ERROR"))

        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["log_level"] == "ERROR"

    @pytest.mark.asyncio
    async def test_get_with_corrupt_json_falls_back_to_defaults(
        self, tmp_path: Path, caplog
    ):
        path = tmp_path / "settings.json"
        path.write_text("{not valid json", encoding="utf-8")
        store = SystemSettingsStore(path)

        result = await store.get()
        assert result.log_level == "INFO"

    # -- overlay ---------------------------------------------------------

    def test_overlay_when_file_missing_returns_config_unchanged(self, tmp_path: Path):
        store = SystemSettingsStore(tmp_path / "nonexistent.json")
        config = LifecycleConfig(log_level="WARNING", gateway_base_domain="example.com")

        result = store.overlay(config)
        assert result.log_level == "WARNING"
        assert result.gateway_base_domain == "example.com"

    def test_overlay_applies_stored_values(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"log_level": "DEBUG", "gateway_base_domain": "stored.example.com"}
            ),
            encoding="utf-8",
        )
        store = SystemSettingsStore(path)

        config = LifecycleConfig(
            log_level="WARNING", gateway_base_domain="env.example.com"
        )
        result = store.overlay(config)
        assert result.log_level == "DEBUG"
        assert result.gateway_base_domain == "stored.example.com"

    def test_overlay_does_not_mutate_original_config(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"log_level": "DEBUG"}),
            encoding="utf-8",
        )
        store = SystemSettingsStore(path)

        config = LifecycleConfig(log_level="WARNING")
        original_level = config.log_level
        result = store.overlay(config)
        assert result.log_level == "DEBUG"
        assert config.log_level == original_level  # original unchanged

    def test_overlay_stored_default_does_not_discard_a_configured_value(
        self, tmp_path: Path
    ):
        """A stored value still at its default is not an operator choice.

        This assertion used to run the other way — the stored default won and
        the configured 5000 was discarded. That is the bug that broke fleet
        GHCR pulls on 2026-08-27 (see ``overlay``); ``SystemSettings`` fills
        every unset field from ``SETTINGS_DEFAULTS`` and the store is seeded
        with a full key set, so "stored" and "never touched" were
        indistinguishable and config.json could not set these keys at all.
        """
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"log_level": "DEBUG"}),
            encoding="utf-8",
        )
        store = SystemSettingsStore(path)

        config = LifecycleConfig(
            log_level="WARNING",
            rate_limit_api_per_hour=5000,
        )
        result = store.overlay(config)
        # A real stored choice still wins.
        assert result.log_level == "DEBUG"
        # A stored default does not overwrite a configured value.
        assert result.rate_limit_api_per_hour == 5000

    def test_overlay_keeps_a_configured_secret_against_a_seeded_empty_store(
        self, tmp_path: Path
    ):
        """The exact 2026-08-27 GHCR shape: seeded ``""`` over a real PAT.

        ``ghcr_pull_token`` is a ``SETTINGS_DEFAULTS`` key, so the PAT saved
        through the config panel (which writes and reads back ``config.json``,
        and so displayed it as stored) was overwritten with the seeded empty
        string on every boot.
        """
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"ghcr_pull_token": "", "log_level": "DEBUG"}),
            encoding="utf-8",
        )
        store = SystemSettingsStore(path)

        result = store.overlay(
            LifecycleConfig(ghcr_pull_token="ghp_configured", log_level="INFO")
        )

        assert result.ghcr_pull_token.get_secret_value() == "ghp_configured"
        assert result.log_level == "DEBUG"

    def test_overlay_stored_secret_still_overrides_the_configured_one(
        self, tmp_path: Path
    ):
        """Rotating a credential through the store must still take effect."""
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"ghcr_pull_token": "ghp_stored"}), encoding="utf-8")
        store = SystemSettingsStore(path)

        result = store.overlay(LifecycleConfig(ghcr_pull_token="ghp_configured"))

        assert result.ghcr_pull_token.get_secret_value() == "ghp_stored"

    def test_overlay_leaves_a_key_at_its_default_when_neither_layer_sets_it(
        self, tmp_path: Path
    ):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"ghcr_pull_token": ""}), encoding="utf-8")
        store = SystemSettingsStore(path)

        result = store.overlay(LifecycleConfig())

        assert result.ghcr_pull_token.get_secret_value() == ""

    def test_overlay_rate_limit_fields(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "rate_limit_api_per_hour": 15000,
                }
            ),
            encoding="utf-8",
        )
        store = SystemSettingsStore(path)

        config = LifecycleConfig(
            rate_limit_api_per_hour=20000,
        )
        result = store.overlay(config)
        assert result.rate_limit_api_per_hour == 15000
