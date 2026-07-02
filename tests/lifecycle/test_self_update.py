"""Tests for the /system/update self-update endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.lifecycle.models import SelfInspect


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


@pytest.fixture
def self_info() -> SelfInspect:
    return SelfInspect(
        container_id="abc123",
        container_name="central-deploy",
        image_ref="ghcr.io/damien-robotsix/robotsix-central-deploy:main",
        running_digest="sha256:old",
        networks=["robotsix-central-deploy_internal", "central-deploy-proxy"],
    )


def _mock_backend(self_info: SelfInspect | None) -> MagicMock:
    backend = MagicMock()
    backend.inspect_self = AsyncMock(return_value=self_info)
    backend.trigger_self_update = AsyncMock(return_value="watchtower-cid")
    return backend


class TestGetSelfUpdateStatus:
    async def test_requires_auth(self, client):
        resp = await client.get("/system/update")
        assert resp.status_code == 401

    async def test_unsupported_when_not_containerised(self, client, auth_headers):
        # Conftest wires the NoopBackend, whose inspect_self returns None.
        resp = await client.get("/system/update", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["supported"] is False
        assert body["update_available"] is False

    async def test_unsupported_when_backend_lacks_support(self, client, auth_headers):
        backend = MagicMock()
        backend.inspect_self = AsyncMock(side_effect=NotImplementedError)
        server_mod.app.state.backend = backend
        resp = await client.get("/system/update", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["supported"] is False

    async def test_update_available_when_digests_differ(
        self, client, auth_headers, self_info
    ):
        server_mod.app.state.backend = _mock_backend(self_info)
        server_mod.app.state.registry_checker.get_latest_digest = AsyncMock(
            return_value="sha256:new"
        )
        resp = await client.get("/system/update", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["supported"] is True
        assert body["container_name"] == "central-deploy"
        assert body["running_digest"] == "sha256:old"
        assert body["latest_digest"] == "sha256:new"
        assert body["update_available"] is True

    async def test_no_update_when_digests_match(self, client, auth_headers, self_info):
        server_mod.app.state.backend = _mock_backend(self_info)
        server_mod.app.state.registry_checker.get_latest_digest = AsyncMock(
            return_value="sha256:old"
        )
        resp = await client.get("/system/update", headers=auth_headers)
        assert resp.json()["update_available"] is False

    async def test_no_update_when_registry_unreachable(
        self, client, auth_headers, self_info
    ):
        server_mod.app.state.backend = _mock_backend(self_info)
        server_mod.app.state.registry_checker.get_latest_digest = AsyncMock(
            return_value=None
        )
        resp = await client.get("/system/update", headers=auth_headers)
        body = resp.json()
        assert body["supported"] is True
        assert body["latest_digest"] == ""
        assert body["update_available"] is False

    async def test_no_update_when_running_digest_unresolved(
        self, client, auth_headers, self_info
    ):
        self_info.running_digest = ""
        server_mod.app.state.backend = _mock_backend(self_info)
        server_mod.app.state.registry_checker.get_latest_digest = AsyncMock(
            return_value="sha256:new"
        )
        resp = await client.get("/system/update", headers=auth_headers)
        assert resp.json()["update_available"] is False


class TestTriggerSelfUpdate:
    async def test_requires_auth(self, client):
        resp = await client.post("/system/update")
        assert resp.status_code == 401

    async def test_503_when_unsupported(self, client, auth_headers):
        # NoopBackend from conftest — inspect_self returns None.
        resp = await client.post("/system/update", headers=auth_headers)
        assert resp.status_code == 503

    async def test_202_launches_watchtower(self, client, auth_headers, self_info):
        backend = _mock_backend(self_info)
        server_mod.app.state.backend = backend
        resp = await client.post("/system/update", headers=auth_headers)
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "update-started"
        assert body["updater_container_id"] == "watchtower-cid"
        backend.trigger_self_update.assert_awaited_once()
        args = backend.trigger_self_update.await_args.args
        assert args[0] is self_info
        assert args[1] == server_mod.app.state.config.self_update_watchtower_image
        assert args[2] == server_mod.app.state.config.docker_socket_url

    async def test_502_when_launch_fails(self, client, auth_headers, self_info):
        backend = _mock_backend(self_info)
        backend.trigger_self_update = AsyncMock(
            side_effect=RuntimeError("failed to launch self-update container: boom")
        )
        server_mod.app.state.backend = backend
        resp = await client.post("/system/update", headers=auth_headers)
        assert resp.status_code == 502
        assert "boom" in resp.json()["error"]
