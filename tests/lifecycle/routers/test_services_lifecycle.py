"""Integration tests for service lifecycle endpoints (start / stop / restart)."""

from __future__ import annotations

from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.models import (
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    ServiceConfig,
)


async def _seed_store(*names: str, image: str = "") -> None:
    """Populate the server's store with records for testing."""
    s = server_mod.app.state.store
    assert s is not None
    for name in names:
        rec = ServiceRecord(
            name=name, state=ServiceState.STOPPED, image=image or f"{name}:latest"
        )
        await s.put(rec)


# ---------------------------------------------------------------------------
# Helpers for sibling tests
# ---------------------------------------------------------------------------


async def _register_sibling_component(
    primary_name: str = "svc-a",
    sibling_service_key: str = "redis",
    sibling_container_name: str = "svc-a-redis",
    sibling_image: str = "redis:7",
) -> ComponentConfig:
    """Create and register a ComponentConfig with one sibling ServiceConfig."""
    config_store = server_mod.app.state.component_config_store
    sibling = ServiceConfig(
        service_key=sibling_service_key,
        container_name=sibling_container_name,
        image=sibling_image,
    )
    cfg = ComponentConfig(
        id=primary_name,
        image=f"{primary_name}:latest",
        container_name=primary_name,
        siblings=[sibling],
    )
    await config_store.put(cfg)
    server_mod.app.state.registry.register(cfg)
    return cfg


async def _seed_primary_and_sibling(
    primary_name: str = "svc-a",
    sibling_name: str = "svc-a-redis",
    sibling_image: str = "redis:7",
) -> None:
    """Seed both primary and sibling ServiceRecords in the store."""
    store = server_mod.app.state.store
    prim = ServiceRecord(
        name=primary_name,
        state=ServiceState.STOPPED,
        image=f"{primary_name}:latest",
    )
    sib = ServiceRecord(
        name=sibling_name,
        state=ServiceState.STOPPED,
        image=sibling_image,
    )
    await store.put(prim)
    await store.put(sib)


# ---------------------------------------------------------------------------
# TestStart
# ---------------------------------------------------------------------------


