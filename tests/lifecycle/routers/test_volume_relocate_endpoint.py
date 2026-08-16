"""Integration tests for POST /volumes/{name}/relocate."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.models import ComponentConfig, ServiceConfig

AUTH = {"X-API-Key": "test-key"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register_component(
    store: ComponentConfigStore,
    component_id: str,
    volume_name: str,
    *,
    target_disk: str = "",
    siblings: list[ServiceConfig] | None = None,
    user: str | None = None,
) -> None:
    cfg = ComponentConfig(
        id=component_id,
        image="test:latest",
        container_name=component_id,
        named_volumes=[volume_name],
        target_disk=target_disk,
        siblings=siblings or [],
        user=user,
    )
    await store.put(cfg)


async def _put_record(name: str, state: ServiceState = ServiceState.RUNNING) -> None:
    await server_mod.app.state.store.put(ServiceRecord(name=name, state=state))


def _make_backend(
    *, relocate_result: dict | None = None, relocate_error: Exception | None = None
) -> MagicMock:
    backend = MagicMock()
    backend.stop = AsyncMock(return_value=ServiceState.STOPPED)
    backend.start = AsyncMock(return_value=ServiceState.RUNNING)
    if relocate_error is not None:
        backend.relocate_volume = AsyncMock(side_effect=relocate_error)
    else:
        backend.relocate_volume = AsyncMock(
            return_value=relocate_result or {"status": "ok", "detail": "moved"}
        )
    return backend


def _set_backend(backend: MagicMock) -> None:
    server_mod.app.state.__setattr__("backend", backend)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestRelocateAuth:
    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.post(
            "/volumes/data/relocate", json={"target_disk": "/mnt/data"}
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Validation / not-found
# ---------------------------------------------------------------------------


class TestRelocateValidation:
    async def test_unresolvable_target_disk_returns_400_no_lifecycle(
        self, client: AsyncClient
    ):
        backend = _make_backend()
        _set_backend(backend)

        with patch(
            "robotsix_central_deploy.lifecycle.routers.volumes.resolve_target_disk",
            side_effect=ValueError("not a disk"),
        ):
            resp = await client.post(
                "/volumes/data/relocate",
                json={"target_disk": "not-a-disk"},
                headers=AUTH,
            )

        assert resp.status_code == 400
        backend.stop.assert_not_called()
        backend.start.assert_not_called()
        backend.relocate_volume.assert_not_called()

    async def test_unowned_volume_returns_404(self, client: AsyncClient):
        backend = _make_backend()
        _set_backend(backend)

        with patch(
            "robotsix_central_deploy.lifecycle.routers.volumes.resolve_target_disk",
            return_value="/mnt/data",
        ):
            resp = await client.post(
                "/volumes/ghost/relocate",
                json={"target_disk": "/mnt/data"},
                headers=AUTH,
            )

        assert resp.status_code == 404
        backend.relocate_volume.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path / short-circuit
# ---------------------------------------------------------------------------


class TestRelocateHappyPath:
    async def test_happy_path_persists_target_and_forwards_user(
        self, client: AsyncClient
    ):
        store: ComponentConfigStore = server_mod.app.state.component_config_store
        await _register_component(store, "svc", "data", user="1000:1000")
        await _put_record("svc")
        backend = _make_backend()
        _set_backend(backend)

        with patch(
            "robotsix_central_deploy.lifecycle.routers.volumes.resolve_target_disk",
            return_value="/mnt/data",
        ):
            resp = await client.post(
                "/volumes/data/relocate",
                json={"target_disk": "/mnt/data"},
                headers=AUTH,
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert store.get("svc").target_disk == "/mnt/data"
        backend.relocate_volume.assert_awaited_once_with(
            "data", "/mnt/data", "1000:1000"
        )
        backend.stop.assert_awaited_once()
        backend.start.assert_awaited_once()

    async def test_already_on_target_short_circuits(self, client: AsyncClient):
        store: ComponentConfigStore = server_mod.app.state.component_config_store
        await _register_component(store, "svc", "data", target_disk="/mnt/data")
        backend = _make_backend()
        _set_backend(backend)

        with patch(
            "robotsix_central_deploy.lifecycle.routers.volumes.resolve_target_disk",
            return_value="/mnt/data",
        ):
            resp = await client.post(
                "/volumes/data/relocate",
                json={"target_disk": "/mnt/data"},
                headers=AUTH,
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        backend.stop.assert_not_called()
        backend.start.assert_not_called()
        backend.relocate_volume.assert_not_called()


# ---------------------------------------------------------------------------
# Failure rollback / restart
# ---------------------------------------------------------------------------


class TestRelocateFailureRollback:
    async def test_backend_failure_rolls_back_every_owner_and_restarts_siblings(
        self, client: AsyncClient
    ):
        store: ComponentConfigStore = server_mod.app.state.component_config_store
        await _register_component(
            store,
            "svc-a",
            "data",
            target_disk="/old/disk",
            siblings=[
                ServiceConfig(
                    service_key="db",
                    container_name="svc-a-db",
                    image="db:latest",
                )
            ],
        )
        await _register_component(store, "svc-b", "data", target_disk="/old/disk")
        await _put_record("svc-a")
        await _put_record("svc-a-db")

        backend = _make_backend(relocate_result={"status": "failed", "detail": "boom"})
        _set_backend(backend)

        with patch(
            "robotsix_central_deploy.lifecycle.routers.volumes.resolve_target_disk",
            return_value="/mnt/data",
        ):
            resp = await client.post(
                "/volumes/data/relocate",
                json={"target_disk": "/mnt/data"},
                headers=AUTH,
            )

        assert resp.status_code == 500
        # Every owning component's target_disk is rolled back.
        assert store.get("svc-a").target_disk == "/old/disk"
        assert store.get("svc-b").target_disk == "/old/disk"
        # Primary + sibling were both stopped before the attempt and restarted
        # after the failure.
        assert backend.stop.await_count == 2
        assert backend.start.await_count == 2
        backend.relocate_volume.assert_awaited_once_with("data", "/mnt/data", None)
