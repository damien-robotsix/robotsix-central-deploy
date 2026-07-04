"""JSON-backed persistence for chat-agent mutation audit entries.

Follows the same tmp-file-rename pattern as ``DeployHistoryStore``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ._store_utils import async_read_json, async_write_json

MAX_AUDIT_ENTRIES: int = 200
"""Maximum retained audit entries. Oldest entries are dropped beyond this cap."""


class ChatAgentAuditEntry(BaseModel):
    """One entry in the chat-agent audit log."""

    timestamp: float = Field(default_factory=time.time)
    agent_id: str = "chat-agent"
    component: str  # service name
    action: str  # "config_update" | "config_rollback" | "restart" | "update"
    key: str | None = None  # dotted config key (None for lifecycle actions)
    old_value: Any = None
    new_value: Any = None
    detail: str = ""  # additional context (e.g. rollback destination snapshot)


class ChatAgentAuditStore:
    """Persist chat-agent mutation audit entries to a JSON file.

    Uses a read-modify-write pattern with an ``asyncio.Lock`` for writes,
    matching the pattern of ``DeployHistoryStore``.
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._lock = asyncio.Lock()

    async def _load(self) -> list[dict[str, Any]]:
        data = await async_read_json(self._path)
        entries: list[dict[str, Any]] = data.get("entries", [])
        return entries

    async def _save(self, entries: list[dict[str, Any]]) -> None:
        await async_write_json(self._path, {"entries": entries})

    async def append(self, entry: ChatAgentAuditEntry) -> None:
        """Prepend *entry* to the audit log, capping at ``MAX_AUDIT_ENTRIES``."""
        async with self._lock:
            entries = await self._load()
            entries.insert(0, entry.model_dump())
            if len(entries) > MAX_AUDIT_ENTRIES:
                entries = entries[:MAX_AUDIT_ENTRIES]
            await self._save(entries)

    async def list(
        self, limit: int = 50, component: str | None = None
    ) -> list[ChatAgentAuditEntry]:
        """Return recent audit entries, optionally filtered by component.

        Most-recent-first.  *limit* caps the result set.
        """
        entries = await self._load()
        if component is not None:
            entries = [e for e in entries if e.get("component") == component]
        return [ChatAgentAuditEntry.model_validate(e) for e in entries[:limit]]
