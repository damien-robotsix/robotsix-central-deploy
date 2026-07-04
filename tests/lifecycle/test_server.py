"""Integration tests for the lifecycle REST server.

Uses ``httpx.AsyncClient`` against a FastAPI test transport so we exercise the
full request/response pipeline including middleware, auth, and error handlers.
"""

from __future__ import annotations

import asyncio
import time

from httpx import AsyncClient

from unittest.mock import AsyncMock, MagicMock

from robotsix_central_deploy.lifecycle.models import (
    ComponentInspect,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.config_yaml_store import ConfigYamlStore
from robotsix_central_deploy.onboard.models import DerivedSpec, SiblingDerivedSpec
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    ConfigAssistSeed,
    PortMapping,
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


class TestHealth:
    async def test_health_returns_ok(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# GET /services
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


class TestGetStatus:
    async def test_not_found(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/services/nonexistent", headers=auth_headers)
        assert resp.status_code == 404

    async def test_returns_status(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        resp = await client.get("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "svc-a"
        assert data["state"] in {e.value for e in ServiceState}
        assert "image" in data


# ---------------------------------------------------------------------------
# GET /services/{name}/health
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


class TestEnvEndpoints:
    async def test_get_env_empty_for_fresh_component(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        resp = await client.get("/services/chat/env", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "env": {},
            "secrets": {},
            "mem_limit": "2g",
            "allow_chat_access": False,
            "claude_mount": False,
        }

    async def test_put_then_get_returns_env_and_masked_secrets(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        put_body = {"env": {"LOG_LEVEL": "debug"}, "secrets": {"API_KEY": "my-token"}}
        r = await client.put("/services/chat/env", json=put_body, headers=auth_headers)
        assert r.status_code == 204

        r = await client.get("/services/chat/env", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["env"] == {"LOG_LEVEL": "debug"}
        assert data["secrets"] == {"API_KEY": "***"}

    async def test_put_merges_not_replaces(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        await client.put(
            "/services/chat/env", json={"env": {"A": "1"}}, headers=auth_headers
        )
        await client.put(
            "/services/chat/env", json={"env": {"B": "2"}}, headers=auth_headers
        )
        r = await client.get("/services/chat/env", headers=auth_headers)
        data = r.json()
        assert data["env"] == {"A": "1", "B": "2"}

    async def test_get_env_nonexistent_service_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get("/services/nonexistent/env", headers=auth_headers)
        assert resp.status_code == 404

    async def test_unauthenticated_get_returns_401(self, client: AsyncClient):
        await _seed_store("chat")
        resp = await client.get("/services/chat/env")
        assert resp.status_code == 401

    async def test_unauthenticated_put_returns_401(self, client: AsyncClient):
        await _seed_store("chat")
        resp = await client.put("/services/chat/env", json={"env": {"A": "1"}})
        assert resp.status_code == 401

    async def test_unauthenticated_delete_returns_401(self, client: AsyncClient):
        await _seed_store("chat")
        resp = await client.delete("/services/chat/env/A")
        assert resp.status_code == 401

    async def test_delete_key_removes_from_env(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        await client.put(
            "/services/chat/env",
            json={"env": {"A": "1", "B": "2"}},
            headers=auth_headers,
        )
        r = await client.delete("/services/chat/env/A", headers=auth_headers)
        assert r.status_code == 204
        r = await client.get("/services/chat/env", headers=auth_headers)
        data = r.json()
        assert data["env"] == {"B": "2"}

    async def test_delete_key_removes_from_secrets(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        await client.put(
            "/services/chat/env",
            json={"secrets": {"TOKEN": "val"}},
            headers=auth_headers,
        )
        r = await client.delete("/services/chat/env/TOKEN", headers=auth_headers)
        assert r.status_code == 204
        r = await client.get("/services/chat/env", headers=auth_headers)
        data = r.json()
        assert data["secrets"] == {}

    async def test_delete_absent_key_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        await client.put(
            "/services/chat/env", json={"env": {"A": "1"}}, headers=auth_headers
        )
        r = await client.delete("/services/chat/env/NOTFOUND", headers=auth_headers)
        assert r.status_code == 404

    async def test_deploy_injects_merged_env(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """deploy_service must call backend.deploy with merged env including secrets."""
        await _seed_store("chat", image="ghcr.io/o/img:main")

        # Set up a fake registry with a component config that has a base env
        from robotsix_central_deploy.registry.loader import ComponentRegistry

        cfg = ComponentConfig(
            id="chat",
            image="ghcr.io/o/img:main",
            container_name="chat",
            env={"BASE_KEY": "base-val", "OVERRIDE": "base"},
        )
        registry = ComponentRegistry([cfg])
        server_mod.app.state.registry = registry

        # Store a secret and an env override via the API
        await client.put(
            "/services/chat/env",
            json={"env": {"OVERRIDE": "user-val"}, "secrets": {"SECRET": "s3cret"}},
            headers=auth_headers,
        )

        # Monkeypatch backend.deploy to capture the config
        captured_configs: list = []
        original_deploy = server_mod.app.state.backend.deploy

        async def _fake_deploy(service, config, image_ref):
            captured_configs.append(config)
            return await original_deploy(service, config, image_ref)

        monkeypatch.setattr(server_mod.app.state.backend, "deploy", _fake_deploy)

        r = await client.post("/services/chat/deploy", headers=auth_headers)
        assert r.status_code == 202

        # Let the background task run to completion.
        await asyncio.sleep(0)

        assert len(captured_configs) == 1
        deployed_env = captured_configs[0].env
        assert deployed_env == {
            "BASE_KEY": "base-val",
            "OVERRIDE": "user-val",
            "SECRET": "s3cret",
        }

    async def test_put_with_mem_limit_updates_config(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PUT /services/{name}/env with mem_limit persists to ComponentConfig."""
        await _seed_store("chat")
        config_store = server_mod.app.state.component_config_store
        await _seed_config(config_store, "chat")

        r = await client.put(
            "/services/chat/env",
            json={"mem_limit": "4g"},
            headers=auth_headers,
        )
        assert r.status_code == 204

        # Verify the mem_limit was persisted
        r = await client.get("/services/chat/env", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["mem_limit"] == "4g"

    async def test_put_without_mem_limit_preserves_existing(
        self, client: AsyncClient, auth_headers: dict
    ):
        """PUT without mem_limit should not change the existing value."""
        await _seed_store("chat")
        config_store = server_mod.app.state.component_config_store
        cfg = await _seed_config(config_store, "chat")
        cfg.mem_limit = "8g"
        await config_store.put(cfg)

        r = await client.put(
            "/services/chat/env",
            json={"env": {"KEY": "val"}},
            headers=auth_headers,
        )
        assert r.status_code == 204

        r = await client.get("/services/chat/env", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["mem_limit"] == "8g"  # unchanged

    async def test_get_env_returns_stored_mem_limit(
        self, client: AsyncClient, auth_headers: dict
    ):
        """GET /services/{name}/env returns mem_limit from ComponentConfig."""
        await _seed_store("chat")
        config_store = server_mod.app.state.component_config_store
        cfg = await _seed_config(config_store, "chat")
        cfg.mem_limit = "512m"
        await config_store.put(cfg)

        r = await client.get("/services/chat/env", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["mem_limit"] == "512m"


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


class TestStartWithSibling:
    async def test_start_propagates_to_sibling(
        self, client: AsyncClient, auth_headers: dict
    ):
        """start_service fans out to sibling records — both transition STOPPED→RUNNING."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store

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

        prim = ServiceRecord(
            name="svc-a", image="svc-a:latest", state=ServiceState.STOPPED
        )
        sib_rec = ServiceRecord(
            name="svc-a-redis", image="redis:7", state=ServiceState.STOPPED
        )
        await store.put(prim)
        await store.put(sib_rec)

        resp = await client.post("/services/svc-a/start", headers=auth_headers)
        assert resp.status_code == 200

        # Primary transitioned
        prim_after = await store.get("svc-a")
        assert prim_after.state == ServiceState.RUNNING

        # Sibling transitioned
        sib_after = await store.get("svc-a-redis")
        assert sib_after.state == ServiceState.RUNNING


class TestStopWithSibling:
    async def test_stop_propagates_to_sibling(
        self, client: AsyncClient, auth_headers: dict
    ):
        """stop_service fans out to sibling records — both transition RUNNING→STOPPED."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store

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

        prim = ServiceRecord(
            name="svc-a", image="svc-a:latest", state=ServiceState.RUNNING
        )
        sib_rec = ServiceRecord(
            name="svc-a-redis", image="redis:7", state=ServiceState.RUNNING
        )
        await store.put(prim)
        await store.put(sib_rec)

        resp = await client.post("/services/svc-a/stop", headers=auth_headers)
        assert resp.status_code == 200

        prim_after = await store.get("svc-a")
        assert prim_after.state == ServiceState.STOPPED

        sib_after = await store.get("svc-a-redis")
        assert sib_after.state == ServiceState.STOPPED


class TestRestartWithSibling:
    async def test_restart_propagates_to_sibling(
        self, client: AsyncClient, auth_headers: dict
    ):
        """restart_service fans out to sibling records — both stay RUNNING after restart."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store

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

        prim = ServiceRecord(
            name="svc-a", image="svc-a:latest", state=ServiceState.RUNNING
        )
        sib_rec = ServiceRecord(
            name="svc-a-redis", image="redis:7", state=ServiceState.RUNNING
        )
        await store.put(prim)
        await store.put(sib_rec)

        resp = await client.post("/services/svc-a/restart", headers=auth_headers)
        assert resp.status_code == 200

        prim_after = await store.get("svc-a")
        assert prim_after.state == ServiceState.RUNNING

        sib_after = await store.get("svc-a-redis")
        assert sib_after.state == ServiceState.RUNNING


class TestDeployWithSibling:
    async def test_deploy_propagates_to_sibling(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """deploy_service fans out to siblings — backend.deploy called for both primary and sibling."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store

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

        prim = ServiceRecord(name="svc-a", image="svc-a:latest")
        sib_rec = ServiceRecord(name="svc-a-redis", image="redis:7")
        await store.put(prim)
        await store.put(sib_rec)

        # Capture backend.deploy calls
        deploy_names: list[str] = []
        original_deploy = server_mod.app.state.backend.deploy

        async def _fake_deploy(service, config, image_ref):
            deploy_names.append(service.name)
            return await original_deploy(service, config, image_ref)

        monkeypatch.setattr(server_mod.app.state.backend, "deploy", _fake_deploy)

        resp = await client.post("/services/svc-a/deploy", headers=auth_headers)
        assert resp.status_code == 202

        # Let the background task run to completion.
        await asyncio.sleep(0)

        # Both primary and sibling were deployed
        assert "svc-a" in deploy_names
        assert "svc-a-redis" in deploy_names

        # Sibling record updated with deploy outcome
        sib_after = await store.get("svc-a-redis")
        assert sib_after.state == ServiceState.RUNNING
        assert sib_after.image == "redis:7"
        assert sib_after.deployed_image_digest == "sha256:noop"


class TestRollbackWithSibling:
    async def test_rollback_propagates_to_sibling(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """rollback_service fans out to siblings — backend.rollback called, digests swapped."""
        from robotsix_central_deploy.registry.models import ServiceConfig

        config_store = server_mod.app.state.component_config_store
        store = server_mod.app.state.store

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

        prim = ServiceRecord(
            name="svc-a",
            image="svc-a:latest",
            deployed_image_digest="sha256:current",
            previous_image_digest="sha256:prior",
        )
        sib_rec = ServiceRecord(
            name="svc-a-redis",
            image="redis:7",
            deployed_image_digest="sha256:sib-current",
            previous_image_digest="sha256:sib-prior",
        )
        await store.put(prim)
        await store.put(sib_rec)

        # Capture backend.rollback calls
        rollback_names: list[str] = []
        original_rollback = server_mod.app.state.backend.rollback

        async def _fake_rollback(service, config):
            rollback_names.append(service.name)
            return await original_rollback(service, config)

        monkeypatch.setattr(server_mod.app.state.backend, "rollback", _fake_rollback)

        resp = await client.post("/services/svc-a/rollback", headers=auth_headers)
        assert resp.status_code == 200

        # Both primary and sibling were rolled back
        assert "svc-a" in rollback_names
        assert "svc-a-redis" in rollback_names

        # Sibling digests swapped
        sib_after = await store.get("svc-a-redis")
        assert sib_after.state == ServiceState.RUNNING
        assert sib_after.deployed_image_digest == "sha256:sib-prior"
        assert sib_after.previous_image_digest == "sha256:sib-current"
        assert sib_after.image_revision == "sha256:sib-prior"


# ---------------------------------------------------------------------------
# GET /services/{name}/config
# ---------------------------------------------------------------------------


class TestGetServiceConfig:
    async def test_returns_schema_and_masked_current(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "password": {"type": "string", "format": "password", "writeOnly": True},
            },
        }
        await store.save_template("chat", schema)
        await store.update_current("chat", {"host": "0.0.0.0", "password": "realpass"})

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "schema" in data
        assert data["schema"] == schema
        assert "current" in data
        assert data["current"]["host"] == "0.0.0.0"
        assert data["current"]["password"] == "***"

    async def test_returns_template_as_current_when_no_current_stored(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
                "port": {"type": "integer", "default": 8080},
            },
        }
        await store.save_template("chat", template)

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["schema"] == template
        # No current stored — current is masked template (with defaults)
        assert data["current"]["host"] == "localhost"
        assert data["current"]["port"] == 8080

    async def test_no_config_schema_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        # No template saved for "chat"

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 404
        assert "No config schema" in resp.json()["error"]

    async def test_nonexistent_service_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get("/services/nonexistent/config", headers=auth_headers)
        assert resp.status_code == 404
        # Service not found takes priority
        assert "not found" in resp.json()["error"].lower()

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.get("/services/chat/config")
        assert resp.status_code == 401

    async def test_nested_secrets_are_masked(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        schema = {
            "type": "object",
            "properties": {
                "server": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "password": {
                            "type": "string",
                            "format": "password",
                            "writeOnly": True,
                        },
                    },
                },
            },
        }
        await store.save_template("chat", schema)
        await store.update_current(
            "chat", {"server": {"host": "0.0.0.0", "password": "s3cret"}}
        )

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current"]["server"]["host"] == "0.0.0.0"
        assert data["current"]["server"]["password"] == "***"

    async def test_null_template_secret_is_masked(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        schema = {
            "type": "object",
            "properties": {
                "api_key": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                },
            },
        }
        await store.save_template("chat", schema)
        await store.update_current("chat", {"api_key": "real-key"})

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current"]["api_key"] == "***"


# ---------------------------------------------------------------------------
# PUT /services/{name}/config
# ---------------------------------------------------------------------------


class TestPutServiceConfig:
    async def test_merge_and_return_204(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
                "port": {"type": "integer", "default": 8080},
            },
        }
        await store.save_template("chat", template)

        resp = await client.put(
            "/services/chat/config",
            json={"values": {"host": "10.0.0.1", "port": 3000}},
            headers=auth_headers,
        )
        assert resp.status_code == 204

        current = await store.get_current("chat")
        assert current == {"host": "10.0.0.1", "port": 3000}

    async def test_preserves_masked_secret(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "password": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                },
            },
        }
        await store.save_template("chat", schema)
        await store.update_current(
            "chat", {"host": "localhost", "password": "realpass"}
        )

        # Submit "***" for the secret — existing value should be preserved
        resp = await client.put(
            "/services/chat/config",
            json={"values": {"host": "10.0.0.1", "password": "***"}},
            headers=auth_headers,
        )
        assert resp.status_code == 204

        current = await store.get_current("chat")
        assert current == {"host": "10.0.0.1", "password": "realpass"}

    async def test_replaces_secret_with_new_value(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        schema = {
            "type": "object",
            "properties": {
                "password": {"type": "string"},
            },
        }
        await store.save_template("chat", schema)
        await store.update_current("chat", {"password": "oldpass"})

        resp = await client.put(
            "/services/chat/config",
            json={"values": {"password": "newpass"}},
            headers=auth_headers,
        )
        assert resp.status_code == 204

        current = await store.get_current("chat")
        assert current == {"password": "newpass"}

    async def test_no_config_schema_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        # No template saved

        resp = await client.put(
            "/services/chat/config",
            json={"values": {"key": "val"}},
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert "No config schema" in resp.json()["error"]

    async def test_nonexistent_service_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.put(
            "/services/nonexistent/config",
            json={"values": {"key": "val"}},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.put("/services/chat/config", json={"values": {}})
        assert resp.status_code == 401

    async def test_merge_nested_config(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        schema = {
            "type": "object",
            "properties": {
                "server": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "port": {"type": "integer", "default": 8080},
                        "password": {
                            "type": "string",
                            "format": "password",
                            "writeOnly": True,
                        },
                    },
                },
            },
        }
        await store.save_template("chat", schema)
        await store.update_current(
            "chat",
            {"server": {"host": "0.0.0.0", "port": 3000, "password": "realpass"}},
        )

        resp = await client.put(
            "/services/chat/config",
            json={"values": {"server": {"host": "10.0.0.1", "password": "***"}}},
            headers=auth_headers,
        )
        assert resp.status_code == 204

        current = await store.get_current("chat")
        # port not in submitted → falls back to schema default (8080)
        assert current == {
            "server": {"host": "10.0.0.1", "port": 8080, "password": "realpass"},
        }

    async def test_write_config_to_volume_called_on_put(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("chat", template)

        # Seed a ComponentConfig so put_service_config finds config_volume
        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="chat",
            image="chat:latest",
            container_name="chat",
            has_config_yaml=True,
            config_volume="chat-config",
        )
        await config_store.put(cfg)

        captured: list[tuple] = []
        original = server_mod.app.state.backend.write_config_to_volume

        async def _fake_write(volume_name: str, config_dict: dict) -> None:
            captured.append((volume_name, config_dict))
            return await original(volume_name, config_dict)

        monkeypatch.setattr(
            server_mod.app.state.backend,
            "write_config_to_volume",
            _fake_write,
        )

        resp = await client.put(
            "/services/chat/config",
            json={"values": {"host": "10.0.0.1"}},
            headers=auth_headers,
        )
        assert resp.status_code == 204
        assert len(captured) == 1
        assert captured[0][0] == "chat-config"
        assert captured[0][1] == {"host": "10.0.0.1"}

    async def test_put_config_restarts_primary_when_running(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """PUT /services/{name}/config restarts the primary component when it's RUNNING."""
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("chat", template)

        # Seed ComponentConfig with config_volume
        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="chat",
            image="chat:latest",
            container_name="chat",
            has_config_yaml=True,
            config_volume="chat-config",
        )
        await config_store.put(cfg)

        # Set the record to RUNNING so restart is triggered
        svc_store = server_mod.app.state.store
        record = await svc_store.get("chat")
        record.state = ServiceState.RUNNING
        await svc_store.put(record)

        # Track restart calls
        restarted: list[str] = []
        original_restart = server_mod.app.state.backend.restart

        async def _fake_restart(rec):
            restarted.append(rec.name)
            return await original_restart(rec)

        monkeypatch.setattr(
            server_mod.app.state.backend,
            "restart",
            _fake_restart,
        )

        resp = await client.put(
            "/services/chat/config",
            json={"values": {"host": "10.0.0.1"}},
            headers=auth_headers,
        )
        assert resp.status_code == 204
        assert restarted == ["chat"]

    async def test_put_config_restart_failure_does_not_fail_request(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """A failed restart after a successful config write does NOT fail the PUT request."""
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("chat", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="chat",
            image="chat:latest",
            container_name="chat",
            has_config_yaml=True,
            config_volume="chat-config",
        )
        await config_store.put(cfg)

        svc_store = server_mod.app.state.store
        record = await svc_store.get("chat")
        record.state = ServiceState.RUNNING
        await svc_store.put(record)

        # Make restart raise
        async def _failing_restart(rec):
            raise RuntimeError("simulated restart failure")

        monkeypatch.setattr(
            server_mod.app.state.backend,
            "restart",
            _failing_restart,
        )

        resp = await client.put(
            "/services/chat/config",
            json={"values": {"host": "10.0.0.1"}},
            headers=auth_headers,
        )
        # Still 204 — config was written successfully
        assert resp.status_code == 204

    async def test_put_config_does_not_restart_when_not_running(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """PUT /services/{name}/config does NOT restart a STOPPED component."""
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("chat", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="chat",
            image="chat:latest",
            container_name="chat",
            has_config_yaml=True,
            config_volume="chat-config",
        )
        await config_store.put(cfg)

        # Record stays STOPPED (default from _seed_store)

        restarted: list[str] = []
        original_restart = server_mod.app.state.backend.restart

        async def _fake_restart(rec):
            restarted.append(rec.name)
            return await original_restart(rec)

        monkeypatch.setattr(
            server_mod.app.state.backend,
            "restart",
            _fake_restart,
        )

        resp = await client.put(
            "/services/chat/config",
            json={"values": {"host": "10.0.0.1"}},
            headers=auth_headers,
        )
        assert resp.status_code == 204
        assert restarted == []  # no restart for stopped component


# ---------------------------------------------------------------------------
# POST /services/{name}/config/assist
# ---------------------------------------------------------------------------


class TestGetServiceConfigAssistFields:
    """GET /services/{name}/config returns config_assist_* fields correctly."""

    async def test_returns_assist_fields_when_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "account": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "password": {
                            "type": "string",
                            "format": "password",
                            "writeOnly": True,
                        },
                    },
                },
            },
        }
        await store.save_template("auto-mail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_assist_command="detect",
            config_assist_seeds=[
                ConfigAssistSeed(key="account.email"),
                ConfigAssistSeed(key="account.password"),
            ],
        )
        await config_store.put(cfg)

        resp = await client.get("/services/auto-mail/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["config_assist_command"] == "detect"
        assert data["config_assist_seeds"] == [
            {"key": "account.email", "label": None},
            {"key": "account.password", "label": None},
        ]

    async def test_returns_seeds_with_labels(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        await store.save_template("auto-mail", {"host": ""})

        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_assist_command="detect",
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username", label="Email"),
                ConfigAssistSeed(key="accounts.0.auth.password", label="Password"),
            ],
        )
        await config_store.put(cfg)

        resp = await client.get("/services/auto-mail/config", headers=auth_headers)
        assert resp.status_code == 200
        seeds = resp.json()["config_assist_seeds"]
        assert seeds == [
            {"key": "accounts.0.auth.username", "label": "Email"},
            {"key": "accounts.0.auth.password", "label": "Password"},
        ]

    async def test_returns_null_and_empty_when_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("chat", template)

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["config_assist_command"] is None
        assert data["config_assist_seeds"] == []

    async def test_returns_null_and_empty_when_no_component_config(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Even without any ComponentConfig registered, the fields default safely."""
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("chat", template)

        resp = await client.get("/services/chat/config", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["config_assist_command"] is None
        assert data["config_assist_seeds"] == []


class TestConfigAssist:
    """POST /services/{name}/config/assist endpoint."""

    async def test_success_path(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "account": {"email": "", "password": ""},
            "imap": {"host": "", "port": 993, "tls": True},
        }
        await store.save_template("auto-mail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command="detect",
            config_assist_seeds=[
                ConfigAssistSeed(key="account.email"),
                ConfigAssistSeed(key="account.password"),
            ],
            mounts=[VolumeMount(host="auto-mail-config", container="/config")],
            env={"APP_ENV": "prod"},
        )
        await config_store.put(cfg)

        # Mock the backend to return auto-filled config and output
        async def _fake_run_assist(
            image,
            command_str,
            volume_name,
            volume_mount_path,
            env_dict,
            timeout_seconds=60,
        ) -> str:
            return "detect: found imap.gmail.com:993, smtp.gmail.com:587"

        async def _fake_read_config(volume_name: str) -> dict:
            return {
                "account": {"email": "user@example.com", "password": "***"},
                "imap": {"host": "imap.gmail.com", "port": 993, "tls": True},
                "smtp": {"host": "smtp.gmail.com", "port": 587, "tls": True},
            }

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend, "read_config_from_volume", _fake_read_config
        )

        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={
                "values": {
                    "account": {"email": "user@example.com", "password": "s3cret"}
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "config" in data
        assert data["config"]["imap"]["host"] == "imap.gmail.com"
        assert data["config"]["smtp"]["host"] == "smtp.gmail.com"
        assert "output" in data
        assert "detect:" in data["output"]

        # Verify we persisted the detected config to config_yaml_store
        current = await store.get_current("auto-mail")
        assert current is not None
        assert current["imap"]["host"] == "imap.gmail.com"
        assert current["smtp"]["host"] == "smtp.gmail.com"

    async def test_404_when_component_not_found(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.post(
            "/services/nonexistent/config/assist",
            json={"values": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_400_when_no_assist_command(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("chat", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="chat",
            image="chat:latest",
            container_name="chat",
            has_config_yaml=True,
            config_volume="chat-config",
            config_assist_command=None,
        )
        await config_store.put(cfg)

        resp = await client.post(
            "/services/chat/config/assist",
            json={"values": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "No config-assist command" in resp.json()["error"]

    async def test_400_when_no_config_volume(
        self, client: AsyncClient, auth_headers: dict
    ):
        await _seed_store("chat")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("chat", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="chat",
            image="chat:latest",
            container_name="chat",
            has_config_yaml=True,
            config_volume=None,
            config_assist_command="detect",
        )
        await config_store.put(cfg)

        resp = await client.post(
            "/services/chat/config/assist",
            json={"values": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "config volume" in resp.json()["error"].lower()

    async def test_504_on_timeout(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("auto-mail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command="detect",
        )
        await config_store.put(cfg)

        async def _fake_timeout(*args, **kwargs):
            raise TimeoutError("timed out after 60s")

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_timeout
        )

        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={"values": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 504

    async def test_nonzero_exit_returns_200_with_output(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("auto-mail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command="detect",
        )
        await config_store.put(cfg)

        async def _fake_runtime_error(*args, **kwargs):
            raise RuntimeError("config-assist exited with code 1:\nsome error output")

        async def _fake_read_config(volume_name: str) -> dict:
            return {"host": "partial-result"}

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_runtime_error
        )
        monkeypatch.setattr(
            server_mod.app.state.backend, "read_config_from_volume", _fake_read_config
        )

        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={"values": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "config" in data
        assert data["config"]["host"] == "partial-result"
        assert "exited with code 1" in data["output"]

    async def test_seed_placeholder_substitution(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """{seed} placeholders in the command get substituted from submitted values."""
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "account": {"email": "", "password": ""},
            "imap": {"host": "", "port": 993},
        }
        await store.save_template("auto-mail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command="detect {account.email} --no-verify --output /config/config.yaml",
            config_assist_seeds=[
                ConfigAssistSeed(key="account.email"),
                ConfigAssistSeed(key="account.password"),
            ],
            mounts=[VolumeMount(host="auto-mail-config", container="/config")],
        )
        await config_store.put(cfg)

        received_command: list[str] = []

        async def _fake_run_assist(
            image,
            command_str,
            volume_name,
            volume_mount_path,
            env_dict,
            timeout_seconds=60,
        ) -> str:
            received_command.append(command_str)
            return "detect: OK"

        async def _fake_read_config(volume_name: str) -> dict:
            return {"imap": {"host": "imap.gmail.com"}}

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend, "read_config_from_volume", _fake_read_config
        )

        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={
                "values": {"account": {"email": "test@gmail.com", "password": "s3cret"}}
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert len(received_command) == 1
        # Placeholder should be substituted with the actual value
        assert "test@gmail.com" in received_command[0]
        assert "{account.email}" not in received_command[0]

    async def test_detected_output_merged_not_clobbered(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """Detected fields are merged into the submitted config — other fields preserved."""
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "account": {"email": "", "password": ""},
            "imap": {"host": "", "port": 993, "tls": True},
        }
        await store.save_template("auto-mail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command="detect {account.email} --no-verify --output /config/config.yaml",
            config_assist_seeds=[ConfigAssistSeed(key="account.email")],
            mounts=[VolumeMount(host="auto-mail-config", container="/config")],
        )
        await config_store.put(cfg)

        async def _fake_run_assist(
            image,
            command_str,
            volume_name,
            volume_mount_path,
            env_dict,
            timeout_seconds=60,
        ) -> str:
            return "detect: OK"

        async def _fake_read_config(volume_name: str) -> dict:
            # Simulate detect only returning the fields it knows about
            return {"imap": {"host": "imap.gmail.com"}}

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend, "read_config_from_volume", _fake_read_config
        )

        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={
                "values": {
                    "account": {"email": "test@gmail.com", "password": "s3cret"},
                    "imap": {"port": 993, "tls": True},
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        config = data["config"]
        # Detected fields
        assert config["imap"]["host"] == "imap.gmail.com"
        # User-entered fields preserved
        assert config["account"]["email"] == "test@gmail.com"
        assert config["account"]["password"] == "s3cret"
        assert config["imap"]["port"] == 993
        assert config["imap"]["tls"] is True

    async def test_stale_command_refreshed_from_repo(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """config_assist_command and config_assist_seeds are refreshed from repo HEAD."""
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("auto-mail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command="old-detect --old",
            config_assist_seeds=["old_host"],
            git_url="https://github.com/example/repo.git",
        )
        await config_store.put(cfg)

        # Patch _fetch_fresh_config_assist to return fresh values synchronously
        def _fresh(git_url: str, name: str) -> tuple[str | None, list[str]]:
            return ("new-detect --new", ["new_host"])

        monkeypatch.setattr(server_mod, "_fetch_fresh_config_assist", _fresh)

        # Capture the command that run_config_assist receives
        captured_command: list[str] = []

        async def _fake_run_assist(
            image,
            command_str,
            volume_name,
            volume_mount_path,
            env_dict,
            timeout_seconds=60,
        ) -> str:
            captured_command.append(command_str)
            return "ok"

        async def _fake_read_config(volume_name: str) -> dict:
            return {}

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend, "read_config_from_volume", _fake_read_config
        )

        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={"values": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        # Command used was the fresh one
        assert len(captured_command) == 1
        assert "new-detect --new" in captured_command[0]

        # Store was updated with fresh values
        updated_cfg = config_store.get("auto-mail")
        assert updated_cfg is not None
        assert updated_cfg.config_assist_command == "new-detect --new"
        assert updated_cfg.config_assist_seeds == [ConfigAssistSeed(key="new_host")]

    async def test_fetch_failure_falls_back_to_stored_command(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """When _fetch_fresh_config_assist raises, the stored command is used."""
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
            },
        }
        await store.save_template("auto-mail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command="stored-detect",
            config_assist_seeds=[],
            git_url="https://github.com/example/repo.git",
        )
        await config_store.put(cfg)

        # Patch _fetch_fresh_config_assist to raise
        def _raise_network_error(git_url: str, name: str):
            raise Exception("simulated network error")

        monkeypatch.setattr(
            server_mod, "_fetch_fresh_config_assist", _raise_network_error
        )

        captured_command: list[str] = []

        async def _fake_run_assist(
            image,
            command_str,
            volume_name,
            volume_mount_path,
            env_dict,
            timeout_seconds=60,
        ) -> str:
            captured_command.append(command_str)
            return "ok"

        async def _fake_read_config(volume_name: str) -> dict:
            return {}

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend, "read_config_from_volume", _fake_read_config
        )

        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={"values": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Stored command was used (fell back)
        assert len(captured_command) == 1
        assert "stored-detect" in captured_command[0]

        # Store was NOT mutated on failure
        updated_cfg = config_store.get("auto-mail")
        assert updated_cfg is not None
        assert updated_cfg.config_assist_command == "stored-detect"
        assert updated_cfg.config_assist_seeds == []

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.post(
            "/services/chat/config/assist",
            json={"values": {}},
        )
        assert resp.status_code == 401

    async def test_config_assist_returns_detected_imap_smtp(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """Regression: detected imap/smtp hosts are returned — the pre-detect
        write must be sparse (no empty-string imap/smtp keys) so the detect
        program fills them in."""
        await _seed_store("test-gmail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "accounts": [
                {
                    "imap": {"host": ""},
                    "smtp": {"host": ""},
                    "auth": {"username": "", "password": ""},
                }
            ]
        }
        await store.save_template("test-gmail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="test-gmail",
            image="auto-mail:latest",
            container_name="test-gmail",
            has_config_yaml=True,
            config_volume="test-gmail-config",
            config_assist_command="detect --output /config/config.yaml",
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username", label="Email"),
                ConfigAssistSeed(key="accounts.0.auth.password", label="Password"),
            ],
            mounts=[VolumeMount(host="test-gmail-config", container="/config")],
        )
        await config_store.put(cfg)

        # Capture the pre-detect write
        pre_detect_writes: list[dict] = []
        original_write = server_mod.app.state.backend.write_config_to_volume

        async def _fake_write(volume_name: str, config_dict: dict) -> None:
            pre_detect_writes.append(dict(config_dict))
            return await original_write(volume_name, config_dict)

        monkeypatch.setattr(
            server_mod.app.state.backend,
            "write_config_to_volume",
            _fake_write,
        )

        async def _fake_run_assist(*args, **kwargs) -> str:
            return "detect: OK"

        async def _fake_read_config(volume_name: str) -> dict:
            return {
                "accounts": [
                    {
                        "imap": {"host": "imap.gmail.com"},
                        "smtp": {"host": "smtp.gmail.com"},
                        "auth": {"username": "t@g.com", "password": "x"},
                    }
                ]
            }

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "read_config_from_volume",
            _fake_read_config,
        )

        resp = await client.post(
            "/services/test-gmail/config/assist",
            json={
                "values": {
                    "accounts": [{"auth": {"username": "t@g.com", "password": "x"}}]
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        cfg_result = data["config"]

        # Detected imap/smtp hosts are returned
        assert cfg_result["accounts"][0]["imap"]["host"] == "imap.gmail.com"
        assert cfg_result["accounts"][0]["smtp"]["host"] == "smtp.gmail.com"

        # User-entered auth preserved
        assert cfg_result["accounts"][0]["auth"]["username"] == "t@g.com"

        # Pre-detect write was sparse: no imap/smtp keys in accounts[0]
        assert len(pre_detect_writes) >= 1
        seed = pre_detect_writes[0]
        assert "accounts" in seed
        assert len(seed["accounts"]) == 1
        assert "imap" not in seed["accounts"][0]
        assert "smtp" not in seed["accounts"][0]

    async def test_config_assist_persists_to_store(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """After a successful auto-detect, config_yaml_store.get_current()
        returns the detected config — GET /services/{name}/config works."""
        await _seed_store("mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "accounts": [
                {
                    "id": "",
                    "imap": {"host": ""},
                    "smtp": {"host": ""},
                    "auth": {"username": "", "password": ""},
                }
            ]
        }
        await store.save_template("mail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="mail",
            image="mail:latest",
            container_name="mail",
            has_config_yaml=True,
            config_volume="mail-config",
            config_assist_command="detect",
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username"),
                ConfigAssistSeed(key="accounts.0.auth.password"),
            ],
            mounts=[VolumeMount(host="mail-config", container="/config")],
        )
        await config_store.put(cfg)

        async def _fake_run_assist(*args, **kwargs) -> str:
            return "detect: OK"

        detected = {
            "accounts": [
                {
                    "imap": {"host": "ssl0.ovh.net"},
                    "smtp": {"host": "ssl0.ovh.net"},
                    "auth": {"username": "u@x.com", "password": "x"},
                }
            ],
            "default_account": "main",
        }

        async def _fake_read_config(volume_name: str) -> dict:
            return detected

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "read_config_from_volume",
            _fake_read_config,
        )

        resp = await client.post(
            "/services/mail/config/assist",
            json={
                "values": {
                    "accounts": [{"auth": {"username": "u@x.com", "password": "x"}}]
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Store was updated — GET /config returns the detected data
        current = await store.get_current("mail")
        assert current is not None
        assert current["accounts"][0]["imap"]["host"] == "ssl0.ovh.net"
        assert current["accounts"][0]["smtp"]["host"] == "ssl0.ovh.net"

    async def test_config_assist_account_name_override(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """A POST with account_name="My Gmail" in add_new mode stores the
        slugified id "my-gmail", overriding the email-derived fallback."""
        await _seed_store("mail3")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "accounts": [
                {
                    "id": "",
                    "imap": {"host": ""},
                    "smtp": {"host": ""},
                    "auth": {"username": "", "password": ""},
                }
            ]
        }
        await store.save_template("mail3", template)
        # Seed an existing account so that mode becomes add_new (not first_setup).
        await store.update_current(
            "mail3",
            {
                "accounts": [
                    {
                        "id": "existing-acct",
                        "imap": {"host": "imap.example.com"},
                        "smtp": {"host": "smtp.example.com"},
                    }
                ]
            },
        )

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="mail3",
            image="mail:latest",
            container_name="mail3",
            has_config_yaml=True,
            config_volume="mail3-config",
            config_assist_command="detect",
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username", label="Email"),
                ConfigAssistSeed(key="accounts.0.auth.password", label="Password"),
            ],
            mounts=[VolumeMount(host="mail3-config", container="/config")],
        )
        await config_store.put(cfg)

        async def _fake_run_assist(*args, **kwargs) -> str:
            return "detect: OK"

        detected = {
            "accounts": [
                {
                    "id": "existing-acct",
                    "imap": {"host": "imap.example.com"},
                    "smtp": {"host": "smtp.example.com"},
                },
                {
                    "id": "my-gmail",
                    "imap": {"host": "imap.other.com"},
                    "smtp": {"host": "smtp.other.com"},
                    "auth": {"username": "new@x.com", "password": "x"},
                },
            ]
        }

        async def _fake_read_config(volume_name: str) -> dict:
            return detected

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "read_config_from_volume",
            _fake_read_config,
        )

        resp = await client.post(
            "/services/mail3/config/assist",
            json={
                "values": {
                    "accounts": [{"auth": {"username": "new@x.com", "password": "x"}}]
                },
                "account_name": "My Gmail",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        # add_new → new account at index 1, named via account_name slugification
        assert len(data["config"]["accounts"]) >= 2
        assert data["config"]["accounts"][1]["id"] == "my-gmail"

        # Also persisted
        current = await store.get_current("mail3")
        assert current is not None
        assert len(current["accounts"]) >= 2
        assert current["accounts"][1]["id"] == "my-gmail"

    async def test_add_second_account_leaves_first_intact(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """When accounts exist and no target_account_index is given,
        --overwrite is stripped and a new account is added at next index."""
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "accounts": [
                {
                    "id": "",
                    "auth": {"username": "", "password": ""},
                    "imap": {"host": ""},
                }
            ]
        }
        await store.save_template("auto-mail", template)
        await store.update_current(
            "auto-mail",
            {
                "accounts": [
                    {
                        "id": "main",
                        "auth": {"username": "old@example.com"},
                        "imap": {"host": "imap.old.com"},
                    }
                ]
            },
        )

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command=(
                "detect {accounts.0.auth.username} --id {accounts.0.id} --overwrite"
            ),
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username"),
            ],
            mounts=[VolumeMount(host="auto-mail-config", container="/config")],
        )
        await config_store.put(cfg)

        received_command: list[str] = []

        async def _fake_run_assist(
            image,
            command_str,
            volume_name,
            volume_mount_path,
            env_dict,
            timeout_seconds=60,
        ) -> str:
            received_command.append(command_str)
            return "detect: OK"

        async def _fake_read_config(volume_name: str) -> dict:
            return {
                "accounts": [
                    {
                        "id": "main",
                        "auth": {"username": "old@example.com"},
                        "imap": {"host": "imap.old.com"},
                    },
                    {
                        "id": "new-example-com",
                        "auth": {"username": "new@example.com"},
                        "imap": {"host": "imap.new.com"},
                    },
                ]
            }

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "read_config_from_volume",
            _fake_read_config,
        )

        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={
                "values": {
                    "accounts": [
                        {
                            "id": "main",
                            "auth": {"username": "old@example.com"},
                        },
                        {
                            "auth": {"username": "new@example.com"},
                        },
                    ]
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        accounts = data["config"]["accounts"]
        assert len(accounts) == 2
        assert accounts[0]["id"] == "main"
        assert accounts[1]["id"] == "new-example-com"

        assert len(received_command) == 1
        cmd = received_command[0]
        assert "--overwrite" not in cmd
        # Placeholders were rewritten to accounts.1, then resolved — so we
        # should see the new account values, not the old ones.
        assert "new@example.com" in cmd
        assert "accounts.0." not in cmd  # old placeholder index absent

    async def test_add_account_seed_bar_relocates_to_new_index(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """Real frontend flow: seed bar writes to accounts.0.*, but
        add_new mode relocates values to accounts[N] so the existing
        account is not corrupted and {accounts.N.*} resolves."""
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "accounts": [
                {
                    "id": "",
                    "auth": {"username": "", "password": ""},
                    "imap": {"host": ""},
                }
            ]
        }
        await store.save_template("auto-mail", template)
        await store.update_current(
            "auto-mail",
            {
                "accounts": [
                    {
                        "id": "ovh",
                        "auth": {"username": "ovh-user@ovh.com"},
                        "imap": {"host": "imap.ovh.com"},
                    }
                ]
            },
        )

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command=(
                "detect {accounts.0.auth.username} --id {accounts.0.id} --overwrite"
            ),
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username"),
            ],
            mounts=[VolumeMount(host="auto-mail-config", container="/config")],
        )
        await config_store.put(cfg)

        # Capture the pre-detect volume write so we can verify existing
        # accounts are not clobbered.
        captured_writes: list[dict] = []

        async def _fake_write(volume_name: str, config_dict: dict) -> None:
            captured_writes.append(dict(config_dict))

        monkeypatch.setattr(
            server_mod.app.state.backend,
            "write_config_to_volume",
            _fake_write,
        )

        received_command: list[str] = []

        async def _fake_run_assist(
            image,
            command_str,
            volume_name,
            volume_mount_path,
            env_dict,
            timeout_seconds=60,
        ) -> str:
            received_command.append(command_str)
            return "detect: OK"

        async def _fake_read_config(volume_name: str) -> dict:
            return {
                "accounts": [
                    {
                        "id": "ovh",
                        "auth": {"username": "ovh-user@ovh.com"},
                        "imap": {"host": "imap.ovh.com"},
                    },
                    {
                        "id": "damien-robotsix-gmail-com",
                        "auth": {"username": "damien.robotsix@gmail.com"},
                        "imap": {"host": "imap.gmail.com"},
                    },
                ]
            }

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "read_config_from_volume",
            _fake_read_config,
        )

        # Simulate the real frontend: only accounts[0] is submitted (the
        # seed bar writes the new email to accounts.0.auth.username).
        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={
                "values": {
                    "accounts": [
                        {
                            "id": "ovh",
                            "auth": {"username": "damien.robotsix@gmail.com"},
                        },
                    ]
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        accounts = data["config"]["accounts"]
        assert len(accounts) == 2
        # Existing account untouched.
        assert accounts[0]["id"] == "ovh"
        assert accounts[0]["auth"]["username"] == "ovh-user@ovh.com"
        # New account has ID derived from email (not "accounts-1" fallback).
        assert accounts[1]["id"] == "damien-robotsix-gmail-com"
        assert accounts[1]["auth"]["username"] == "damien.robotsix@gmail.com"

        assert len(received_command) == 1
        cmd = received_command[0]
        assert "--overwrite" not in cmd
        # Placeholders resolved to the new email, not left as literal
        # {accounts.1.auth.username}.
        assert "damien.robotsix@gmail.com" in cmd
        assert "{accounts.1." not in cmd

        # Volume write: existing account NOT clobbered
        # 2 writes: [0] pre-detect seed, [1] cleaned config written back (#114)
        assert len(captured_writes) == 2
        vol_accts = captured_writes[0]["accounts"]
        assert vol_accts[0]["id"] == "ovh"
        assert vol_accts[0]["auth"]["username"] == "ovh-user@ovh.com"

    async def test_update_nth_account_uses_overwrite(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """When target_account_index=1 and two accounts exist,
        --overwrite is kept and accounts.1.* placeholders are used."""
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "accounts": [
                {
                    "id": "",
                    "auth": {"username": "", "password": ""},
                    "imap": {"host": ""},
                }
            ]
        }
        await store.save_template("auto-mail", template)
        await store.update_current(
            "auto-mail",
            {
                "accounts": [
                    {
                        "id": "main",
                        "auth": {"username": "old@example.com"},
                        "imap": {"host": "imap.old.com"},
                    },
                    {
                        "id": "secondary",
                        "auth": {"username": "sec@example.com"},
                        "imap": {"host": "imap.sec.com"},
                    },
                ]
            },
        )

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command=(
                "detect {accounts.0.auth.username} --id {accounts.0.id} --overwrite"
            ),
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username"),
            ],
            mounts=[VolumeMount(host="auto-mail-config", container="/config")],
        )
        await config_store.put(cfg)

        received_command: list[str] = []

        async def _fake_run_assist(
            image,
            command_str,
            volume_name,
            volume_mount_path,
            env_dict,
            timeout_seconds=60,
        ) -> str:
            received_command.append(command_str)
            return "detect: OK"

        async def _fake_read_config(volume_name: str) -> dict:
            return {
                "accounts": [
                    {
                        "id": "main",
                        "auth": {"username": "old@example.com"},
                        "imap": {"host": "imap.old.com"},
                    },
                    {
                        "id": "secondary",
                        "auth": {"username": "updated@example.com"},
                        "imap": {"host": "imap.updated.com"},
                    },
                ]
            }

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "read_config_from_volume",
            _fake_read_config,
        )

        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={
                "values": {
                    "accounts": [
                        {
                            "id": "main",
                            "auth": {"username": "old@example.com"},
                        },
                        {
                            "id": "secondary",
                            "auth": {"username": "updated@example.com"},
                        },
                    ]
                },
                "target_account_index": 1,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        accounts = data["config"]["accounts"]
        assert len(accounts) == 2
        assert accounts[0]["id"] == "main"

        assert len(received_command) == 1
        cmd = received_command[0]
        assert "--overwrite" in cmd
        assert "updated@example.com" in cmd
        assert "accounts.0." not in cmd

    async def test_add_account_three_existing_seed_bar_preserves_all(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """When 3 accounts exist (e.g. ovh + gmail + gmail-robotsix) and the
        seed bar clobbers accounts[0].auth.username, all existing accounts
        must be preserved verbatim — no credential corruption, no
        re-detection, and the new account lands at the correct index."""
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "accounts": [
                {
                    "id": "",
                    "auth": {"username": "", "password": "SECRET"},
                    "imap": {"host": ""},
                }
            ]
        }
        await store.save_template("auto-mail", template)
        await store.update_current(
            "auto-mail",
            {
                "accounts": [
                    {
                        "id": "ovh",
                        "auth": {"username": "ovh@ovh.com", "password": "secret-ovh"},
                        "imap": {"host": "imap.ovh.com"},
                    },
                    {
                        "id": "gmail",
                        "auth": {
                            "username": "g@g.com",
                            "password": "secret-gmail",
                        },
                        "imap": {"host": "imap.gmail.com"},
                    },
                    {
                        "id": "gmail-robotsix",
                        "auth": {"username": "r@g.com", "password": "secret-r"},
                        "imap": {"host": "imap.gmail.com"},
                    },
                ]
            },
        )

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command=(
                "detect {accounts.0.auth.username} --id {accounts.0.id} --overwrite"
            ),
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username"),
            ],
            mounts=[VolumeMount(host="auto-mail-config", container="/config")],
        )
        await config_store.put(cfg)

        # Capture the pre-detect volume write to verify existing accounts
        # are written verbatim (not clobbered by seed bar).
        captured_writes: list[dict] = []

        async def _fake_write(volume_name: str, config_dict: dict) -> None:
            captured_writes.append(dict(config_dict))

        monkeypatch.setattr(
            server_mod.app.state.backend,
            "write_config_to_volume",
            _fake_write,
        )

        received_command: list[str] = []

        async def _fake_run_assist(
            image,
            command_str,
            volume_name,
            volume_mount_path,
            env_dict,
            timeout_seconds=60,
        ) -> str:
            received_command.append(command_str)
            return "detect: OK"

        async def _fake_read_config(volume_name: str) -> dict:
            return {
                "accounts": [
                    {
                        "id": "ovh",
                        "auth": {
                            "username": "ovh@ovh.com",
                            "password": "secret-ovh",
                        },
                        "imap": {"host": "imap.ovh.com"},
                    },
                    {
                        "id": "gmail",
                        "auth": {
                            "username": "g@g.com",
                            "password": "secret-gmail",
                        },
                        "imap": {"host": "imap.gmail.com"},
                    },
                    {
                        "id": "gmail-robotsix",
                        "auth": {"username": "r@g.com", "password": "secret-r"},
                        "imap": {"host": "imap.gmail.com"},
                    },
                    {
                        "id": "damien-robotsix-gmail-com",
                        "auth": {"username": "damien.robotsix@gmail.com"},
                        "imap": {"host": "imap.gmail.com"},
                    },
                ]
            }

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "read_config_from_volume",
            _fake_read_config,
        )

        # Simulate real frontend: all 3 existing accounts + seed bar
        # clobbering slot 0 username (form secret fields are empty strings).
        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={
                "values": {
                    "accounts": [
                        {
                            "id": "ovh",
                            "auth": {
                                "username": "damien.robotsix@gmail.com",
                                "password": "",
                            },
                        },
                        {
                            "id": "gmail",
                            "auth": {"username": "g@g.com", "password": ""},
                        },
                        {
                            "id": "gmail-robotsix",
                            "auth": {"username": "r@g.com", "password": ""},
                        },
                    ]
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        accounts = data["config"]["accounts"]
        assert len(accounts) == 4

        # --- Existing accounts preserved verbatim ---
        assert accounts[0]["id"] == "ovh"
        assert accounts[0]["auth"]["username"] == "ovh@ovh.com"
        assert accounts[1]["id"] == "gmail"
        assert accounts[1]["auth"]["username"] == "g@g.com"
        assert accounts[2]["id"] == "gmail-robotsix"
        assert accounts[2]["auth"]["username"] == "r@g.com"

        # New account lands at index 3 with derived id
        assert accounts[3]["id"] == "damien-robotsix-gmail-com"
        assert accounts[3]["auth"]["username"] == "damien.robotsix@gmail.com"

        # --- Volume pre-detect write ---
        # 2 writes: [0] pre-detect seed, [1] cleaned config written back (#114)
        assert len(captured_writes) == 2
        seed = captured_writes[0]
        seed_accts = seed["accounts"]
        assert len(seed_accts) == 4  # 3 existing + 1 new seed

        # Existing account 0 (ovh) NOT clobbered in volume write
        assert seed_accts[0]["auth"]["username"] == "ovh@ovh.com"
        assert seed_accts[0]["auth"]["password"] == "secret-ovh"
        # Existing account 1 (gmail) preserved
        assert seed_accts[1]["auth"]["username"] == "g@g.com"
        assert seed_accts[1]["auth"]["password"] == "secret-gmail"
        # Existing account 2 (gmail-robotsix) preserved
        assert seed_accts[2]["auth"]["username"] == "r@g.com"
        assert seed_accts[2]["auth"]["password"] == "secret-r"

        # New account seed has the submitted email only (sparse)
        assert seed_accts[3]["auth"]["username"] == "damien.robotsix@gmail.com"

        # --- Command ---
        assert len(received_command) == 1
        cmd = received_command[0]
        assert "damien.robotsix@gmail.com" in cmd
        assert "--id damien-robotsix-gmail-com" in cmd
        assert "--overwrite" not in cmd

    async def test_add_account_name_used_when_template_id_is_placeholder(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """Template has <account-N> placeholder default; the user-supplied
        account_name must override it and must NOT leak into the command
        or stored config."""
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "accounts": [
                {
                    "id": "<account-N>",
                    "auth": {"username": "", "password": "SECRET"},
                    "imap": {"host": ""},
                }
            ]
        }
        await store.save_template("auto-mail", template)
        await store.update_current(
            "auto-mail",
            {
                "accounts": [
                    {
                        "id": "main",
                        "auth": {"username": "old@example.com"},
                        "imap": {"host": "imap.old.com"},
                    }
                ]
            },
        )

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command=(
                "detect {accounts.0.auth.username} --id {accounts.0.id} --overwrite"
            ),
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username"),
            ],
            mounts=[VolumeMount(host="auto-mail-config", container="/config")],
        )
        await config_store.put(cfg)

        # Suppress volume write (not relevant for this test).
        async def _fake_write(volume_name: str, config_dict: dict) -> None:
            pass

        monkeypatch.setattr(
            server_mod.app.state.backend,
            "write_config_to_volume",
            _fake_write,
        )

        received_command: list[str] = []

        async def _fake_run_assist(
            image,
            command_str,
            volume_name,
            volume_mount_path,
            env_dict,
            timeout_seconds=60,
        ) -> str:
            received_command.append(command_str)
            return "detect: OK"

        async def _fake_read_config(volume_name: str) -> dict:
            return {
                "accounts": [
                    {
                        "id": "main",
                        "auth": {"username": "old@example.com"},
                        "imap": {"host": "imap.old.com"},
                    },
                    {
                        "id": "mygmail",
                        "auth": {"username": "new@x.com"},
                        "imap": {"host": "imap.gmail.com"},
                    },
                ]
            }

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "read_config_from_volume",
            _fake_read_config,
        )

        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={
                "values": {
                    "accounts": [
                        {
                            "id": "main",
                            "auth": {"username": "old@example.com", "password": ""},
                        },
                        {
                            "auth": {"username": "new@x.com", "password": ""},
                        },
                    ]
                },
                "account_name": "mygmail",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        accounts = data["config"]["accounts"]
        assert len(accounts) == 2

        # New account id is the user-supplied name, not <account-N>
        assert accounts[1]["id"] == "mygmail"

        # No <account-N> placeholder anywhere in the response
        for acct in accounts:
            assert "<account-N>" not in str(acct)

        # Command contains --id mygmail, not <account-N>
        assert len(received_command) == 1
        cmd = received_command[0]
        assert "--id mygmail" in cmd
        assert "<account-N>" not in cmd

    async def test_first_setup_no_accounts_backward_compat(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """When no accounts exist and target_account_index is omitted,
        first_setup mode keeps --overwrite and accounts.0.* placeholders."""
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "accounts": [
                {
                    "id": "",
                    "auth": {"username": "", "password": ""},
                    "imap": {"host": ""},
                }
            ]
        }
        await store.save_template("auto-mail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command=(
                "detect {accounts.0.auth.username} --id {accounts.0.id} --overwrite"
            ),
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username"),
            ],
            mounts=[VolumeMount(host="auto-mail-config", container="/config")],
        )
        await config_store.put(cfg)

        received_command: list[str] = []

        async def _fake_run_assist(
            image,
            command_str,
            volume_name,
            volume_mount_path,
            env_dict,
            timeout_seconds=60,
        ) -> str:
            received_command.append(command_str)
            return "detect: OK"

        async def _fake_read_config(volume_name: str) -> dict:
            return {
                "accounts": [
                    {
                        "id": "new-account",
                        "auth": {"username": "user@example.com"},
                        "imap": {"host": "imap.example.com"},
                    }
                ]
            }

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "read_config_from_volume",
            _fake_read_config,
        )

        # Omit target_account_index entirely (None is default)
        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={
                "values": {
                    "accounts": [
                        {
                            "auth": {"username": "user@example.com"},
                        }
                    ]
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        accounts = data["config"]["accounts"]
        assert len(accounts) == 1

        assert len(received_command) == 1
        cmd = received_command[0]
        assert "--overwrite" in cmd
        # In first_setup, accounts.0. placeholders are resolved from the
        # submitted values — verify the value made it into the command.
        assert "user@example.com" in cmd

    async def test_no_main_stub_on_fresh_onboard(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """Fresh onboard with a template containing an id='main' placeholder
        must NOT enter add_new mode — the placeholder should not count as an
        existing account.  After detect, get_current returns exactly one
        account, id != 'main', with imap/auth filled from detect output."""
        await _seed_store("fresh-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "accounts": [
                {
                    "id": "main",
                    "auth": {"username": "", "password": ""},
                    "imap": {"host": ""},
                    "smtp": {"host": ""},
                }
            ]
        }
        await store.save_template("fresh-mail", template)
        # Do NOT call store.update_current — simulates a fresh onboard.

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="fresh-mail",
            image="fresh-mail:latest",
            container_name="fresh-mail",
            has_config_yaml=True,
            config_volume="fresh-mail-config",
            config_assist_command=(
                "detect {accounts.0.auth.username} --id {accounts.0.id} --overwrite"
            ),
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username"),
            ],
            mounts=[VolumeMount(host="fresh-mail-config", container="/config")],
        )
        await config_store.put(cfg)

        async def _fake_run_assist(*args, **kwargs) -> str:
            return "detect: OK"

        async def _fake_read_config(volume_name: str) -> dict:
            return {
                "accounts": [
                    {
                        "id": "x-com",
                        "auth": {"username": "x@y.com", "password": "secret"},
                        "imap": {"host": "imap.x.com"},
                        "smtp": {"host": "smtp.x.com"},
                    }
                ]
            }

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "read_config_from_volume",
            _fake_read_config,
        )

        resp = await client.post(
            "/services/fresh-mail/config/assist",
            json={
                "values": {
                    "accounts": [
                        {
                            "auth": {
                                "username": "x@y.com",
                                "password": "secret",
                            }
                        }
                    ]
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        accounts = data["config"]["accounts"]
        assert len(accounts) == 1, (
            f"Expected 1 account after fresh onboard, got {len(accounts)}: {accounts}"
        )
        assert accounts[0].get("id") != "main", (
            "Placeholder 'main' should have been replaced"
        )
        assert accounts[0]["imap"]["host"] == "imap.x.com"
        assert accounts[0]["auth"]["username"] == "x@y.com"

        # Verify persistence
        current = await store.get_current("fresh-mail")
        assert current is not None
        assert len(current["accounts"]) == 1
        assert current["accounts"][0].get("id") != "main"

    async def test_existing_accounts_preserved_add_new(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """Regression guard: after Edit 1, add_new mode still works correctly.
        Pre-seed one real account; send a second via the seed bar.
        After assist, get_current returns exactly two accounts:
        slot 0 matches the pre-existing account, slot 1 has the new imap host."""
        await _seed_store("mail-multi")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "accounts": [
                {
                    "id": "main",
                    "auth": {"username": "", "password": ""},
                    "imap": {"host": ""},
                    "smtp": {"host": ""},
                }
            ]
        }
        await store.save_template("mail-multi", template)
        # Pre-seed one real account — add_new should be reachable.
        await store.update_current(
            "mail-multi",
            {
                "accounts": [
                    {
                        "id": "existing",
                        "auth": {"username": "a@b.com", "password": "x"},
                        "imap": {"host": "imap.b.com"},
                        "smtp": {"host": "smtp.b.com"},
                    }
                ]
            },
        )

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="mail-multi",
            image="mail-multi:latest",
            container_name="mail-multi",
            has_config_yaml=True,
            config_volume="mail-multi-config",
            config_assist_command="detect --overwrite",
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username"),
                ConfigAssistSeed(key="accounts.0.auth.password"),
            ],
            mounts=[VolumeMount(host="mail-multi-config", container="/config")],
        )
        await config_store.put(cfg)

        async def _fake_run_assist(*args, **kwargs) -> str:
            return "detect: OK"

        async def _fake_read_config(volume_name: str) -> dict:
            return {
                "accounts": [
                    {
                        "id": "existing",
                        "auth": {"username": "a@b.com", "password": "x"},
                        "imap": {"host": "imap.b.com"},
                        "smtp": {"host": "smtp.b.com"},
                    },
                    {
                        "id": "new-acct",
                        "auth": {"username": "n@c.com", "password": "y"},
                        "imap": {"host": "imap.c.com"},
                        "smtp": {"host": "smtp.c.com"},
                    },
                ]
            }

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "read_config_from_volume",
            _fake_read_config,
        )

        resp = await client.post(
            "/services/mail-multi/config/assist",
            json={
                "values": {
                    "accounts": [
                        {
                            "auth": {
                                "username": "n@c.com",
                                "password": "y",
                            }
                        }
                    ]
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        accounts = data["config"]["accounts"]
        assert len(accounts) == 2, (
            f"Expected 2 accounts, got {len(accounts)}: {accounts}"
        )
        assert accounts[0]["id"] == "existing"
        assert accounts[1]["imap"]["host"] == "imap.c.com"

        # Verify persistence
        current = await store.get_current("mail-multi")
        assert current is not None
        assert len(current["accounts"]) == 2
        assert current["accounts"][0]["id"] == "existing"
        assert current["accounts"][1]["imap"]["host"] == "imap.c.com"

    async def test_office365_imap_host_sets_oauth2_flag(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "accounts": [
                {
                    "imap": {"host": ""},
                    "smtp": {"host": ""},
                    "auth": {"username": "", "password": ""},
                }
            ]
        }
        await store.save_template("auto-mail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command="detect",
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username", label="Email"),
                ConfigAssistSeed(key="accounts.0.auth.password", label="Password"),
            ],
            mounts=[VolumeMount(host="auto-mail-config", container="/config")],
        )
        await config_store.put(cfg)

        async def _fake_run_assist(*args, **kwargs) -> str:
            return "detect: OK"

        async def _fake_read_config(volume_name: str) -> dict:
            return {
                "accounts": [
                    {
                        "imap": {"host": "outlook.office365.com"},
                        "smtp": {"host": "smtp.office365.com"},
                        "auth": {"username": "user@example.com", "password": "hunter2"},
                    }
                ]
            }

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "read_config_from_volume",
            _fake_read_config,
        )

        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={
                "values": {
                    "accounts": [
                        {
                            "auth": {
                                "username": "user@example.com",
                                "password": "hunter2",
                            }
                        }
                    ]
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["config"]["accounts"][0]["auth"]["oauth2_provider"] == "microsoft"
        assert "password" not in data["config"]["accounts"][0]["auth"]
        assert "Microsoft/Office365" in data["output"]
        assert "Authorize button" in data["output"]

    async def test_office365_smtp_only_triggers_flag(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        await _seed_store("auto-mail")
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = {
            "accounts": [
                {
                    "imap": {"host": ""},
                    "smtp": {"host": ""},
                    "auth": {"username": "", "password": ""},
                }
            ]
        }
        await store.save_template("auto-mail", template)

        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="auto-mail",
            image="auto-mail:latest",
            container_name="auto-mail",
            has_config_yaml=True,
            config_volume="auto-mail-config",
            config_assist_command="detect",
            config_assist_seeds=[
                ConfigAssistSeed(key="accounts.0.auth.username", label="Email"),
                ConfigAssistSeed(key="accounts.0.auth.password", label="Password"),
            ],
            mounts=[VolumeMount(host="auto-mail-config", container="/config")],
        )
        await config_store.put(cfg)

        async def _fake_run_assist(*args, **kwargs) -> str:
            return "detect: OK"

        async def _fake_read_config(volume_name: str) -> dict:
            return {
                "accounts": [
                    {
                        "imap": {"host": "imap.example.com"},
                        "smtp": {"host": "smtp.office365.com"},
                        "auth": {"username": "user@example.com", "password": "hunter2"},
                    }
                ]
            }

        monkeypatch.setattr(
            server_mod.app.state.backend, "run_config_assist", _fake_run_assist
        )
        monkeypatch.setattr(
            server_mod.app.state.backend,
            "read_config_from_volume",
            _fake_read_config,
        )

        resp = await client.post(
            "/services/auto-mail/config/assist",
            json={
                "values": {
                    "accounts": [
                        {
                            "auth": {
                                "username": "user@example.com",
                                "password": "hunter2",
                            }
                        }
                    ]
                }
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["config"]["accounts"][0]["auth"]["oauth2_provider"] == "microsoft"
        assert "Microsoft/Office365" in data["output"]


# ---------------------------------------------------------------------------
# _namespace_spec_volumes unit tests
# ---------------------------------------------------------------------------


class TestNamespaceSpecVolumes:
    """Unit tests for the volume-namespacing helper."""

    def test_renames_primary_volume_mounts(self):
        spec = DerivedSpec.model_construct(
            name="test-svc",
            git_url="https://github.com/org/test.git",
            image="ghcr.io/org/test:main",
            ports=[],
            volume_mounts=[
                VolumeMount(host="auto-mail-config", container="/config"),
                VolumeMount(host="auto-mail-data", container="/data"),
            ],
            env={},
            claude_mount=False,
            config_volume="auto-mail-config",
            siblings=[],
        )
        result = server_mod._namespace_spec_volumes(spec, "mail")

        assert result.volume_mounts[0].host == "mail-auto-mail-config"
        assert result.volume_mounts[0].container == "/config"
        assert result.volume_mounts[1].host == "mail-auto-mail-data"
        assert result.volume_mounts[1].container == "/data"
        assert result.config_volume == "mail-auto-mail-config"

    def test_config_volume_none_is_preserved(self):
        spec = DerivedSpec.model_construct(
            name="test-svc",
            git_url="https://github.com/org/test.git",
            image="ghcr.io/org/test:main",
            ports=[],
            volume_mounts=[VolumeMount(host="vol1", container="/vol1")],
            env={},
            claude_mount=False,
            config_volume=None,
            siblings=[],
        )
        result = server_mod._namespace_spec_volumes(spec, "mail")
        assert result.config_volume is None

    def test_renames_sibling_volume_mounts(self):
        spec = DerivedSpec.model_construct(
            name="test-svc",
            git_url="https://github.com/org/test.git",
            image="ghcr.io/org/test:main",
            ports=[],
            volume_mounts=[VolumeMount(host="shared-vol", container="/shared")],
            env={},
            claude_mount=False,
            siblings=[
                SiblingDerivedSpec.model_construct(
                    service_key="worker",
                    container_name="worker",
                    image="ghcr.io/org/worker:main",
                    volume_mounts=[
                        VolumeMount(host="worker-data", container="/data"),
                    ],
                ),
                SiblingDerivedSpec.model_construct(
                    service_key="cache",
                    container_name="cache",
                    image="ghcr.io/org/cache:main",
                    volume_mounts=[
                        VolumeMount(host="cache-data", container="/cache"),
                    ],
                ),
            ],
        )
        result = server_mod._namespace_spec_volumes(spec, "zzztest")

        assert result.volume_mounts[0].host == "zzztest-shared-vol"
        assert result.siblings[0].volume_mounts[0].host == "zzztest-worker-data"
        assert result.siblings[1].volume_mounts[0].host == "zzztest-cache-data"

    def test_second_component_gets_different_names(self):
        """Same image onboarded twice produces disjoint volume names."""
        spec = DerivedSpec.model_construct(
            name="test-svc",
            git_url="https://github.com/org/test.git",
            image="ghcr.io/org/test:main",
            ports=[],
            volume_mounts=[
                VolumeMount(host="auto-mail-config", container="/config"),
                VolumeMount(host="auto-mail-data", container="/data"),
                VolumeMount(host="auto-mail-logs", container="/logs"),
            ],
            env={},
            claude_mount=False,
            config_volume="auto-mail-config",
            siblings=[],
        )
        mail_result = server_mod._namespace_spec_volumes(spec, "mail")
        zzz_result = server_mod._namespace_spec_volumes(spec, "zzztest")

        mail_hosts = {m.host for m in mail_result.volume_mounts}
        zzz_hosts = {m.host for m in zzz_result.volume_mounts}
        assert mail_hosts == {
            "mail-auto-mail-config",
            "mail-auto-mail-data",
            "mail-auto-mail-logs",
        }
        assert zzz_hosts == {
            "zzztest-auto-mail-config",
            "zzztest-auto-mail-data",
            "zzztest-auto-mail-logs",
        }
        assert mail_hosts.isdisjoint(zzz_hosts)


# ---------------------------------------------------------------------------
# GET /chat/components
# ---------------------------------------------------------------------------


class TestChatComponents:
    async def test_empty_when_no_components(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.get("/chat/components")
        assert resp.status_code == 401

    async def test_skips_components_without_allow_chat_access(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="no-chat",
            image="no-chat:latest",
            container_name="no-chat",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = False
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_component_with_skill(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="chatty",
            image="chatty:latest",
            container_name="chatty",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        # Mock the httpx.AsyncClient so the skill probe succeeds.
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "# Chatty Skill\nDo the thing."
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        # Clear the cache so we get a fresh probe.
        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "chatty"
        assert data[0]["base_url"] == "http://chatty:8080"
        assert data[0]["skill"] == "# Chatty Skill\nDo the thing."

    async def test_skips_component_with_failed_probe(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="flaky",
            image="flaky:latest",
            container_name="flaky",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        # Mock httpx.AsyncClient to raise an exception.
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=Exception("boom"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_serves_stale_skill_when_probe_fails(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="stale-ok",
            image="stale-ok:latest",
            container_name="stale-ok",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        # Probe raises, but an expired cache entry holds a last-known-good
        # skill — the component must stay in the roster with that body.
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=Exception("boom"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()
        expired_at = time.monotonic() - chat_mod._SKILL_CACHE_TTL - 1
        chat_mod._skill_cache["stale-ok"] = (expired_at, "# Stale Skill")

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "stale-ok"
        assert data[0]["skill"] == "# Stale Skill"
        # The stale timestamp is preserved so the next request re-probes.
        assert chat_mod._skill_cache["stale-ok"][0] == expired_at

    async def test_skips_component_with_non_200_probe(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="bad-status",
            image="bad-status:latest",
            container_name="bad-status",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Error"
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_skips_empty_skill_body(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="empty-skill",
            image="empty-skill:latest",
            container_name="empty-skill",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "   "  # whitespace-only
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_caches_skill_bodies(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="cached",
            image="cached:latest",
            container_name="cached",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        call_count = 0

        async def mock_get(url):
            nonlocal call_count
            call_count += 1
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = f"Skill v{call_count}"
            return mock_resp

        mock_client = MagicMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        # First call: probes and caches.
        resp1 = await client.get("/chat/components", headers=auth_headers)
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert len(data1) == 1
        assert data1[0]["skill"] == "Skill v1"

        # Second call: should use cache (no additional probe).
        resp2 = await client.get("/chat/components", headers=auth_headers)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2[0]["skill"] == "Skill v1"
        assert call_count == 1  # still 1 — cache hit

    async def test_skips_component_without_ports(
        self, client: AsyncClient, auth_headers: dict
    ):
        config_store = server_mod.app.state.component_config_store
        cfg = ComponentConfig(
            id="noport",
            image="noport:latest",
            container_name="noport",
            ports=[],
        )
        cfg.allow_chat_access = True
        await config_store.put(cfg)
        server_mod.app.state.registry.register(cfg)

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_multiple_components_mixed(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        config_store = server_mod.app.state.component_config_store

        # Component with chat access enabled that returns a skill.
        cfg1 = ComponentConfig(
            id="alpha",
            image="alpha:latest",
            container_name="alpha",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg1.allow_chat_access = True
        await config_store.put(cfg1)
        server_mod.app.state.registry.register(cfg1)

        # Component without chat access — should be skipped.
        cfg2 = ComponentConfig(
            id="beta",
            image="beta:latest",
            container_name="beta",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg2.allow_chat_access = False
        await config_store.put(cfg2)
        server_mod.app.state.registry.register(cfg2)

        # Component with chat access but probe fails — should be skipped.
        cfg3 = ComponentConfig(
            id="gamma",
            image="gamma:latest",
            container_name="gamma",
            ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        )
        cfg3.allow_chat_access = True
        await config_store.put(cfg3)
        server_mod.app.state.registry.register(cfg3)

        # Mock: alpha returns 200, gamma returns 500.
        async def mock_get(url):
            mock_resp = MagicMock()
            if "alpha" in url:
                mock_resp.status_code = 200
                mock_resp.text = "# Alpha Skill"
            else:
                mock_resp.status_code = 500
                mock_resp.text = "Error"
            return mock_resp

        mock_client = MagicMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: mock_client)

        import robotsix_central_deploy.lifecycle.routers.chat as chat_mod

        chat_mod._skill_cache.clear()

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "alpha"
