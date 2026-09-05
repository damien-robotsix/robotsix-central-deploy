"""Tests for the one-time caretaker auto-update config migration."""

from __future__ import annotations

import json
from pathlib import Path

from robotsix_central_deploy.lifecycle.deps._auto_update_migration import (
    migrate_legacy_auto_update_settings,
)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_maps_legacy_component_flag_and_removes_keys(tmp_path: Path) -> None:
    """caretaker_auto_update is copied onto auto_update_enabled and dropped."""
    comp_path = tmp_path / "component_configs.json"
    settings_path = tmp_path / "system_settings.json"
    config_path = tmp_path / "config.json"
    _write(
        comp_path,
        {
            "mill": {
                "id": "mill",
                "image": "img",
                "container_name": "mill",
                "caretaker_auto_update": False,
            },
            "chat": {
                "id": "chat",
                "image": "img",
                "container_name": "chat",
                "caretaker_auto_update": True,
                "auto_update_enabled": False,  # explicit value wins
            },
        },
    )
    _write(settings_path, {"caretaker_self_update_enabled": False})
    _write(config_path, {"caretaker_self_update_enabled": True})

    result = migrate_legacy_auto_update_settings(comp_path, settings_path, config_path)

    # Settings store wins over config.json.
    assert result is False

    comps = json.loads(comp_path.read_text())
    assert comps["mill"]["auto_update_enabled"] is False
    assert "caretaker_auto_update" not in comps["mill"]
    assert comps["chat"]["auto_update_enabled"] is False  # explicit value preserved
    assert "caretaker_auto_update" not in comps["chat"]

    # Legacy self-update key scrubbed from both sources.
    assert "caretaker_self_update_enabled" not in json.loads(settings_path.read_text())
    assert "caretaker_self_update_enabled" not in json.loads(config_path.read_text())


def test_no_legacy_keys_is_a_noop(tmp_path: Path) -> None:
    """Absent legacy keys leave files untouched and return None."""
    comp_path = tmp_path / "component_configs.json"
    settings_path = tmp_path / "system_settings.json"
    config_path = tmp_path / "config.json"
    _write(
        comp_path,
        {"chat": {"id": "chat", "image": "img", "container_name": "chat"}},
    )
    _write(settings_path, {"caretaker_enabled": True})
    _write(config_path, {"caretaker_enabled": False})

    result = migrate_legacy_auto_update_settings(comp_path, settings_path, config_path)

    assert result is None
    comps = json.loads(comp_path.read_text())
    assert "auto_update_enabled" not in comps["chat"]
    assert "caretaker_enabled" in json.loads(settings_path.read_text())


def test_explicit_true_preserved(tmp_path: Path) -> None:
    """A legacy True self-update value resolves to True."""
    settings_path = tmp_path / "system_settings.json"
    _write(settings_path, {"caretaker_self_update_enabled": True})

    result = migrate_legacy_auto_update_settings(
        tmp_path / "component_configs.json", settings_path, tmp_path / "config.json"
    )

    assert result is True


def test_missing_files_are_tolerated(tmp_path: Path) -> None:
    """No persisted files at all is a safe no-op."""
    result = migrate_legacy_auto_update_settings(
        tmp_path / "component_configs.json",
        tmp_path / "system_settings.json",
        tmp_path / "config.json",
    )
    assert result is None
