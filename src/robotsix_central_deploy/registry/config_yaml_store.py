"""JSON-backed persistence for per-component config.json schema and values.

Stores a ``template`` (parsed from the repo's ``config/config.json``, immutable
after onboard) and ``current`` (user-saved merged dict) for each component.
"""

from __future__ import annotations

import logging
from typing import Any

from ._store_utils import JsonFileStore

logger = logging.getLogger(__name__)


class ConfigYamlStore(JsonFileStore):
    """Persist per-component config.json template and current values to a JSON file.

    Uses a read-modify-write pattern with an ``asyncio.Lock`` for writes,
    matching the pattern of ``EnvStore`` in ``registry/env_store.py``.
    """

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

        def _mutate(data: dict[str, Any]) -> None:
            existing = data.get(name, {})
            existing["template"] = template
            data[name] = existing

        await self._update(_mutate)

    async def update_current(self, name: str, current: dict[str, Any]) -> None:
        """Update only the *current* dict for *name*."""

        def _mutate(data: dict[str, Any]) -> None:
            entry = data.get(name, {})
            entry["current"] = current
            data[name] = entry

        await self._update(_mutate)

    async def get_volume_hash(self, name: str) -> str | None:
        """Return the stored volume hash for *name*, or None if absent."""
        data = await self._load()
        result: str | None = data.get(name, {}).get("volume_hash")
        return result

    async def update_current_and_hash(
        self, name: str, current: dict[str, Any], volume_hash: str
    ) -> None:
        """Atomically update *current* and *volume_hash* in one JSON write."""

        def _mutate(data: dict[str, Any]) -> None:
            entry = data.get(name, {})
            entry["current"] = current
            entry["volume_hash"] = volume_hash
            data[name] = entry

        await self._update(_mutate)

    async def get_previous(self, name: str) -> dict[str, Any] | None:
        """Return the previous (pre-rollback) config snapshot for *name*, or None."""
        data = await self._load()
        entry: dict[str, Any] | None = data.get(name)
        if entry is None:
            return None
        previous: dict[str, Any] | None = entry.get("previous")
        return previous

    async def save_previous(self, name: str, previous: dict[str, Any]) -> None:
        """Store a previous-config snapshot for *name* (rollback target)."""

        def _mutate(data: dict[str, Any]) -> None:
            entry = data.get(name, {})
            entry["previous"] = previous
            data[name] = entry

        await self._update(_mutate)

    async def delete(self, name: str) -> None:
        """Remove the entire entry for *name*. No-op if absent."""

        def _mutate(data: dict[str, Any]) -> None:
            data.pop(name, None)

        await self._update(_mutate)
