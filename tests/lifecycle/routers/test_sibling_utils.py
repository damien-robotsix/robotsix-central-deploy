"""Unit tests for _sibling_utils.py — best-effort sibling fan-out helpers.

These tests use monkeypatched ``_get_sibling_pairs`` and fake backends
to exercise the continue-on-failure contract and the env-merge fallback
without touching Docker or the full ASGI stack.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_central_deploy.lifecycle.models import (
    DeployOutcome,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.lifecycle.routers._sibling_utils import (
    _fanout_siblings_best_effort,
    _fanout_siblings_deploy_best_effort,
)
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.registry.models import ComponentConfig, ServiceConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_svc_cfg(
    service_key: str = "redis",
    *,
    container_name: str = "",
    image: str = "redis:7",
    env: dict[str, str] | None = None,
) -> ServiceConfig:
    return ServiceConfig(
        service_key=service_key,
        container_name=container_name or f"svc-{service_key}",
        image=image,
        env=env or {},
    )


def _make_component_config(
    id: str = "svc-a",
    *,
    siblings: list[ServiceConfig] | None = None,
) -> ComponentConfig:
    return ComponentConfig(
        id=id,
        image=f"{id}:latest",
        container_name=id,
        siblings=siblings or [],
    )


def _make_service_record(
    name: str,
    *,
    state: ServiceState = ServiceState.STOPPED,
    image: str = "dummy:latest",
) -> ServiceRecord:
    return ServiceRecord(name=name, state=state, image=image)


# ---------------------------------------------------------------------------
# _fanout_siblings_best_effort
# ---------------------------------------------------------------------------


class TestFanoutSiblingsBestEffort:
    """Unit tests for ``_fanout_siblings_best_effort``."""

    async def test_all_succeed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both siblings' backend actions succeed — both records updated."""
        store = InMemoryStore()
        backend = MagicMock()
        backend.start = AsyncMock(return_value=ServiceState.RUNNING)

        redis_cfg = _make_svc_cfg("redis")
        redis_rec = _make_service_record("svc-a-redis")
        db_cfg = _make_svc_cfg("db", container_name="svc-db", image="postgres:15")
        db_rec = _make_service_record("svc-a-db")

        cfg = _make_component_config(siblings=[redis_cfg, db_cfg])

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers._sibling_utils._get_sibling_pairs",
            AsyncMock(return_value=[(redis_cfg, redis_rec), (db_cfg, db_rec)]),
        )

        await _fanout_siblings_best_effort("svc-a", cfg, store, backend, "start")

        assert backend.start.call_count == 2
        assert backend.start.await_args_list[0].args[0] is redis_rec
        assert backend.start.await_args_list[1].args[0] is db_rec

        # Records updated in store
        updated_redis = await store.get("svc-a-redis")
        assert updated_redis is not None
        assert updated_redis.state == ServiceState.RUNNING

        updated_db = await store.get("svc-a-db")
        assert updated_db is not None
        assert updated_db.state == ServiceState.RUNNING

    async def test_one_fails_others_continue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First sibling raises — logged and skipped; second still succeeds."""
        store = InMemoryStore()
        backend = MagicMock()

        async def _start(record: ServiceRecord) -> ServiceState:
            if record.name == "svc-a-redis":
                raise RuntimeError("boom")
            return ServiceState.RUNNING

        backend.start = _start

        redis_cfg = _make_svc_cfg("redis")
        redis_rec = _make_service_record("svc-a-redis")
        db_cfg = _make_svc_cfg("db", container_name="svc-db", image="postgres:15")
        db_rec = _make_service_record("svc-a-db")

        cfg = _make_component_config(siblings=[redis_cfg, db_cfg])

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers._sibling_utils._get_sibling_pairs",
            AsyncMock(return_value=[(redis_cfg, redis_rec), (db_cfg, db_rec)]),
        )

        await _fanout_siblings_best_effort("svc-a", cfg, store, backend, "start")

        # First sibling NOT updated (it raised before store.put)
        redis_after = await store.get("svc-a-redis")
        assert redis_after is None

        # Second sibling updated successfully
        db_after = await store.get("svc-a-db")
        assert db_after is not None
        assert db_after.state == ServiceState.RUNNING

    @pytest.mark.parametrize("action", ["start", "stop", "restart"])
    async def test_action_dispatch(
        self, monkeypatch: pytest.MonkeyPatch, action: str
    ) -> None:
        """Correct backend method is called for each action string."""
        store = InMemoryStore()
        backend = MagicMock()
        backend.start = AsyncMock(return_value=ServiceState.RUNNING)
        backend.stop = AsyncMock(return_value=ServiceState.STOPPED)
        backend.restart = AsyncMock(return_value=ServiceState.RUNNING)

        sib_cfg = _make_svc_cfg("redis")
        sib_rec = _make_service_record("svc-a-redis")
        cfg = _make_component_config(siblings=[sib_cfg])

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers._sibling_utils._get_sibling_pairs",
            AsyncMock(return_value=[(sib_cfg, sib_rec)]),
        )

        await _fanout_siblings_best_effort("svc-a", cfg, store, backend, action)

        # Only the targeted method was called
        method = getattr(backend, action)
        assert method.call_count == 1

        for other in {"start", "stop", "restart"} - {action}:
            assert getattr(backend, other).call_count == 0

    async def test_no_siblings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty sibling list — no-op, no backend calls."""
        store = InMemoryStore()
        backend = MagicMock()
        backend.start = AsyncMock(return_value=ServiceState.RUNNING)

        cfg = _make_component_config(siblings=[])

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers._sibling_utils._get_sibling_pairs",
            AsyncMock(return_value=[]),
        )

        await _fanout_siblings_best_effort("svc-a", cfg, store, backend, "start")

        backend.start.assert_not_called()

    async def test_all_fail_still_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When every sibling fails the loop still exits cleanly (no re-raise)."""
        store = InMemoryStore()
        backend = MagicMock()

        async def _start(record: ServiceRecord) -> ServiceState:
            raise RuntimeError("all broken")

        backend.start = _start

        sib_cfg = _make_svc_cfg("redis")
        sib_rec = _make_service_record("svc-a-redis")
        cfg = _make_component_config(siblings=[sib_cfg])

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers._sibling_utils._get_sibling_pairs",
            AsyncMock(return_value=[(sib_cfg, sib_rec)]),
        )

        # Must not raise
        await _fanout_siblings_best_effort("svc-a", cfg, store, backend, "start")

        # No record persisted because store.put was never reached
        assert await store.get("svc-a-redis") is None


# ---------------------------------------------------------------------------
# _fanout_siblings_deploy_best_effort
# ---------------------------------------------------------------------------


class TestFanoutSiblingsDeployBestEffort:
    """Unit tests for ``_fanout_siblings_deploy_best_effort``."""

    async def test_all_succeed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both siblings deploy successfully — records updated, names returned."""
        store = InMemoryStore()
        backend = MagicMock()
        backend.deploy = AsyncMock(
            return_value=DeployOutcome(
                deployed_digest="sha256:new",
                previous_digest="sha256:old",
                state=ServiceState.RUNNING,
            )
        )

        redis_cfg = _make_svc_cfg("redis")
        redis_rec = _make_service_record("svc-a-redis")
        db_cfg = _make_svc_cfg("db", container_name="svc-db", image="postgres:15")
        db_rec = _make_service_record("svc-a-db")

        cfg = _make_component_config(siblings=[redis_cfg, db_cfg])

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers._sibling_utils._get_sibling_pairs",
            AsyncMock(return_value=[(redis_cfg, redis_rec), (db_cfg, db_rec)]),
        )

        deployed = await _fanout_siblings_deploy_best_effort(
            "svc-a", cfg, store, backend, "test-deploy"
        )

        assert deployed == ["svc-a-redis", "svc-a-db"]
        assert backend.deploy.call_count == 2

        # Redis record updated
        redis_after = await store.get("svc-a-redis")
        assert redis_after is not None
        assert redis_after.state == ServiceState.RUNNING
        assert redis_after.deployed_image_digest == "sha256:new"
        assert redis_after.previous_image_digest == "sha256:old"
        assert redis_after.image == "redis:7"

        # DB record updated
        db_after = await store.get("svc-a-db")
        assert db_after is not None
        assert db_after.state == ServiceState.RUNNING

    async def test_one_fails_others_continue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First sibling deploy raises — skipped; second succeeds and is returned."""
        store = InMemoryStore()
        backend = MagicMock()

        async def _deploy(
            service: ServiceRecord, config: ComponentConfig, image_ref: str
        ) -> DeployOutcome:
            if service.name == "svc-a-redis":
                raise RuntimeError("pull failed")
            return DeployOutcome(
                deployed_digest="sha256:ok",
                previous_digest="sha256:prior",
                state=ServiceState.RUNNING,
            )

        backend.deploy = _deploy

        redis_cfg = _make_svc_cfg("redis")
        redis_rec = _make_service_record("svc-a-redis")
        db_cfg = _make_svc_cfg("db", container_name="svc-db", image="postgres:15")
        db_rec = _make_service_record("svc-a-db")

        cfg = _make_component_config(siblings=[redis_cfg, db_cfg])

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers._sibling_utils._get_sibling_pairs",
            AsyncMock(return_value=[(redis_cfg, redis_rec), (db_cfg, db_rec)]),
        )

        deployed = await _fanout_siblings_deploy_best_effort(
            "svc-a", cfg, store, backend, "test-deploy"
        )

        assert deployed == ["svc-a-db"]
        assert await store.get("svc-a-redis") is None
        db_after = await store.get("svc-a-db")
        assert db_after is not None
        assert db_after.deployed_image_digest == "sha256:ok"

    async def test_env_store_merge_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When env_store is provided, get_merged_env is called with correct args."""
        store = InMemoryStore()
        backend = MagicMock()
        backend.deploy = AsyncMock(
            return_value=DeployOutcome(
                deployed_digest="sha256:new",
                previous_digest="sha256:old",
                state=ServiceState.RUNNING,
            )
        )

        env_store = MagicMock()
        env_store.get_merged_env = AsyncMock(return_value={"MERGED_KEY": "merged_val"})

        sib_cfg = _make_svc_cfg("redis", env={"STATIC_KEY": "static_val"})
        sib_rec = _make_service_record("svc-a-redis")
        cfg = _make_component_config(siblings=[sib_cfg])

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers._sibling_utils._get_sibling_pairs",
            AsyncMock(return_value=[(sib_cfg, sib_rec)]),
        )

        await _fanout_siblings_deploy_best_effort(
            "svc-a", cfg, store, backend, "test-deploy", env_store=env_store
        )

        env_store.get_merged_env.assert_awaited_once_with(
            "svc-a-redis", {"STATIC_KEY": "static_val"}
        )

        # The effective config passed to deploy should contain the merged env
        _, effective_cfg, _ = backend.deploy.await_args_list[0].args
        assert effective_cfg.env == {"MERGED_KEY": "merged_val"}

    async def test_env_fallback_no_env_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When env_store is None, sib_cfg.env is used directly."""
        store = InMemoryStore()
        backend = MagicMock()
        backend.deploy = AsyncMock(
            return_value=DeployOutcome(
                deployed_digest="sha256:new",
                previous_digest="sha256:old",
                state=ServiceState.RUNNING,
            )
        )

        sib_cfg = _make_svc_cfg("redis", env={"STATIC_KEY": "static_val"})
        sib_rec = _make_service_record("svc-a-redis")
        cfg = _make_component_config(siblings=[sib_cfg])

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers._sibling_utils._get_sibling_pairs",
            AsyncMock(return_value=[(sib_cfg, sib_rec)]),
        )

        await _fanout_siblings_deploy_best_effort(
            "svc-a", cfg, store, backend, "test-deploy", env_store=None
        )

        # The effective config passed to deploy should contain static env
        _, effective_cfg, _ = backend.deploy.await_args_list[0].args
        assert effective_cfg.env == {"STATIC_KEY": "static_val"}

    async def test_no_siblings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty sibling list — returns empty list, no backend calls."""
        store = InMemoryStore()
        backend = MagicMock()
        backend.deploy = AsyncMock()

        cfg = _make_component_config(siblings=[])

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers._sibling_utils._get_sibling_pairs",
            AsyncMock(return_value=[]),
        )

        deployed = await _fanout_siblings_deploy_best_effort(
            "svc-a", cfg, store, backend, "test-deploy"
        )

        assert deployed == []
        backend.deploy.assert_not_called()

    async def test_effective_config_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The effective config passed to deploy contains all sibling-specific fields."""
        store = InMemoryStore()
        backend = MagicMock()
        backend.deploy = AsyncMock(
            return_value=DeployOutcome(
                deployed_digest="sha256:new",
                previous_digest="sha256:old",
                state=ServiceState.RUNNING,
            )
        )

        sib_cfg = _make_svc_cfg("redis", env={"KEY": "val"})
        sib_rec = _make_service_record("svc-a-redis")
        cfg = _make_component_config(siblings=[sib_cfg])

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers._sibling_utils._get_sibling_pairs",
            AsyncMock(return_value=[(sib_cfg, sib_rec)]),
        )

        await _fanout_siblings_deploy_best_effort(
            "svc-a", cfg, store, backend, "test-deploy"
        )

        _, effective_cfg, image_ref = backend.deploy.await_args_list[0].args

        assert effective_cfg.id == "svc-a-redis"
        assert effective_cfg.image == sib_cfg.image
        assert effective_cfg.container_name == sib_cfg.container_name
        assert effective_cfg.ports == sib_cfg.ports
        assert effective_cfg.mounts == sib_cfg.mounts
        assert effective_cfg.health_check == sib_cfg.health_check
        assert effective_cfg.claude_mount == sib_cfg.claude_mount
        assert effective_cfg.claude_mount_path == sib_cfg.claude_mount_path
        assert effective_cfg.host_docker_sock == sib_cfg.host_docker_sock
        assert effective_cfg.command == sib_cfg.command
        assert effective_cfg.entrypoint == sib_cfg.entrypoint
        assert effective_cfg.tmpfs == sib_cfg.tmpfs
        assert effective_cfg.mem_limit == sib_cfg.mem_limit
        assert effective_cfg.user == sib_cfg.user
        assert image_ref == sib_cfg.image

    async def test_all_fail_still_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When every sibling deploy fails the loop exits cleanly, returns empty list."""
        store = InMemoryStore()
        backend = MagicMock()

        async def _deploy(
            service: ServiceRecord, config: ComponentConfig, image_ref: str
        ) -> DeployOutcome:
            raise RuntimeError("all broken")

        backend.deploy = _deploy

        sib_cfg = _make_svc_cfg("redis")
        sib_rec = _make_service_record("svc-a-redis")
        cfg = _make_component_config(siblings=[sib_cfg])

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers._sibling_utils._get_sibling_pairs",
            AsyncMock(return_value=[(sib_cfg, sib_rec)]),
        )

        deployed = await _fanout_siblings_deploy_best_effort(
            "svc-a", cfg, store, backend, "test-deploy"
        )

        assert deployed == []
        assert await store.get("svc-a-redis") is None
