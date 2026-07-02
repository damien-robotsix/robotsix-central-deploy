"""JSON-backed persistence for per-component config.yaml schema and values.

Stores a ``template`` (parsed from the repo's ``config/config.yaml``, immutable
after onboard) and ``current`` (user-saved merged dict) for each component.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ConfigYamlStore:
    """Persist per-component config.yaml template and current values to a JSON file.

    Uses a read-modify-write pattern with an ``asyncio.Lock`` for writes,
    matching the pattern of ``EnvStore`` in ``registry/env_store.py``.
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._lock = asyncio.Lock()

    async def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        raw = self._path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data: dict[str, Any] = json.loads(raw)
        return data

    async def _save(self, data: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.rename(self._path)

    async def get_template(self, name: str) -> dict[str, Any] | None:
        data = await self._load()
        entry: dict[str, Any] | None = data.get(name)
        if entry is None:
            return None
        template: dict[str, Any] | None = entry.get("template")
        return template

    async def get_current(self, name: str) -> dict[str, Any] | None:
        data = await self._load()
        entry: dict[str, Any] | None = data.get(name)
        if entry is None:
            return None
        current: dict[str, Any] | None = entry.get("current")
        return current

    async def save_template(self, name: str, template: dict[str, Any]) -> None:
        """Store/overwrite *template*; preserve existing *current* if present."""
        async with self._lock:
            data = await self._load()
            existing = data.get(name, {})
            existing["template"] = template
            data[name] = existing
            await self._save(data)

    async def update_current(self, name: str, current: dict[str, Any]) -> None:
        """Update only the *current* dict for *name*."""
        async with self._lock:
            data = await self._load()
            entry = data.get(name, {})
            entry["current"] = current
            data[name] = entry
            await self._save(data)

    async def get_volume_hash(self, name: str) -> str | None:
        """Return the stored volume hash for *name*, or None if absent."""
        data = await self._load()
        result: str | None = data.get(name, {}).get("volume_hash")
        return result

    async def update_current_and_hash(
        self, name: str, current: dict[str, Any], volume_hash: str
    ) -> None:
        """Atomically update *current* and *volume_hash* in one JSON write."""
        async with self._lock:
            data = await self._load()
            entry = data.get(name, {})
            entry["current"] = current
            entry["volume_hash"] = volume_hash
            data[name] = entry
            await self._save(data)

    async def delete(self, name: str) -> None:
        """Remove the entire entry for *name*. No-op if absent."""
        async with self._lock:
            data = await self._load()
            data.pop(name, None)
            await self._save(data)