class TestStart:
    async def test_start_stopped_service(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        resp = await client.post("/services/svc-a/start", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "start"
        assert data["previous_state"] == ServiceState.STOPPED.value
        assert data["current_state"] == ServiceState.RUNNING.value

    async def test_start_already_running_is_idempotent(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.RUNNING
        await s.put(rec)

        resp = await client.post("/services/svc-a/start", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_state"] == ServiceState.RUNNING.value
        assert "already running" in data["detail"]

    async def test_start_already_starting_is_idempotent(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.STARTING
        await s.put(rec)

        resp = await client.post("/services/svc-a/start", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_state"] == ServiceState.STARTING.value
        assert "already in progress" in data["detail"]

    async def test_start_not_found(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/services/nonexistent/start", headers=auth_headers)
        assert resp.status_code == 404

    async def test_start_from_failed_state(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.FAILED
        await s.put(rec)

        resp = await client.post("/services/svc-a/start", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["previous_state"] == ServiceState.FAILED.value
        assert data["current_state"] == ServiceState.RUNNING.value

    async def test_start_conflict_from_stopping(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.STOPPING
        await s.put(rec)

        resp = await client.post("/services/svc-a/start", headers=auth_headers)
        assert resp.status_code == 409

    async def test_start_backend_failure(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("svc-a")

        async def _failing_start(service):
            raise RuntimeError("docker daemon unreachable")

        monkeypatch.setattr(server_mod.app.state.backend, "start", _failing_start)

        resp = await client.post("/services/svc-a/start", headers=auth_headers)
        assert resp.status_code == 500
        data = resp.json()
        assert "Start failed" in data["error"]

        # Verify the record is marked FAILED in the store.
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        assert rec is not None
        assert rec.state == ServiceState.FAILED
        assert "docker daemon unreachable" in rec.last_error

    async def test_start_with_sibling(self, client: AsyncClient, auth_headers: dict):
        await _register_sibling_component()
        await _seed_primary_and_sibling()

        resp = await client.post("/services/svc-a/start", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "start"
        assert data["current_state"] == ServiceState.RUNNING.value

        # Verify both primary and sibling are RUNNING.
        store = server_mod.app.state.store
        prim = await store.get("svc-a")
        sib = await store.get("svc-a-redis")
        assert prim is not None
        assert sib is not None
        assert prim.state == ServiceState.RUNNING
        assert sib.state == ServiceState.RUNNING


# ---------------------------------------------------------------------------
# TestStop
# ---------------------------------------------------------------------------


class TestStop:
    async def test_stop_running_service(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.RUNNING
        await s.put(rec)

        resp = await client.post("/services/svc-a/stop", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "stop"
        assert data["previous_state"] == ServiceState.RUNNING.value
        assert data["current_state"] == ServiceState.STOPPED.value

    async def test_stop_already_stopped_is_idempotent(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        resp = await client.post("/services/svc-a/stop", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_state"] == ServiceState.STOPPED.value
        assert "already stopped" in data["detail"]

    async def test_stop_already_stopping_is_idempotent(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.STOPPING
        await s.put(rec)

        resp = await client.post("/services/svc-a/stop", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_state"] == ServiceState.STOPPING.value
        assert "already in progress" in data["detail"]

    async def test_stop_not_found(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/services/nonexistent/stop", headers=auth_headers)
        assert resp.status_code == 404

    async def test_stop_conflict_from_starting(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.STARTING
        await s.put(rec)

        resp = await client.post("/services/svc-a/stop", headers=auth_headers)
        assert resp.status_code == 409

    async def test_stop_backend_failure(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.RUNNING
        await s.put(rec)

        async def _failing_stop(service):
            raise RuntimeError("docker daemon unreachable")

        monkeypatch.setattr(server_mod.app.state.backend, "stop", _failing_stop)

        resp = await client.post("/services/svc-a/stop", headers=auth_headers)
        assert resp.status_code == 500
        data = resp.json()
        assert "Stop failed" in data["error"]

        # Verify the record is marked FAILED in the store.
        rec = await s.get("svc-a")
        assert rec is not None
        assert rec.state == ServiceState.FAILED
        assert "docker daemon unreachable" in rec.last_error

    async def test_stop_with_sibling(self, client: AsyncClient, auth_headers: dict):
        await _register_sibling_component()
        await _seed_primary_and_sibling()

        # Set both to RUNNING so stop is valid.
        store = server_mod.app.state.store
        prim = await store.get("svc-a")
        sib = await store.get("svc-a-redis")
        prim.state = ServiceState.RUNNING
        sib.state = ServiceState.RUNNING
        await store.put(prim)
        await store.put(sib)

        resp = await client.post("/services/svc-a/stop", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "stop"
        assert data["current_state"] == ServiceState.STOPPED.value

        # Verify both primary and sibling are STOPPED.
        prim = await store.get("svc-a")
        sib = await store.get("svc-a-redis")
        assert prim is not None
        assert sib is not None
        assert prim.state == ServiceState.STOPPED
        assert sib.state == ServiceState.STOPPED


# ---------------------------------------------------------------------------
# TestRestart
# ---------------------------------------------------------------------------


class TestRestart:
    async def test_restart_running_service(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.RUNNING
        await s.put(rec)

        resp = await client.post("/services/svc-a/restart", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "restart"
        assert data["previous_state"] == ServiceState.RUNNING.value
        assert data["current_state"] == ServiceState.RUNNING.value

    async def test_restart_already_restarting_is_idempotent(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.RESTARTING
        await s.put(rec)

        resp = await client.post("/services/svc-a/restart", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_state"] == ServiceState.RESTARTING.value
        assert "already in progress" in data["detail"]

    async def test_restart_not_found(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/services/nonexistent/restart", headers=auth_headers)
        assert resp.status_code == 404

    async def test_restart_conflict_from_stopped(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        resp = await client.post("/services/svc-a/restart", headers=auth_headers)
        assert resp.status_code == 409

    async def test_restart_backend_failure(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("svc-a")
        s = server_mod.app.state.store
        rec = await s.get("svc-a")
        rec.state = ServiceState.RUNNING
        await s.put(rec)

        async def _failing_restart(service):
            raise RuntimeError("docker daemon unreachable")

        monkeypatch.setattr(server_mod.app.state.backend, "restart", _failing_restart)

        resp = await client.post("/services/svc-a/restart", headers=auth_headers)
        assert resp.status_code == 500
        data = resp.json()
        assert "Restart failed" in data["error"]

        # Verify the record is marked FAILED in the store.
        rec = await s.get("svc-a")
        assert rec is not None
        assert rec.state == ServiceState.FAILED
        assert "docker daemon unreachable" in rec.last_error

    async def test_restart_with_sibling(self, client: AsyncClient, auth_headers: dict):
        await _register_sibling_component()
        await _seed_primary_and_sibling()

        # Set both to RUNNING so restart is valid.
        store = server_mod.app.state.store
        prim = await store.get("svc-a")
        sib = await store.get("svc-a-redis")
        prim.state = ServiceState.RUNNING
        sib.state = ServiceState.RUNNING
        await store.put(prim)
        await store.put(sib)

        resp = await client.post("/services/svc-a/restart", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "restart"
        assert data["current_state"] == ServiceState.RUNNING.value

        # Verify both primary and sibling end up RUNNING.
        prim = await store.get("svc-a")
        sib = await store.get("svc-a-redis")
        assert prim is not None
        assert sib is not None
        assert prim.state == ServiceState.RUNNING
        assert sib.state == ServiceState.RUNNING
