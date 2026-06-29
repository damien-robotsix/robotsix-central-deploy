"""Tests for ComponentConfigStore corruption guard."""

from __future__ import annotations

import json
import logging
from pathlib import Path


from robotsix_central_deploy.registry.config_store import ComponentConfigStore
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
            json.dumps(
                {
                    "svc-a": _make_config("svc-a").model_dump(),
                }
            ),
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
        with caplog.at_level(
            logging.ERROR, logger="robotsix_central_deploy.registry.config_store"
        ):
            _ = store.all()

        # At least one ERROR message mentioning the path
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) >= 1
        assert "ComponentConfigStore" in errors[0].message
