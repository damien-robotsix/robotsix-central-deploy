"""JSON-backed persistence for per-component config *schema*.

Stores the ``template`` — the JSON Schema parsed from the repo's shipped
``config/config.json`` — which the deploy UI renders its config form from.

It deliberately does **not** store config *values*. Per robotsix-standards
``config-ownership.md`` the deploy plane keeps no copy of them: a component's
own config file is the single source of truth, read through
``read_component_config``. The previous ``current`` / ``volume_hash`` fields
were exactly such a copy, and their staleness was silent — on 2026-08-07 a
stale entry was written over chat's live Langfuse credentials during a deploy,
removing chat from fleet-wide discovery with nothing erroring anywhere.

The ``previous`` slot is gone too. It backed the chat-agent config rollback,
which restored a template-shaped snapshot over the component's volume — the
same defect as the write path it paired with. Rollback now belongs to the
component's own ``POST /config/rollback`` and its own version history.
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

    async def save_template(self, name: str, template: dict[str, Any]) -> None:
        """Store/overwrite *template*; preserve existing *current* if present."""

        def _mutate(data: dict[str, Any]) -> None:
            existing = data.get(name, {})
            existing["template"] = template
            data[name] = existing

        await self._update(_mutate)

    async def delete(self, name: str) -> None:
        """Remove the entire entry for *name*. No-op if absent."""

        def _mutate(data: dict[str, Any]) -> None:
            data.pop(name, None)

        await self._update(_mutate)
