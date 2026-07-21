"""Integration tests for sibling service fan-out."""

from __future__ import annotations

import asyncio

from httpx import AsyncClient


from robotsix_central_deploy.lifecycle.models import (
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
)

# Import the server module itself (not just symbols) so we can set its globals.
import robotsix_central_deploy.lifecycle.app as server_mod


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
