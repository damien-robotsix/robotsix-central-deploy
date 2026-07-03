"""JSON-backed persistence for per-component deploy-history entries.

Mirrors the lock + tmp-rename pattern of ``registry/env_store.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ..lifecycle.models import DeployHistoryEntry

MAX_HISTORY_ENTRIES: int = 20
"""Maximum retained history entries per component. Oldest entries are
dropped beyond this cap."""


class DeployHistoryStore:
    """Persist per-component deploy-history entries to a JSON file.

    Uses a read-modify-write pattern with an ``asyncio.Lock`` for writes,
    matching the pattern of ``EnvStore`` in ``registry/env_store.py``.
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._lock = asyncio.Lock()

    async def _load(self) -> dict[str, list[dict[str, Any]]]:
        if not self._path.exists():
            return {}
        raw = self._path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        return json.loads(raw)  # type: ignore[no-any-return]

    async def _save(self, data: dict[str, list[dict[str, Any]]]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.rename(self._path)

    async def append(self, name: str, entry: DeployHistoryEntry) -> None:
        """Prepend *entry* to the history for *name*, capping at ``MAX_HISTORY_ENTRIES``."""
        async with self._lock:
            data = await self._load()
            entries: list[dict[str, Any]] = data.get(name, [])
            entries.insert(0, entry.model_dump())
            if len(entries) > MAX_HISTORY_ENTRIES:
                entries = entries[:MAX_HISTORY_ENTRIES]
            data[name] = entries
            await self._save(data)

    async def list(self, name: str) -> list[DeployHistoryEntry]:
        """Return history for *name*, most-recent-first; empty list when none."""
        data = await self._load()
        raw_entries: list[dict[str, Any]] = data.get(name, [])
        return [DeployHistoryEntry.model_validate(e) for e in raw_entries]
