"""Integration tests for GET /volumes/orphans and POST /volumes/prune.

Orphan = a Docker volume owned by no registered component AND not attached to
any container (``in_use=False``).  A component's own volumes (even when the
component is stopped) and in-use volumes must never be pruned.
"""

from __future__ import annotations

from httpx import AsyncClient

from robotsix_central_deploy.lifecycle.models import DockerDfStats, VolumeStat
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.models import ComponentConfig

import robotsix_central_deploy.lifecycle.app as server_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeVolBackend:
    """Minimal backend exposing disk_df + remove_volume backed by an in-memory
    volume set, so removals are reflected in the follow-up disk_df call."""

    def __init__(
        self, volumes: list[tuple[str, int, bool]], *, no_op_remove: bool = False
    ):
        self._vols = {
            name: VolumeStat(name=name, size_bytes=size, in_use=in_use)
            for name, size, in_use in volumes
        }
        self.removed: list[str] = []
        self._no_op_remove = no_op_remove

    async def disk_df(self) -> DockerDfStats:
        return DockerDfStats(volumes=list(self._vols.values()))

    async def remove_volume(self, name: str) -> None:
        self.removed.append(name)
        if not self._no_op_remove:
            self._vols.pop(name, None)


async def _register_component_with_volume(
    store: ComponentConfigStore, component_id: str, *volume_names: str
) -> None:
    cfg = ComponentConfig(
        id=component_id,
        image="test:latest",
        container_name=component_id,
        named_volumes=list(volume_names),
    )
    await store.put(cfg)


def _set_backend(backend: _FakeVolBackend) -> None:
    server_mod.app.state.__setattr__("backend", backend)


AUTH = {"X-API-Key": "test-key"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestOrphanAuth:
    async def test_orphans_requires_auth(self, client: AsyncClient):
        assert (await client.get("/volumes/orphans")).status_code == 401

    async def test_prune_requires_auth(self, client: AsyncClient):
        assert (await client.post("/volumes/prune")).status_code == 401


# ---------------------------------------------------------------------------
# GET /volumes/orphans
# ---------------------------------------------------------------------------


class TestListOrphans:
    async def test_excludes_owned_and_in_use(self, client: AsyncClient):
        store = server_mod.app.state.component_config_store
        # owned-by-component (stopped), in-use, and two true orphans
        await _register_component_with_volume(store, "svc", "owned-vol")
        _set_backend(
            _FakeVolBackend(
                [
                    ("owned-vol", 100, False),  # owned → excluded even if idle
                    ("busy-vol", 200, True),  # in use → excluded
                    ("orphan-a", 300, False),  # orphan
                    ("orphan-b", 50, False),  # orphan
                ]
            )
        )
        resp = await client.get("/volumes/orphans", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        names = {v["name"] for v in data["volumes"]}
        assert names == {"orphan-a", "orphan-b"}
        assert data["total_bytes"] == 350

    async def test_no_orphans_returns_empty(self, client: AsyncClient):
        store = server_mod.app.state.component_config_store
        await _register_component_with_volume(store, "svc", "owned-vol")
        _set_backend(_FakeVolBackend([("owned-vol", 100, False), ("busy", 5, True)]))
        resp = await client.get("/volumes/orphans", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json() == {"volumes": [], "total_bytes": 0}


# ---------------------------------------------------------------------------
# POST /volumes/prune
# ---------------------------------------------------------------------------


class TestPruneOrphans:
    async def test_prune_all_removes_only_orphans(self, client: AsyncClient):
        store = server_mod.app.state.component_config_store
        await _register_component_with_volume(store, "svc", "owned-vol")
        backend = _FakeVolBackend(
            [
                ("owned-vol", 100, False),
                ("busy-vol", 200, True),
                ("orphan-a", 300, False),
                ("orphan-b", 50, False),
            ]
        )
        _set_backend(backend)
        resp = await client.post("/volumes/prune", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["removed"]) == {"orphan-a", "orphan-b"}
        assert data["failed"] == []
        assert data["space_reclaimed_bytes"] == 350
        # owned + in-use volumes were never touched
        assert set(backend.removed) == {"orphan-a", "orphan-b"}

    async def test_prune_named_subset(self, client: AsyncClient):
        _set_backend(
            _FakeVolBackend([("orphan-a", 300, False), ("orphan-b", 50, False)])
        )
        resp = await client.post(
            "/volumes/prune", headers=AUTH, json={"names": ["orphan-a"]}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["removed"] == ["orphan-a"]
        assert data["space_reclaimed_bytes"] == 300

    async def test_prune_rejects_non_orphan_names(self, client: AsyncClient):
        store = server_mod.app.state.component_config_store
        await _register_component_with_volume(store, "svc", "owned-vol")
        backend = _FakeVolBackend(
            [
                ("owned-vol", 100, False),
                ("busy-vol", 200, True),
                ("orphan-a", 300, False),
            ]
        )
        _set_backend(backend)
        # Attempt to prune an owned volume, an in-use one, and a real orphan.
        resp = await client.post(
            "/volumes/prune",
            headers=AUTH,
            json={"names": ["owned-vol", "busy-vol", "orphan-a"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["removed"] == ["orphan-a"]
        assert set(data["skipped"]) == {"owned-vol", "busy-vol"}
        # Only the orphan was ever handed to remove_volume.
        assert backend.removed == ["orphan-a"]

    async def test_prune_reports_failed_when_removal_noop(self, client: AsyncClient):
        # remove_volume swallows errors; a volume still present afterwards is failed.
        _set_backend(_FakeVolBackend([("orphan-a", 300, False)], no_op_remove=True))
        resp = await client.post("/volumes/prune", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["removed"] == []
        assert data["failed"] == ["orphan-a"]
        assert data["space_reclaimed_bytes"] == 0
