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
        return json.loads(raw)

    async def _save(self, data: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.rename(self._path)

    async def get_template(self, name: str) -> dict | None:
        data = await self._load()
        entry = data.get(name)
        if entry is None:
            return None
        return entry.get("template")

    async def get_current(self, name: str) -> dict | None:
        data = await self._load()
        entry = data.get(name)
        if entry is None:
            return None
        return entry.get("current")

    async def save_template(self, name: str, template: dict) -> None:
        """Store/overwrite *template*; preserve existing *current* if present."""
        async with self._lock:
            data = await self._load()
            existing = data.get(name, {})
            existing["template"] = template
            data[name] = existing
            await self._save(data)

    async def update_current(self, name: str, current: dict) -> None:
        """Update only the *current* dict for *name*."""
        async with self._lock:
            data = await self._load()
            entry = data.get(name, {})
            entry["current"] = current
            data[name] = entry
            await self._save(data)

    async def delete(self, name: str) -> None:
        """Remove the entire entry for *name*. No-op if absent."""
        async with self._lock:
            data = await self._load()
            data.pop(name, None)
            await self._save(data)
