"""Tests for the deploy-history endpoint and digest-targeted rollback."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.models import (
    DeployHistoryEntry,
    DeployOutcome,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.deploy_history_store import DeployHistoryStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    PortMapping,
    VolumeMount,
)


def _make_config(
    component_id: str = "svc-a", image: str = "repo:v1"
) -> ComponentConfig:
    return ComponentConfig(
        id=component_id,
        image=image,
        container_name=component_id,
        ports=[PortMapping(host=8080, container=8080)],
        mounts=[VolumeMount(host="/data", container="/data")],
        env={"KEY": "val"},
    )


@pytest.fixture
def registry():
    return ComponentRegistry([_make_config("svc-a", "repo:v1")])


@pytest.fixture
def component_config_store(tmp_path):
    cs = ComponentConfigStore(tmp_path / "test_ccs.json")
    cs.put = MagicMock()  # type: ignore[method-assign]
    config = _make_config()
    cs.get = MagicMock(return_value=config)  # type: ignore[method-assign]
    cs.all = MagicMock(return_value=[config])  # type: ignore[method-assign]
    return cs


async def _seed_record(name: str = "svc-a") -> ServiceRecord:
    store = server_mod.app.state.store
    record = ServiceRecord(
        name=name,
        state=ServiceState.RUNNING,
        image="repo:v1",
        deployed_image_digest="sha256:current",
        previous_image_digest="sha256:previous",
    )
    await store.put(record)
    return record


class TestGetHistory:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_history(self, client, auth_headers):
        await _seed_record("svc-a")
        resp = await client.get("/services/svc-a/history", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "svc-a"
        assert body["entries"] == []

    @pytest.mark.asyncio
    async def test_returns_entries_most_recent_first(self, client, auth_headers):
        await _seed_record("svc-a")
        dhs: DeployHistoryStore = server_mod.app.state.deploy_history_store
        await dhs.append(
            "svc-a",
            DeployHistoryEntry(
                digest="sha256:abc",
                image_ref="repo:v1",
                timestamp=1000.0,
                source="manual",
                previous_digest="",
            ),
        )
        await dhs.append(
            "svc-a",
            DeployHistoryEntry(
                digest="sha256:def",
                image_ref="repo:v2",
                timestamp=2000.0,
                source="caretaker",
                previous_digest="sha256:abc",
            ),
        )

        resp = await client.get("/services/svc-a/history", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "svc-a"
        assert len(body["entries"]) == 2
        assert body["entries"][0]["digest"] == "sha256:def"
        assert body["entries"][0]["source"] == "caretaker"
        assert body["entries"][1]["digest"] == "sha256:abc"
        assert body["entries"][1]["source"] == "manual"

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_service(self, client, auth_headers):
        resp = await client.get("/services/unknown/history", headers=auth_headers)
        assert resp.status_code == 404


class TestDeployRecordsHistory:
    @pytest.mark.asyncio
    async def test_manual_deploy_records_history(self, client, auth_headers, registry):
        await _seed_record("svc-a")
        server_mod.app.state.registry = registry
        server_mod.app.state.component_config_store = MagicMock(
            spec=ComponentConfigStore
        )
        server_mod.app.state.component_config_store.get = MagicMock(
            return_value=_make_config()
        )

        outcome = DeployOutcome(
            deployed_digest="sha256:new",
            previous_digest="sha256:current",
            state=ServiceState.RUNNING,
        )

        with patch.object(
            server_mod.app.state.backend, "deploy", AsyncMock(return_value=outcome)
        ):
            resp = await client.post("/services/svc-a/deploy", headers=auth_headers)
            assert resp.status_code == 202
            body = resp.json()
            assert "job_id" in body

            # Let the background task run to completion inside the patch context.
            import asyncio

            await asyncio.sleep(0)

        # Verify history was recorded
        dhs: DeployHistoryStore = server_mod.app.state.deploy_history_store
        entries = await dhs.list("svc-a")
        assert len(entries) == 1
        assert entries[0].digest == "sha256:new"
        assert entries[0].source == "manual"
        assert entries[0].previous_digest == "sha256:current"


class TestRollbackWithDigest:
    @pytest.mark.asyncio
    async def test_rollback_with_recorded_digest(
        self, client, auth_headers, registry, component_config_store
    ):
        await _seed_record("svc-a")
        server_mod.app.state.registry = registry
        server_mod.app.state.component_config_store = component_config_store

        # Pre-seed history with an older digest
        dhs: DeployHistoryStore = server_mod.app.state.deploy_history_store
        await dhs.append(
            "svc-a",
            DeployHistoryEntry(
                digest="sha256:old",
                image_ref="repo@sha256:old",
                timestamp=1000.0,
                source="manual",
                previous_digest="",
            ),
        )

        deploy_outcome = DeployOutcome(
            deployed_digest="sha256:old",
            previous_digest="sha256:current",
            state=ServiceState.RUNNING,
        )

        with patch.object(
            server_mod.app.state.backend,
            "deploy",
            AsyncMock(return_value=deploy_outcome),
        ):
            resp = await client.post(
                "/services/svc-a/rollback",
                json={"digest": "sha256:old"},
                headers=auth_headers,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["rolled_back_to_digest"] == "sha256:old"

        # Verify history was recorded with source="rollback"
        entries = await dhs.list("svc-a")
        # Should have the original entry + the rollback entry
        assert len(entries) == 2
        assert entries[0].source == "rollback"
        assert entries[0].digest == "sha256:old"

    @pytest.mark.asyncio
    async def test_rollback_with_unrecorded_digest_returns_409(
        self, client, auth_headers, registry, component_config_store
    ):
        await _seed_record("svc-a")
        server_mod.app.state.registry = registry
        server_mod.app.state.component_config_store = component_config_store

        resp = await client.post(
            "/services/svc-a/rollback",
            json={"digest": "sha256:never-deployed"},
            headers=auth_headers,
        )

        assert resp.status_code == 409
        body = resp.json()
        assert "digest not in deploy history" in body["error"]

    @pytest.mark.asyncio
    async def test_rollback_no_body_preserves_original_behavior(
        self, client, auth_headers, registry, component_config_store
    ):
        await _seed_record("svc-a")
        server_mod.app.state.registry = registry
        server_mod.app.state.component_config_store = component_config_store

        from robotsix_central_deploy.lifecycle.models import RollbackOutcome

        outcome = RollbackOutcome(
            deployed_digest="sha256:previous",
            state=ServiceState.RUNNING,
        )

        with patch.object(
            server_mod.app.state.backend,
            "rollback",
            AsyncMock(return_value=outcome),
        ):
            resp = await client.post(
                "/services/svc-a/rollback",
                # No body — original one-step swap
                headers=auth_headers,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["rolled_back_to_digest"] == "sha256:previous"

    @pytest.mark.asyncio
    async def test_rollback_no_body_409_when_no_previous(
        self, client, auth_headers, registry, component_config_store
    ):
        """One-step rollback without body still requires previous_image_digest."""
        store = server_mod.app.state.store
        record = ServiceRecord(
            name="svc-a",
            state=ServiceState.RUNNING,
            image="repo:v1",
        )
        await store.put(record)
        server_mod.app.state.registry = registry
        server_mod.app.state.component_config_store = component_config_store

        resp = await client.post(
            "/services/svc-a/rollback",
            headers=auth_headers,
        )

        assert resp.status_code == 409
        body = resp.json()
        assert "No prior image digest" in body["error"]
