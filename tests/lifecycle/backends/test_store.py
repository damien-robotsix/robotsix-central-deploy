"""Tests for the persistence layer."""

import tempfile
from pathlib import Path

import pytest

from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState
from robotsix_central_deploy.lifecycle.store import (
    FileStore,
    InMemoryStore,
    ServiceStore,
)


@pytest.fixture
def mem_store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def file_store() -> FileStore:
    with tempfile.TemporaryDirectory() as td:
        yield FileStore(Path(td) / "state.yaml")


@pytest.fixture(params=["mem_store", "file_store"])
def store(request) -> ServiceStore:
    return request.getfixturevalue(request.param)


# ---------------------------------------------------------------------------
# Shared behaviour
# ---------------------------------------------------------------------------


class TestStoreContract:
    """Every ServiceStore implementation must pass these."""

    async def test_get_nonexistent_returns_none(self, store: ServiceStore):
        assert await store.get("no-such") is None

    async def test_put_and_get_roundtrip(self, store: ServiceStore):
        rec = ServiceRecord(name="a", image="img", state=ServiceState.STOPPED)
        await store.put(rec)
        got = await store.get("a")
        assert got is not None
        assert got.name == "a"
        assert got.image == "img"
        assert got.state == ServiceState.STOPPED

    async def test_put_updates_in_place(self, store: ServiceStore):
        rec = ServiceRecord(name="b", state=ServiceState.STOPPED)
        await store.put(rec)
        rec.state = ServiceState.RUNNING
        await store.put(rec)
        got = await store.get("b")
        assert got is not None
        assert got.state == ServiceState.RUNNING

    async def test_delete_existing(self, store: ServiceStore):
        await store.put(ServiceRecord(name="c"))
        assert await store.delete("c") is True
        assert await store.get("c") is None

    async def test_delete_nonexistent(self, store: ServiceStore):
        assert await store.delete("ghost") is False

    async def test_list_all_empty(self, store: ServiceStore):
        records = await store.list_all()
        assert records == []

    async def test_list_all_returns_all(self, store: ServiceStore):
        await store.put(ServiceRecord(name="a"))
        await store.put(ServiceRecord(name="b"))
        records = await store.list_all()
        names = {r.name for r in records}
        assert names == {"a", "b"}

    async def test_count(self, store: ServiceStore):
        assert await store.count() == 0
        await store.put(ServiceRecord(name="x"))
        assert await store.count() == 1
        await store.put(ServiceRecord(name="y"))
        assert await store.count() == 2
        await store.delete("x")
        assert await store.count() == 1


class TestFileStoreSpecific:
    """Behaviours unique to the file-backed store."""

    async def test_survives_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.yaml"
            store1 = FileStore(path)
            await store1.put(
                ServiceRecord(name="survivor", state=ServiceState.RUNNING, image="i:v2")
            )

            store2 = FileStore(path)
            got = await store2.get("survivor")
            assert got is not None
            assert got.name == "survivor"
            assert got.state == ServiceState.RUNNING
            assert got.image == "i:v2"
