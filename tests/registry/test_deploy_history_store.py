"""Tests for DeployHistoryStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from robotsix_central_deploy.lifecycle.models import DeployHistoryEntry
from robotsix_central_deploy.registry.deploy_history_store import (
    DeployHistoryStore,
    MAX_HISTORY_ENTRIES,
)


def _make_entry(
    digest: str = "sha256:abc",
    image_ref: str = "ghcr.io/org/svc:main",
    timestamp: float = 1000.0,
    source: str = "manual",
    previous_digest: str = "sha256:old",
) -> DeployHistoryEntry:
    return DeployHistoryEntry(
        digest=digest,
        image_ref=image_ref,
        timestamp=timestamp,
        source=source,
        previous_digest=previous_digest,
    )


class TestDeployHistoryStoreAppend:
    @pytest.mark.asyncio
    async def test_append_creates_entry(self, tmp_path: Path):
        store = DeployHistoryStore(tmp_path / "history.json")
        entry = _make_entry()
        await store.append("svc-a", entry)

        entries = await store.list("svc-a")
        assert len(entries) == 1
        assert entries[0].digest == "sha256:abc"
        assert entries[0].source == "manual"
        assert entries[0].previous_digest == "sha256:old"

    @pytest.mark.asyncio
    async def test_list_most_recent_first(self, tmp_path: Path):
        store = DeployHistoryStore(tmp_path / "history.json")
        await store.append("svc-a", _make_entry(digest="sha256:first", timestamp=1.0))
        await store.append("svc-a", _make_entry(digest="sha256:second", timestamp=2.0))

        entries = await store.list("svc-a")
        assert len(entries) == 2
        assert entries[0].digest == "sha256:second"
        assert entries[1].digest == "sha256:first"

    @pytest.mark.asyncio
    async def test_list_empty_when_no_history(self, tmp_path: Path):
        store = DeployHistoryStore(tmp_path / "history.json")
        entries = await store.list("svc-a")
        assert entries == []

    @pytest.mark.asyncio
    async def test_list_unknown_component_returns_empty(self, tmp_path: Path):
        store = DeployHistoryStore(tmp_path / "history.json")
        await store.append("svc-a", _make_entry())
        entries = await store.list("svc-b")
        assert entries == []

    @pytest.mark.asyncio
    async def test_caps_at_max_entries(self, tmp_path: Path):
        store = DeployHistoryStore(tmp_path / "history.json")
        for i in range(MAX_HISTORY_ENTRIES + 5):
            await store.append("svc-a", _make_entry(digest=f"sha256:{i:03d}"))

        entries = await store.list("svc-a")
        assert len(entries) == MAX_HISTORY_ENTRIES
        # Most recent first: the last appended entry should be at index 0
        assert entries[0].digest == f"sha256:{MAX_HISTORY_ENTRIES + 4:03d}"
        # Oldest retained entry
        assert entries[-1].digest == f"sha256:{5:03d}"

    @pytest.mark.asyncio
    async def test_round_trip_through_save_and_load(self, tmp_path: Path):
        path = tmp_path / "history.json"
        store1 = DeployHistoryStore(path)
        await store1.append("svc-a", _make_entry(digest="sha256:one"))
        await store1.append("svc-b", _make_entry(digest="sha256:two"))

        # New instance re-reads the same file
        store2 = DeployHistoryStore(path)
        entries_a = await store2.list("svc-a")
        entries_b = await store2.list("svc-b")
        assert len(entries_a) == 1
        assert entries_a[0].digest == "sha256:one"
        assert len(entries_b) == 1
        assert entries_b[0].digest == "sha256:two"

    @pytest.mark.asyncio
    async def test_persists_across_instances(self, tmp_path: Path):
        path = tmp_path / "history.json"
        store1 = DeployHistoryStore(path)
        await store1.append("svc-a", _make_entry(digest="sha256:one"))

        # Append more with a new instance
        store2 = DeployHistoryStore(path)
        await store2.append("svc-a", _make_entry(digest="sha256:two"))

        entries = await store2.list("svc-a")
        assert len(entries) == 2
        assert entries[0].digest == "sha256:two"
        assert entries[1].digest == "sha256:one"

    @pytest.mark.asyncio
    async def test_all_source_types(self, tmp_path: Path):
        store = DeployHistoryStore(tmp_path / "history.json")
        await store.append("svc-a", _make_entry(source="manual"))
        await store.append("svc-a", _make_entry(source="caretaker"))
        await store.append("svc-a", _make_entry(source="rollback"))

        entries = await store.list("svc-a")
        sources = [e.source for e in entries]
        assert sources == ["rollback", "caretaker", "manual"]
