"""JSON-backed persistence for chat-agent mutation audit entries.

Follows the same tmp-file-rename pattern as ``DeployHistoryStore``.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from ._store_utils import JsonFileStore

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


class ChatAgentAuditStore(JsonFileStore):
    """Persist chat-agent mutation audit entries to a JSON file.

    Uses a read-modify-write pattern with an ``asyncio.Lock`` for writes,
    matching the pattern of ``DeployHistoryStore``.
    """

    async def append(self, entry: ChatAgentAuditEntry) -> None:
        """Prepend *entry* to the audit log, capping at ``MAX_AUDIT_ENTRIES``."""

        def _mutate(data: dict[str, Any]) -> None:
            entries: list[dict[str, Any]] = data.get("entries", [])
            entries.insert(0, entry.model_dump())
            if len(entries) > MAX_AUDIT_ENTRIES:
                entries = entries[:MAX_AUDIT_ENTRIES]
            data["entries"] = entries

        await self._update(_mutate)

    async def list(
        self, limit: int = 50, component: str | None = None
    ) -> list[ChatAgentAuditEntry]:
        """Return recent audit entries, optionally filtered by component.

        Most-recent-first.  *limit* caps the result set.
        """
        data = await self._load()
        entries: list[dict[str, Any]] = data.get("entries", [])
        if component is not None:
            entries = [e for e in entries if e.get("component") == component]
        return [ChatAgentAuditEntry.model_validate(e) for e in entries[:limit]]
