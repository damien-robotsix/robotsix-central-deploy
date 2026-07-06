"""Tests for ``ChatAgentAuditStore`` and ``ChatAgentAuditEntry``."""

from __future__ import annotations

from pathlib import Path

import pytest

from robotsix_central_deploy.registry.chat_agent_audit_store import (
    MAX_AUDIT_ENTRIES,
    ChatAgentAuditEntry,
    ChatAgentAuditStore,
)


class TestChatAgentAuditEntry:
    def test_minimal_entry(self):
        entry = ChatAgentAuditEntry(component="svc-a", action="restart")
        assert entry.component == "svc-a"
        assert entry.action == "restart"
        assert entry.agent_id == "chat-agent"
        assert isinstance(entry.timestamp, float)
        assert entry.key is None
        assert entry.detail == ""

    def test_full_entry(self):
        entry = ChatAgentAuditEntry(
            component="svc-b",
            action="config_update",
            key="log_level",
            old_value="INFO",
            new_value="DEBUG",
            detail="Changed by operator",
        )
        assert entry.component == "svc-b"
        assert entry.action == "config_update"
        assert entry.key == "log_level"
        assert entry.old_value == "INFO"
        assert entry.new_value == "DEBUG"
        assert entry.detail == "Changed by operator"


class TestChatAgentAuditStore:
    """Tests for append, list, and capping behaviour."""

    @pytest.mark.asyncio
    async def test_list_empty_store_returns_empty(self, tmp_path: Path):
        store = ChatAgentAuditStore(tmp_path / "audit.json")
        entries = await store.list()
        assert entries == []

    @pytest.mark.asyncio
    async def test_append_and_list_round_trip(self, tmp_path: Path):
        store = ChatAgentAuditStore(tmp_path / "audit.json")
        entry = ChatAgentAuditEntry(component="svc-a", action="restart")
        await store.append(entry)

        entries = await store.list()
        assert len(entries) == 1
        assert entries[0].component == "svc-a"
        assert entries[0].action == "restart"

    @pytest.mark.asyncio
    async def test_append_prepends_most_recent_first(self, tmp_path: Path):
        store = ChatAgentAuditStore(tmp_path / "audit.json")
        e1 = ChatAgentAuditEntry(component="svc-a", action="restart")
        e2 = ChatAgentAuditEntry(component="svc-b", action="config_update")
        await store.append(e1)
        await store.append(e2)

        entries = await store.list()
        assert len(entries) == 2
        assert entries[0].component == "svc-b"
        assert entries[1].component == "svc-a"

    @pytest.mark.asyncio
    async def test_list_respects_limit(self, tmp_path: Path):
        store = ChatAgentAuditStore(tmp_path / "audit.json")
        for i in range(10):
            await store.append(
                ChatAgentAuditEntry(component=f"svc-{i}", action="restart")
            )

        entries = await store.list(limit=3)
        assert len(entries) == 3
        # Most recent first
        assert entries[0].component == "svc-9"

    @pytest.mark.asyncio
    async def test_list_filters_by_component(self, tmp_path: Path):
        store = ChatAgentAuditStore(tmp_path / "audit.json")
        await store.append(ChatAgentAuditEntry(component="svc-a", action="restart"))
        await store.append(ChatAgentAuditEntry(component="svc-b", action="restart"))
        await store.append(
            ChatAgentAuditEntry(component="svc-a", action="config_update")
        )

        entries = await store.list(component="svc-a")
        assert len(entries) == 2
        assert all(e.component == "svc-a" for e in entries)

    @pytest.mark.asyncio
    async def test_list_component_filter_with_limit(self, tmp_path: Path):
        store = ChatAgentAuditStore(tmp_path / "audit.json")
        for i in range(5):
            await store.append(ChatAgentAuditEntry(component="svc-a", action="restart"))
        await store.append(ChatAgentAuditEntry(component="svc-b", action="restart"))

        entries = await store.list(limit=2, component="svc-a")
        assert len(entries) == 2
        assert all(e.component == "svc-a" for e in entries)

    @pytest.mark.asyncio
    async def test_cap_at_max_entries(self, tmp_path: Path):
        store = ChatAgentAuditStore(tmp_path / "audit.json")
        total = MAX_AUDIT_ENTRIES + 50
        for i in range(total):
            await store.append(
                ChatAgentAuditEntry(component=f"svc-{i}", action="restart")
            )

        entries = await store.list(limit=500)
        assert len(entries) == MAX_AUDIT_ENTRIES
        # Oldest entries should be dropped; newest (highest index) first
        assert entries[0].component == f"svc-{total - 1}"
        assert entries[-1].component == f"svc-{total - MAX_AUDIT_ENTRIES}"

    @pytest.mark.asyncio
    async def test_append_exactly_at_cap(self, tmp_path: Path):
        store = ChatAgentAuditStore(tmp_path / "audit.json")
        for i in range(MAX_AUDIT_ENTRIES):
            await store.append(
                ChatAgentAuditEntry(component=f"svc-{i}", action="restart")
            )

        entries = await store.list(limit=500)
        assert len(entries) == MAX_AUDIT_ENTRIES

    @pytest.mark.asyncio
    async def test_persists_across_store_instances(self, tmp_path: Path):
        path = tmp_path / "audit.json"
        store1 = ChatAgentAuditStore(path)
        await store1.append(ChatAgentAuditEntry(component="svc-a", action="restart"))

        store2 = ChatAgentAuditStore(path)
        entries = await store2.list()
        assert len(entries) == 1
        assert entries[0].component == "svc-a"
