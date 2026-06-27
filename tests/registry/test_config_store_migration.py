"""Tests for ComponentConfigStore corruption guard and yaml-to-store migration."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from robotsix_central_deploy.lifecycle.server import _migrate_yaml_to_store
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import ComponentConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(id: str, image: str | None = None) -> ComponentConfig:
    return ComponentConfig(
        id=id,
        image=image or f"ghcr.io/robotsix/{id}:latest",
        container_name=id,
    )


def _write_yaml(tmp_path: Path, configs: list[ComponentConfig]) -> Path:
    """Write a temporary components.yaml and return its path."""
    import yaml

    path = tmp_path / "components.yaml"
    raw = {
        "components": [c.model_dump() for c in configs],
    }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _migrate_yaml_to_store tests
# ---------------------------------------------------------------------------


class TestMigrateYamlToStore:
    """Tests for the _migrate_yaml_to_store helper."""

    async def test_migrate_seeds_missing_ids(self, tmp_path: Path):
        """yaml has 2 entries, store file absent — both written, return = 2."""
        svc_a = _make_config("svc-a", "ghcr.io/robotsix/svc-a:v1")
        svc_b = _make_config("svc-b", "ghcr.io/robotsix/svc-b:v1")
        yaml_path = _write_yaml(tmp_path, [svc_a, svc_b])
        registry = ComponentRegistry.from_yaml(yaml_path)

        store_path = tmp_path / "data" / "component_configs.json"
        store = ComponentConfigStore(store_path)

        migrated = await _migrate_yaml_to_store(registry, store)
        assert migrated == 2

        # Verify both are in the store
        stored = store.get("svc-a")
        assert stored is not None
        assert stored.image == "ghcr.io/robotsix/svc-a:v1"
        stored_b = store.get("svc-b")
        assert stored_b is not None
        assert stored_b.image == "ghcr.io/robotsix/svc-b:v1"

    async def test_migrate_skips_existing_ids(self, tmp_path: Path):
        """yaml has id svc-a, store already has svc-a — skip, return = 0."""
        svc_a = _make_config("svc-a", "ghcr.io/robotsix/svc-a:v1")
        yaml_path = _write_yaml(tmp_path, [svc_a])
        registry = ComponentRegistry.from_yaml(yaml_path)

        store_path = tmp_path / "data" / "component_configs.json"
        store = ComponentConfigStore(store_path)

        # Pre-populate the store with a *different* image — this must survive
        pre_existing = _make_config("svc-a", "ghcr.io/robotsix/svc-a:old")
        await store.put(pre_existing)

        migrated = await _migrate_yaml_to_store(registry, store)
        assert migrated == 0

        # Store entry must be unchanged
        stored = store.get("svc-a")
        assert stored is not None
        assert stored.image == "ghcr.io/robotsix/svc-a:old"

    async def test_migrate_partial_overlap(self, tmp_path: Path):
        """yaml has svc-a + svc-b, store has svc-a — svc-b written, svc-a untouched, return = 1."""
        svc_a = _make_config("svc-a", "ghcr.io/robotsix/svc-a:v1")
        svc_b = _make_config("svc-b", "ghcr.io/robotsix/svc-b:v1")
        yaml_path = _write_yaml(tmp_path, [svc_a, svc_b])
        registry = ComponentRegistry.from_yaml(yaml_path)

        store_path = tmp_path / "data" / "component_configs.json"
        store = ComponentConfigStore(store_path)

        # Pre-populate svc-a
        pre_existing = _make_config("svc-a", "ghcr.io/robotsix/svc-a:old")
        await store.put(pre_existing)

        migrated = await _migrate_yaml_to_store(registry, store)
        assert migrated == 1

        # svc-a must be untouched
        stored_a = store.get("svc-a")
        assert stored_a is not None
        assert stored_a.image == "ghcr.io/robotsix/svc-a:old"

        # svc-b must be written
        stored_b = store.get("svc-b")
        assert stored_b is not None
        assert stored_b.image == "ghcr.io/robotsix/svc-b:v1"

    async def test_migrate_empty_yaml(self, tmp_path: Path):
        """Empty yaml registry — return = 0, store unchanged."""
        yaml_path = _write_yaml(tmp_path, [])
        registry = ComponentRegistry.from_yaml(yaml_path)

        store_path = tmp_path / "data" / "component_configs.json"
        store = ComponentConfigStore(store_path)

        migrated = await _migrate_yaml_to_store(registry, store)
        assert migrated == 0
        assert store.all() == []


# ---------------------------------------------------------------------------
# _load corruption guard tests
# ---------------------------------------------------------------------------


class TestConfigStoreCorruptionGuard:
    """Tests for ComponentConfigStore._load() resilience."""

    def test_load_corrupted_json_returns_empty(self, tmp_path: Path):
        """Store file contains invalid JSON — _load returns {} without raising."""
        store_path = tmp_path / "data" / "component_configs.json"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text("{{{ invalid json", encoding="utf-8")

        store = ComponentConfigStore(store_path)
        result = store.all()
        assert result == []

    def test_load_missing_file_returns_empty(self, tmp_path: Path):
        """Store file does not exist — _load returns {} without raising."""
        store_path = tmp_path / "data" / "component_configs.json"
        # Do NOT create the file
        store = ComponentConfigStore(store_path)
        result = store.all()
        assert result == []

    def test_load_valid_json(self, tmp_path: Path):
        """Valid JSON loads correctly."""
        store_path = tmp_path / "data" / "component_configs.json"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text(
            json.dumps({
                "svc-a": _make_config("svc-a").model_dump(),
            }),
            encoding="utf-8",
        )

        store = ComponentConfigStore(store_path)
        result = store.all()
        assert len(result) == 1
        assert result[0].id == "svc-a"

    def test_load_corrupted_json_logs_error(self, tmp_path: Path, caplog):
        """Invalid JSON must produce a logger.error message."""
        store_path = tmp_path / "data" / "component_configs.json"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text("{{{ invalid json", encoding="utf-8")

        store = ComponentConfigStore(store_path)
        with caplog.at_level(logging.ERROR, logger="robotsix_central_deploy.registry.config_store"):
            _ = store.all()

        # At least one ERROR message mentioning the path
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) >= 1
        assert "ComponentConfigStore" in errors[0].message
