"""JSON-backed persistence for per-component deploy-history entries.

Mirrors the lock + tmp-rename pattern of ``registry/env_store.py``.
"""

from __future__ import annotations

from typing import Any

from ..lifecycle.models import DeployHistoryEntry

from ._store_utils import JsonFileStore

MAX_HISTORY_ENTRIES: int = 20
"""Maximum retained history entries per component. Oldest entries are
dropped beyond this cap."""


class DeployHistoryStore(JsonFileStore):
    """Persist per-component deploy-history entries to a JSON file.

    Uses a read-modify-write pattern with an ``asyncio.Lock`` for writes,
    matching the pattern of ``EnvStore`` in ``registry/env_store.py``.
    """

    async def append(self, name: str, entry: DeployHistoryEntry) -> None:
        """Prepend *entry* to the history for *name*, capping at ``MAX_HISTORY_ENTRIES``."""

        def _mutate(data: dict[str, Any]) -> None:
            entries: list[dict[str, Any]] = data.get(name, [])
            entries.insert(0, entry.model_dump())
            if len(entries) > MAX_HISTORY_ENTRIES:
                entries = entries[:MAX_HISTORY_ENTRIES]
            data[name] = entries

        await self._update(_mutate)

    async def list(self, name: str) -> list[DeployHistoryEntry]:
        """Return history for *name*, most-recent-first; empty list when none."""
        data = await self._load()
        raw_entries: list[dict[str, Any]] = data.get(name, [])
        return [DeployHistoryEntry.model_validate(e) for e in raw_entries]
