"""Tests for the shared JSON file persistence helpers in _store_utils."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from robotsix_central_deploy.registry._store_utils import (
    JsonFileStore,
    async_read_json,
    async_write_json,
)


# ---------------------------------------------------------------------------
# async_read_json
# ---------------------------------------------------------------------------


class TestAsyncReadJson:
    @pytest.mark.asyncio
    async def test_non_existent_file_returns_empty_dict(self, tmp_path: Path):
        result = await async_read_json(tmp_path / "nonexistent.json")
        assert result == {}

    @pytest.mark.asyncio
    async def test_empty_file_returns_empty_dict(self, tmp_path: Path):
        path = tmp_path / "empty.json"
        path.write_text("", encoding="utf-8")
        result = await async_read_json(path)
        assert result == {}

    @pytest.mark.asyncio
    async def test_whitespace_only_file_returns_empty_dict(self, tmp_path: Path):
        path = tmp_path / "whitespace.json"
        path.write_text("   \n  \t  ", encoding="utf-8")
        result = await async_read_json(path)
        assert result == {}

    @pytest.mark.asyncio
    async def test_valid_json(self, tmp_path: Path):
        path = tmp_path / "valid.json"
        data = {"key": "value", "nested": {"a": 1}}
        path.write_text(json.dumps(data), encoding="utf-8")
        result = await async_read_json(path)
        assert result == data

    @pytest.mark.asyncio
    async def test_malformed_json_raises(self, tmp_path: Path):
        path = tmp_path / "malformed.json"
        path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            await async_read_json(path)


# ---------------------------------------------------------------------------
# async_write_json
# ---------------------------------------------------------------------------


class TestAsyncWriteJson:
    @pytest.mark.asyncio
    async def test_writes_indented_json(self, tmp_path: Path):
        path = tmp_path / "out.json"
        data = {"b": 1, "a": 2}
        await async_write_json(path, data)
        raw = path.read_text(encoding="utf-8")
        expected = json.dumps(data, indent=2, sort_keys=True)
        assert raw == expected

    @pytest.mark.asyncio
    async def test_round_trip_through_read(self, tmp_path: Path):
        path = tmp_path / "roundtrip.json"
        data = {"hello": "world", "flag": True, "count": 42}
        await async_write_json(path, data)
        result = await async_read_json(path)
        assert result == data

    @pytest.mark.asyncio
    async def test_no_tmp_file_left_behind(self, tmp_path: Path):
        """After atomic write the .tmp file must not exist — only the target."""
        path = tmp_path / "out.json"
        await async_write_json(path, {"k": "v"})
        assert path.exists()
        assert not path.with_suffix(".tmp").exists()


# ---------------------------------------------------------------------------
# JsonFileStore
# ---------------------------------------------------------------------------


class _IncrementMutator:
    """Mutator that increments a counter key in the stored dict."""

    def __call__(self, data: dict) -> None:
        data["count"] = data.get("count", 0) + 1


class TestJsonFileStore:
    @pytest.mark.asyncio
    async def test_load_returns_empty_for_non_existent_store(self, tmp_path: Path):
        store = JsonFileStore(tmp_path / "store.json")
        assert await store._load() == {}

    @pytest.mark.asyncio
    async def test_save_and_load_round_trip(self, tmp_path: Path):
        store = JsonFileStore(tmp_path / "store.json")
        data = {"key": "val", "nested": {"x": [1, 2]}}
        await store._save(data)
        assert await store._load() == data

    @pytest.mark.asyncio
    async def test_update_applies_mutator_and_persists(self, tmp_path: Path):
        store = JsonFileStore(tmp_path / "store.json")
        await store._save({"count": 0})
        await store._update(_IncrementMutator())
        assert await store._load() == {"count": 1}

    @pytest.mark.asyncio
    async def test_update_lock_serialises_concurrent_modifications(
        self, tmp_path: Path
    ):
        """Concurrent _update calls must serialise: 100 calls → count == 100."""
        store = JsonFileStore(tmp_path / "store.json")
        await store._save({"count": 0})

        async def increment() -> None:
            await store._update(_IncrementMutator())

        tasks = [asyncio.create_task(increment()) for _ in range(100)]
        await asyncio.gather(*tasks)

        assert await store._load() == {"count": 100}
