"""Integration tests for service management endpoints."""

from __future__ import annotations


from httpx import AsyncClient

from unittest.mock import AsyncMock, MagicMock

from robotsix_central_deploy.lifecycle.models import (
    ComponentInspect,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.config_yaml_store import ConfigYamlStore
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    VolumeMount,
)

# Import the server module itself (not just symbols) so we can set its globals.
from robotsix_central_deploy.lifecycle import server as server_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_store(*names: str, image: str = "", deployed_digest: str = "") -> None:
    """Populate the server's store with records for testing."""
    s = server_mod.app.state.store
    assert s is not None
    for name in names:
        rec = ServiceRecord(
            name=name, state=ServiceState.STOPPED, image=image or f"{name}:latest"
        )
        if deployed_digest:
            rec.deployed_image_digest = deployed_digest
        await s.put(rec)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------


class TestListServices:
    async def test_empty_list(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/services", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"services": []}

    async def test_list_with_items(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a", "svc-b")
        resp = await client.get("/services", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        names = {s["name"] for s in data["services"]}
        assert names == {"svc-a", "svc-b"}
        for s in data["services"]:
            assert s["state"] in {e.value for e in ServiceState}


# ---------------------------------------------------------------------------
# GET /services/{name}
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------


class TestGetServiceHealth:
    async def test_not_found(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/services/nonexistent/health", headers=auth_headers)
        assert resp.status_code == 404

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        await _seed_store("svc-a")
        resp = await client.get("/services/svc-a/health")
        assert resp.status_code == 401

    async def test_no_health_check_returns_unknown(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        # NoopBackend.status returns ComponentInspect with health="" by default
        resp = await client.get("/services/svc-a/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"name": "svc-a", "health": "unknown"}

    async def test_healthy_container(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("svc-a")
        inspect = ComponentInspect(state=ServiceState.RUNNING, health="healthy")
        mock_status = AsyncMock(return_value=inspect)
        monkeypatch.setattr(server_mod.app.state.backend, "status", mock_status)

        resp = await client.get("/services/svc-a/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"name": "svc-a", "health": "healthy"}

    async def test_unhealthy_container(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("svc-a")
        inspect = ComponentInspect(state=ServiceState.RUNNING, health="unhealthy")
        mock_status = AsyncMock(return_value=inspect)
        monkeypatch.setattr(server_mod.app.state.backend, "status", mock_status)

        resp = await client.get("/services/svc-a/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"name": "svc-a", "health": "unhealthy"}


# ---------------------------------------------------------------------------
# POST /services/{name}/start
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# POST /services/{name}/stop
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# POST /services/{name}/restart
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# GET /services/{name}/logs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------


class TestLogsEndpoint:
    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.get("/services/svc-a/logs")
        assert resp.status_code == 401

    async def test_unknown_service_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get("/services/nonexistent/logs", headers=auth_headers)
        assert resp.status_code == 404

    async def test_noop_backend_returns_stub_body(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        resp = await client.get("/services/svc-a/logs", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        body = resp.content
        assert b"[noop backend]\n" in body

    async def test_query_params_forwarded_to_backend(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")

        captured: list[dict] = []

        async def _fake_stream(service, tail=100, since=None, follow=False):
            captured.append({"tail": tail, "since": since, "follow": follow})
            yield f"tail={tail} since={since}".encode()

        original = server_mod.app.state.backend.stream_logs
        server_mod.app.state.backend.stream_logs = _fake_stream
        try:
            resp = await client.get(
                "/services/svc-a/logs?tail=50&since=1700000000",
                headers=auth_headers,
            )
            assert resp.status_code == 200
            assert b"tail=50 since=1700000000" in resp.content
            assert len(captured) == 1
            assert captured[0] == {"tail": 50, "since": "1700000000", "follow": False}
        finally:
            server_mod.app.state.backend.stream_logs = original

    async def test_follow_param_forwarded_to_backend(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        captured: list[dict] = []

        async def _fake_stream(service, tail=100, since=None, follow=False):
            captured.append({"tail": tail, "since": since, "follow": follow})
            yield b"live line\n"

        original = server_mod.app.state.backend.stream_logs
        server_mod.app.state.backend.stream_logs = _fake_stream
        try:
            resp = await client.get(
                "/services/svc-a/logs?follow=true",
                headers=auth_headers,
            )
            assert resp.status_code == 200
            assert len(captured) == 1
            assert captured[0]["follow"] is True
        finally:
            server_mod.app.state.backend.stream_logs = original

    async def test_tail_out_of_range_returns_422(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        resp = await client.get("/services/svc-a/logs?tail=0", headers=auth_headers)
        assert resp.status_code == 422
        resp2 = await client.get(
            "/services/svc-a/logs?tail=10001", headers=auth_headers
        )
        assert resp2.status_code == 422


# ---------------------------------------------------------------------------
# Update available (registry check)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------


class TestUpdateAvailable:
    async def test_up_to_date_when_digests_match(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store(
            "svc-a", image="ghcr.io/o/img:main", deployed_digest="sha256:aaa"
        )
        mock_checker = MagicMock()
        mock_checker.get_latest_digest = AsyncMock(return_value="sha256:aaa")
        monkeypatch.setattr(server_mod.app.state, "registry_checker", mock_checker)
        resp = await client.get("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["update_available"] is False
        assert data["update_state"] == "up-to-date"
        assert data["running_digest"] == "sha256:aaa"
        assert data["latest_digest"] == "sha256:aaa"

    async def test_update_available_when_digests_differ(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store(
            "svc-a", image="ghcr.io/o/img:main", deployed_digest="sha256:aaa"
        )
        mock_checker = MagicMock()
        mock_checker.get_latest_digest = AsyncMock(return_value="sha256:bbb")
        monkeypatch.setattr(server_mod.app.state, "registry_checker", mock_checker)
        resp = await client.get("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["update_available"] is True
        assert data["update_state"] == "update-available"
        assert data["running_digest"] == "sha256:aaa"
        assert data["latest_digest"] == "sha256:bbb"

    async def test_registry_unreachable_degrades_gracefully(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store(
            "svc-a", image="ghcr.io/o/img:main", deployed_digest="sha256:aaa"
        )
        mock_checker = MagicMock()
        mock_checker.get_latest_digest = AsyncMock(return_value=None)
        monkeypatch.setattr(server_mod.app.state, "registry_checker", mock_checker)
        resp = await client.get("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["update_available"] is False
        assert data["update_state"] == "unknown"
        assert data["latest_digest"] == ""

    async def test_list_returns_update_available_field(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("svc-a")
        resp = await client.get("/services", headers=auth_headers)
        assert resp.status_code == 200
        items = resp.json()["services"]
        assert all("update_available" in item for item in items)

    async def test_running_digest_populated_from_inspect(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("svc-a", image="ghcr.io/o/img:main", deployed_digest="")
        inspect = ComponentInspect(
            state=ServiceState.RUNNING, running_digest="sha256:e9f0"
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "status",
            AsyncMock(return_value=inspect),
        )
        resp = await client.get("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["running_digest"] == "sha256:e9f0"
        assert data["update_state"] == "unknown"  # latest not yet fetched


# ---------------------------------------------------------------------------
# GET / PUT / DELETE /services/{name}/env
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# DELETE /services/{name}
# ---------------------------------------------------------------------------


async def _seed_config(
    config_store: ComponentConfigStore, name: str, *, siblings: list = None
) -> ComponentConfig:
    """Create and persist a ComponentConfig in the config store, plus register it."""
    cfg = ComponentConfig(
        id=name,
        image=f"{name}:latest",
        container_name=name,
        siblings=siblings or [],
    )
    await config_store.put(cfg)
    server_mod.app.state.registry.register(cfg)
    return cfg


class TestDeleteService:
    async def test_nonexistent_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.delete("/services/nonexistent", headers=auth_headers)
        assert resp.status_code == 404

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.delete("/services/svc-a")
        assert resp.status_code == 401

    async def test_delete_existing_returns_204(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")
        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204

    async def test_after_delete_service_list_excludes_name(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")
        await client.delete("/services/svc-a", headers=auth_headers)

        resp = await client.get("/services", headers=auth_headers)
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()["services"]}
        assert "svc-a" not in names

    async def test_after_delete_get_service_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")
        await client.delete("/services/svc-a", headers=auth_headers)

        resp = await client.get("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 404

    async def test_after_delete_env_store_cleared(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        env_store = server_mod.app.state.env_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")

        # Put some env data
        await env_store.upsert("svc-a", {"FOO": "bar"}, {})

        await client.delete("/services/svc-a", headers=auth_headers)

        # Env should be empty
        cfg = await env_store.get("svc-a")
        assert cfg.env == {}
        assert cfg.secret_tokens == {}

    async def test_stop_container_false_backend_not_called(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")

        stop_called = False
        remove_called = False
        original_stop = server_mod.app.state.backend.stop
        original_remove = server_mod.app.state.backend.remove_container

        async def _fake_stop(service):
            nonlocal stop_called
            stop_called = True
            return await original_stop(service)

        async def _fake_remove(service):
            nonlocal remove_called
            remove_called = True
            return await original_remove(service)

        monkeypatch.setattr(server_mod.app.state.backend, "stop", _fake_stop)
        monkeypatch.setattr(
            server_mod.app.state.backend, "remove_container", _fake_remove
        )

        resp = await client.delete(
            "/services/svc-a?stop_container=false", headers=auth_headers
        )
        assert resp.status_code == 204
        assert not stop_called
        assert not remove_called

    async def test_stop_container_true_calls_backend(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")

        stop_called = False
        remove_called = False
        original_stop = server_mod.app.state.backend.stop
        original_remove = server_mod.app.state.backend.remove_container

        async def _fake_stop(service):
            nonlocal stop_called
            stop_called = True
            return await original_stop(service)

        async def _fake_remove(service):
            nonlocal remove_called
            remove_called = True
            return await original_remove(service)

        monkeypatch.setattr(server_mod.app.state.backend, "stop", _fake_stop)
        monkeypatch.setattr(
            server_mod.app.state.backend, "remove_container", _fake_remove
        )

        # Default stop_container=true
        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204
        assert stop_called
        assert remove_called

    async def test_backend_stop_error_does_not_abort(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")

        async def _failing_stop(service):
            raise RuntimeError("docker daemon down")

        monkeypatch.setattr(server_mod.app.state.backend, "stop", _failing_stop)

        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204

    async def test_delete_with_sibling(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        from robotsix_central_deploy.registry.models import ServiceConfig

        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store
        env_store = server_mod.app.state.env_store

        # Create a component with a sibling
        sibling = ServiceConfig(
            service_key="redis",
            container_name="svc-a-redis",
            image="redis:7",
        )
        cfg = ComponentConfig(
            id="svc-a",
            image="svc-a:latest",
            container_name="svc-a",
            siblings=[sibling],
        )
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        # Seed primary and sibling records
        prim = ServiceRecord(name="svc-a", image="svc-a:latest")
        sib_rec = ServiceRecord(name="svc-a-redis", image="redis:7")
        await store.put(prim)
        await store.put(sib_rec)

        # Put env for both
        await env_store.upsert("svc-a", {"PRIMARY": "1"}, {})
        await env_store.upsert("svc-a-redis", {"SIBLING": "1"}, {})

        # Track backend calls
        stop_names: list[str] = []
        remove_names: list[str] = []
        original_stop = server_mod.app.state.backend.stop
        original_remove = server_mod.app.state.backend.remove_container

        async def _fake_stop(service):
            stop_names.append(service.name)
            return await original_stop(service)

        async def _fake_remove(service):
            remove_names.append(service.name)
            return await original_remove(service)

        monkeypatch.setattr(server_mod.app.state.backend, "stop", _fake_stop)
        monkeypatch.setattr(
            server_mod.app.state.backend, "remove_container", _fake_remove
        )

        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204

        # Both containers were stopped and removed
        assert "svc-a" in stop_names
        assert "svc-a-redis" in stop_names
        assert "svc-a" in remove_names
        assert "svc-a-redis" in remove_names

        # Both records are gone
        assert await store.get("svc-a") is None
        assert await store.get("svc-a-redis") is None

        # Both env entries are cleared
        prim_env = await env_store.get("svc-a")
        sib_env = await env_store.get("svc-a-redis")
        assert prim_env.env == {}
        assert sib_env.env == {}

        # Config is gone
        assert config_store.get("svc-a") is None

        # Registry entry is gone
        assert server_mod.app.state.registry.get("svc-a") is None

    async def test_remove_volumes_default_false_never_removes(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """Default delete (remove_volumes omitted) must never touch volumes."""
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="svc-a",
            image="svc-a:latest",
            container_name="svc-a",
            named_volumes=["svc-a-data"],
        )
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)
        await _seed_store("svc-a", image="svc-a:latest")

        removed: list[str] = []

        async def _fake_remove_volume(volume_name):
            removed.append(volume_name)

        monkeypatch.setattr(
            server_mod.app.state.backend, "remove_volume", _fake_remove_volume
        )

        # No remove_volumes query param → default false.
        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204
        assert removed == []

    async def test_remove_volumes_true_removes_primary_and_siblings_deduped(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """remove_volumes=true removes each unique volume once (primary + siblings)."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store

        # Sibling declares a mount whose host is a named volume ("sib-data")
        # plus a duplicate of the primary's volume ("shared-data") to exercise
        # de-duplication.
        sibling = ServiceConfig(
            service_key="redis",
            container_name="svc-a-redis",
            image="redis:7",
            mounts=[
                VolumeMount(host="sib-data", container="/data"),
                VolumeMount(host="shared-data", container="/shared"),
            ],
        )
        cfg = ComponentConfig(
            id="svc-a",
            image="svc-a:latest",
            container_name="svc-a",
            named_volumes=["prim-data", "shared-data"],
            siblings=[sibling],
        )
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)
        await store.put(ServiceRecord(name="svc-a", image="svc-a:latest"))
        await store.put(ServiceRecord(name="svc-a-redis", image="redis:7"))

        removed: list[str] = []

        async def _fake_remove_volume(volume_name):
            removed.append(volume_name)

        monkeypatch.setattr(
            server_mod.app.state.backend, "remove_volume", _fake_remove_volume
        )

        resp = await client.delete(
            "/services/svc-a?remove_volumes=true", headers=auth_headers
        )
        assert resp.status_code == 204

        # Each unique volume removed exactly once; "shared-data" de-duped.
        assert sorted(removed) == ["prim-data", "shared-data", "sib-data"]
        assert len(removed) == len(set(removed))

        # Records still deleted alongside the volumes.
        assert await store.get("svc-a") is None
        assert await store.get("svc-a-redis") is None

    async def test_remove_volume_error_does_not_abort_delete(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """A remove_volume that raises must not abort the delete (still 204)."""
        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store
        cfg = ComponentConfig(
            id="svc-a",
            image="svc-a:latest",
            container_name="svc-a",
            named_volumes=["svc-a-data"],
        )
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)
        await store.put(ServiceRecord(name="svc-a", image="svc-a:latest"))

        async def _failing_remove_volume(volume_name):
            raise RuntimeError("volume in use / NotFound")

        monkeypatch.setattr(
            server_mod.app.state.backend, "remove_volume", _failing_remove_volume
        )

        resp = await client.delete(
            "/services/svc-a?remove_volumes=true", headers=auth_headers
        )
        assert resp.status_code == 204

        # Records were still deleted despite the volume-removal failure.
        assert await store.get("svc-a") is None
        assert config_store.get("svc-a") is None

    async def test_delete_missing_config_still_purges_service_record(
        self, client: AsyncClient, auth_headers: dict
    ):
        """DELETE succeeds and purges the ServiceRecord even when the
        component config entry is already absent (no 404-abort)."""
        store = server_mod.app.state.store
        env_store = server_mod.app.state.env_store
        config_yaml_store: ConfigYamlStore = server_mod.app.state.config_yaml_store

        # Seed a service record and env WITHOUT a component config
        prim = ServiceRecord(name="orphan", image="orphan:latest")
        await store.put(prim)
        await env_store.upsert("orphan", {"KEY": "val"}, {"SECRET": "tok"})
        await config_yaml_store.save_template("orphan", {"type": "object"})
        await config_yaml_store.update_current("orphan", {"foo": "bar"})

        resp = await client.delete("/services/orphan", headers=auth_headers)
        assert resp.status_code == 204

        # ServiceRecord is gone
        assert await store.get("orphan") is None

        # Env is cleared
        cfg = await env_store.get("orphan")
        assert cfg.env == {}
        assert cfg.secret_tokens == {}

        # Config YAML is cleared
        assert await config_yaml_store.get_template("orphan") is None
        assert await config_yaml_store.get_current("orphan") is None

    async def test_delete_clears_config_yaml_store(
        self, client: AsyncClient, auth_headers: dict
    ):
        """DELETE clears the config_yaml_store current + template for the component."""
        config_store = server_mod.app.state.component_config_store
        config_yaml_store: ConfigYamlStore = server_mod.app.state.config_yaml_store

        await _seed_config(config_store, "svc-a")
        await _seed_store("svc-a", image="svc-a:latest")
        await config_yaml_store.save_template("svc-a", {"type": "object"})
        await config_yaml_store.update_current("svc-a", {"host": "example.com"})

        resp = await client.delete("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 204

        # Both template and current are gone
        assert await config_yaml_store.get_template("svc-a") is None
        assert await config_yaml_store.get_current("svc-a") is None

    async def test_delete_with_socket_proxy_helper(
        self, client: AsyncClient, auth_headers: dict
    ):
        """DELETE tears down helper services (e.g. <name>-socket-proxy) alongside
        the primary, even when discovered by prefix scan (config absent)."""
        store = server_mod.app.state.store
        env_store = server_mod.app.state.env_store

        # Seed primary + a socket-proxy helper service record (no config)
        prim = ServiceRecord(name="mill", image="mill:latest")
        helper = ServiceRecord(name="mill-socket-proxy", image="socket-proxy:latest")
        await store.put(prim)
        await store.put(helper)
        await env_store.upsert("mill", {"PRIMARY": "1"}, {})
        await env_store.upsert("mill-socket-proxy", {"HELPER": "1"}, {})

        resp = await client.delete("/services/mill", headers=auth_headers)
        assert resp.status_code == 204

        # Both primary and helper are gone
        assert await store.get("mill") is None
        assert await store.get("mill-socket-proxy") is None

        # Both env entries are cleared
        prim_env = await env_store.get("mill")
        helper_env = await env_store.get("mill-socket-proxy")
        assert prim_env.env == {}
        assert helper_env.env == {}


# ---------------------------------------------------------------------------
# Lifecycle sibling fan-out
# ---------------------------------------------------------------------------
