"""Shared JSON file persistence helpers for registry stores.

All file-backed registry stores use the same tmp-file-rename pattern
for atomic writes, and the same existence-check for loads.  Extracting
these avoids duplicated boilerplate across ``ConfigYamlStore``,
``EnvStore``, and ``DeployHistoryStore``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


async def async_read_json(path: Path) -> dict[str, Any]:
    """Read *path* as JSON, returning ``{}`` when the file is missing or empty."""
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    data: dict[str, Any] = json.loads(raw)
    return data


async def async_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write *data* as indented JSON via a temporary file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.rename(path)


class JsonFileStore:
    """Base class for JSON-file-backed registry stores.

    Provides ``asyncio.Lock``-guarded ``_load`` / ``_save`` and a
    convenience ``_update(mutator)`` helper for the common
    read-modify-write pattern.  Subclasses call ``super().__init__(store_path)``
    and may add extra attributes (e.g. a ``SecretKeyManager``).
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._lock = asyncio.Lock()

    async def _load(self) -> dict[str, Any]:
        return await async_read_json(self._path)

    async def _save(self, data: dict[str, Any]) -> None:
        await async_write_json(self._path, data)

    async def _update(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        """Acquire the lock, load data, invoke *mutator* in-place, then save."""
        async with self._lock:
            data = await self._load()
            mutator(data)
            await self._save(data)
