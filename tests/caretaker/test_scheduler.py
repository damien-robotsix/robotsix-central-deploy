"""Tests for caretaker/scheduler.py."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from robotsix_http import RetryClient

from robotsix_central_deploy.caretaker.models import CaretakerReport
from robotsix_central_deploy.caretaker.scheduler import CaretakerScheduler

# Import lifecycle.models first to break the circular import through
# lifecycle → deps → caretaker.scheduler (deps.CaretakerScheduler at module-level).
from robotsix_central_deploy.lifecycle.models import (
    ComponentInspect,
    SelfInspect,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.deploy_history_store import DeployHistoryStore
from robotsix_central_deploy.registry.loader import ComponentRegistry


def _register_mill(ccs, mill_id="mill", port=9999):
    """Make the mocked config store discover a mill component under *mill_id*."""
    mill_cfg = MagicMock()
    mill_cfg.container_name = mill_id
    mill_cfg.ports = [MagicMock(host=port, container=port)]
    default_cfg = MagicMock()
    default_cfg.repo_id = "my-repo"
    default_cfg.auto_update_enabled = True
    default_cfg.consumed_scopes = []
    ccs.get = MagicMock(
        side_effect=lambda cid: mill_cfg if cid == mill_id else default_cfg
    )


@pytest.fixture
def scheduler_fixtures(tmp_path):
    from robotsix_central_deploy.lifecycle.config import LifecycleConfig
    from robotsix_central_deploy.registry.settings_store import (
        SystemSettings,
        SystemSettingsStore,
    )

    config = LifecycleConfig(  # type: ignore[call-arg]
        system_settings_path=str(tmp_path / "settings.json"),
        disk_path="/",
    )
    backend = MagicMock()
    # A bare MagicMock returns a non-awaitable for inspect_self, which the
    # scheduler now treats as "self-identity unknown" and fails closed on.
    # Give it a real answer naming a container none of these tests own, so
    # phase_update runs normally without ever matching a record as self.
    backend.inspect_self = AsyncMock(
        return_value=SelfInspect(
            container_id="self-cid",
            container_name="robotsix-central-deploy-central-deploy-1",
            image_ref="ghcr.io/damien-robotsix/robotsix-central-deploy:main",
            running_digest="sha256:running",
            networks=[],
        )
    )
    registry = ComponentRegistry([])
    service_store = MagicMock()
    component_config_store = MagicMock(spec=ComponentConfigStore)
    volume_audit_scheduler = MagicMock()
    volume_audit_scheduler.run_once = AsyncMock()
    volume_audit_scheduler.get_audit_response = MagicMock(
        return_value=MagicMock(volumes=[], recent_findings=[])
    )

    settings_store = SystemSettingsStore(tmp_path / "settings.json")
    # Seed default settings — write the file directly to avoid
    # needing an event loop in a sync fixture.
    import json

    settings_path = tmp_path / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            SystemSettings(
                caretaker_enabled=True, caretaker_interval_hours=24
            ).model_dump()
        ),
        encoding="utf-8",
    )

    http_client = MagicMock(spec=RetryClient)
    deploy_history_store = DeployHistoryStore(tmp_path / "deploy_history.json")
    env_store = MagicMock()
    env_store.get_merged_env = AsyncMock(
        side_effect=lambda name, base: dict(base) if isinstance(base, dict) else {}
    )
    env_store.resolve_consumed_credentials = AsyncMock(return_value={})

    scheduler = CaretakerScheduler(
        config=config,
        backend=backend,
        registry=registry,
        service_store=service_store,
        component_config_store=component_config_store,
        volume_audit_scheduler=volume_audit_scheduler,
        settings_store=settings_store,
        http_client=http_client,
        deploy_history_store=deploy_history_store,
        env_store=env_store,
    )
    return scheduler, service_store, backend, component_config_store, http_client


class TestScheduler:
    @pytest.mark.asyncio
    async def test_run_once_calls_all_phases(self, scheduler_fixtures):
        scheduler, store, backend, _ccs, _http = scheduler_fixtures

        # No records → health/update produce nothing, volumes runs
        store.list_all = AsyncMock(return_value=[])
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))

        report = await scheduler.run_once()
        assert "update" in report.phases_run
        assert "health" in report.phases_run
        assert "volumes" in report.phases_run
        assert isinstance(report, CaretakerReport)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("inspect_self_mock", "expect_known"),
        [
            (AsyncMock(return_value=None), False),
            (AsyncMock(side_effect=RuntimeError("socket proxy blew up")), False),
            (AsyncMock(side_effect=NotImplementedError), True),
        ],
        ids=["returns-none", "raises", "unsupported-backend"],
    )
    async def test_self_identity_propagated_to_phase_update(
        self, scheduler_fixtures, monkeypatch, inspect_self_mock, expect_known
    ):
        """A failed self-lookup must mark identity unknown for phase_update.

        Regression (2026-07-31 outage): inspect_self returned None and the
        scheduler silently passed an empty container name, which phase_update
        treated as "no self to skip" and deployed central-deploy over itself.
        NotImplementedError is different — the backend has no self container
        at all, so auto-update stays enabled.
        """
        scheduler, store, backend, _ccs, _http = scheduler_fixtures
        store.list_all = AsyncMock(return_value=[])
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))
        backend.inspect_self = inspect_self_mock

        captured: dict[str, object] = {}

        async def _fake_phase_update(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return []

        sched_mod = sys.modules["robotsix_central_deploy.caretaker.scheduler"]
        monkeypatch.setattr(sched_mod, "phase_update", _fake_phase_update)

        await scheduler.run_once()

        # self_identity_known is the last positional argument.
        assert captured["args"][-1] is expect_known

    @pytest.mark.asyncio
    async def test_findings_are_recorded_locally_never_ingested(
        self, scheduler_fixtures, monkeypatch, caplog
    ):
        """Findings land in the log + local JSONL and NEVER become tickets.

        The caretaker's ticket-filing capability was removed outright
        (operator decision, 2026-09-01: it "just updates the containers") —
        no mill probe, no ingest POST, regardless of repo_id.
        """
        scheduler, store, backend, _ccs, http = scheduler_fixtures

        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))
        monkeypatch.setattr(
            "shutil.disk_usage",
            lambda path: (10**12, 9 * 10**11, 10**11),
        )

        # A stopped tracked service makes phase_health emit a finding WITH a
        # repo_id — exactly the case the old code forwarded to the mill.
        record = ServiceRecord(name="svc", image="repo:v1", repo_id="my-repo")
        store.list_all = AsyncMock(return_value=[record])
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.STOPPED, health="")
        )
        http.post = AsyncMock()
        http.get = AsyncMock()

        with caplog.at_level("WARNING"):
            report = await scheduler.run_once()

        assert len(report.findings) >= 1
        # Recorded locally: JSONL written and a WARNING logged per finding.
        assert scheduler._findings_path.exists()
        assert any("caretaker finding" in r.message for r in caplog.records)
        # Never a ticket: no HTTP at all from the reporting step.
        http.post.assert_not_awaited()
        http.get.assert_not_awaited()
        # The mill-reporting counters are gone from the report model.
        assert not hasattr(report, "mill_reported")
        assert not hasattr(report, "mill_reachable")

    @pytest.mark.asyncio
    async def test_image_auto_prune_after_update(self, scheduler_fixtures):
        from robotsix_central_deploy.lifecycle.models import DeployOutcome
        from robotsix_central_deploy.registry.settings_store import SystemSettings

        scheduler, store, backend, ccs, http = scheduler_fixtures
        await scheduler._settings_store.put(
            SystemSettings(caretaker_enabled=True, image_auto_prune=True)
        )
        _register_mill(ccs)
        http.post = AsyncMock(return_value=MagicMock(is_success=True))
        http.get = AsyncMock(return_value=MagicMock(is_success=True))

        record = ServiceRecord(
            name="svc",
            image="repo:v1",
            repo_id="my-repo",
            update_available=True,
            latest_registry_digest="sha256:new",
        )
        store.list_all = AsyncMock(return_value=[record])
        store.put = AsyncMock()
        backend.deploy = AsyncMock(
            return_value=DeployOutcome(
                deployed_digest="sha256:new",
                previous_digest="sha256:old",
                state=ServiceState.RUNNING,
            )
        )
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.RUNNING, health="healthy")
        )
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))
        backend.prune_images = AsyncMock(return_value=1234)

        await scheduler.run_once()

        backend.prune_images.assert_awaited_once()
        protected = backend.prune_images.call_args[0][0]
        assert "sha256:new" in protected
        assert "sha256:old" in protected

    @pytest.mark.asyncio
    async def test_image_prune_runs_without_applied_update(self, scheduler_fixtures):
        """Prune runs every cycle: images also accumulate from pulls that
        bypass the deploy path, so it must not be gated on applied updates."""
        from robotsix_central_deploy.registry.settings_store import SystemSettings

        scheduler, store, backend, ccs, http = scheduler_fixtures
        await scheduler._settings_store.put(
            SystemSettings(caretaker_enabled=True, image_auto_prune=True)
        )
        _register_mill(ccs)
        http.post = AsyncMock(return_value=MagicMock(is_success=True))
        http.get = AsyncMock(return_value=MagicMock(is_success=True))

        record = ServiceRecord(
            name="svc",
            image="repo:v1",
            repo_id="my-repo",
            update_available=False,
            deployed_image_digest="sha256:current",
        )
        store.list_all = AsyncMock(return_value=[record])
        store.put = AsyncMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.RUNNING, health="healthy")
        )
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))
        backend.prune_images = AsyncMock(return_value=0)

        await scheduler.run_once()

        backend.prune_images.assert_awaited_once()
        protected = backend.prune_images.call_args[0][0]
        assert "sha256:current" in protected

    @pytest.mark.asyncio
    async def test_no_image_prune_when_disabled(self, scheduler_fixtures):
        from robotsix_central_deploy.lifecycle.models import DeployOutcome

        scheduler, store, backend, ccs, http = scheduler_fixtures
        _register_mill(ccs)
        http.post = AsyncMock(return_value=MagicMock(is_success=True))
        http.get = AsyncMock(return_value=MagicMock(is_success=True))

        record = ServiceRecord(
            name="svc",
            image="repo:v1",
            repo_id="my-repo",
            update_available=True,
            latest_registry_digest="sha256:new",
        )
        store.list_all = AsyncMock(return_value=[record])
        store.put = AsyncMock()
        backend.deploy = AsyncMock(
            return_value=DeployOutcome(
                deployed_digest="sha256:new",
                previous_digest="sha256:old",
                state=ServiceState.RUNNING,
            )
        )
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.RUNNING, health="healthy")
        )
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))
        backend.prune_images = AsyncMock()

        await scheduler.run_once()
        backend.prune_images.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_status(self, scheduler_fixtures):
        scheduler, _store, _backend, _ccs, _http = scheduler_fixtures
        status = await scheduler.get_status()
        assert "enabled" in status
        assert "last_run_at" in status
        assert "last_report" in status
        assert "mill_reachable" not in status
        assert status["enabled"] is True


class TestSelfUpdate:
    """End-of-pass detached self-update of the plane itself."""

    SELF_CONTAINER = "robotsix-central-deploy-central-deploy-1"

    @staticmethod
    def _self_record(**overrides) -> ServiceRecord:
        base = {
            "name": "central-deploy",
            "image": "ghcr.io/damien-robotsix/robotsix-central-deploy:main",
            "container_name": TestSelfUpdate.SELF_CONTAINER,
            "deployed_image_digest": "sha256:running",
            "update_available": True,
            "latest_registry_digest": "sha256:new",
        }
        base.update(overrides)
        return ServiceRecord(**base)

    @pytest.mark.asyncio
    async def test_triggered_at_end_of_pass(self, scheduler_fixtures):
        """A pending self update launches the detached updater (POST /system/update path)."""
        from robotsix_central_deploy.caretaker.models import FindingKind

        scheduler, store, backend, _ccs, _http = scheduler_fixtures
        store.list_all = AsyncMock(return_value=[self._self_record()])
        store.put = AsyncMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.RUNNING, health="healthy")
        )
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))
        backend.trigger_self_update = AsyncMock(return_value="updater-cid")

        report = await scheduler.run_once()

        backend.trigger_self_update.assert_awaited_once()
        # Same signature/args as POST /system/update: target, watchtower
        # image, docker host URL, docker api version.
        args = backend.trigger_self_update.call_args[0]
        assert isinstance(args[0], SelfInspect)
        assert args[1] == scheduler._config.self_update_watchtower_image
        assert args[2] == scheduler._config.docker_socket_url
        assert args[3] == scheduler._config.self_update_docker_api_version
        assert "self-update" in report.phases_run
        kinds = [f.kind for f in report.findings]
        assert FindingKind.SELF_UPDATE_TRIGGERED in kinds
        assert scheduler._last_self_update_digest == "sha256:new"

    @pytest.mark.asyncio
    async def test_gated_by_component_auto_update_flag(self, scheduler_fixtures):
        """Self-update follows the central-deploy component's unified
        per-component auto-update flag — the SAME predicate every other
        component's auto-update goes through — with no
        ``caretaker_self_update_enabled`` special case.

        Toggling the flag enables/disables the plane's self-update.
        """
        scheduler, store, backend, ccs, _http = scheduler_fixtures
        store.put = AsyncMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.RUNNING, health="healthy")
        )
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))

        # Flag OFF for the central-deploy component → no self-update.
        store.list_all = AsyncMock(return_value=[self._self_record()])
        ccs.get = MagicMock(return_value=MagicMock(auto_update_enabled=False))
        backend.trigger_self_update = AsyncMock()
        await scheduler.run_once()
        backend.trigger_self_update.assert_not_awaited()
        assert scheduler._last_self_update_digest is None
        ccs.get.assert_any_call("central-deploy")

        # Flag ON → self-update triggers through the detached updater.
        store.list_all = AsyncMock(return_value=[self._self_record()])
        ccs.get = MagicMock(return_value=MagicMock(auto_update_enabled=True))
        backend.trigger_self_update = AsyncMock(return_value="updater-cid")
        await scheduler.run_once()
        backend.trigger_self_update.assert_awaited_once()
        assert scheduler._last_self_update_digest == "sha256:new"

    @pytest.mark.asyncio
    async def test_skipped_without_pending_update(self, scheduler_fixtures):
        scheduler, store, backend, _ccs, _http = scheduler_fixtures
        store.list_all = AsyncMock(
            return_value=[
                self._self_record(update_available=False, latest_registry_digest="")
            ]
        )
        store.put = AsyncMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.RUNNING, health="healthy")
        )
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))
        backend.trigger_self_update = AsyncMock()

        await scheduler.run_once()

        backend.trigger_self_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skipped_when_digest_unchanged(self, scheduler_fixtures):
        """update_available true but the pending digest equals the running one."""
        scheduler, store, backend, _ccs, _http = scheduler_fixtures
        store.list_all = AsyncMock(
            return_value=[self._self_record(latest_registry_digest="sha256:running")]
        )
        store.put = AsyncMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.RUNNING, health="healthy")
        )
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))
        backend.trigger_self_update = AsyncMock()

        await scheduler.run_once()

        backend.trigger_self_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_retrigger_when_running_digest_up_to_date(
        self, scheduler_fixtures
    ):
        """A stale store must not re-trigger after a successful self-update.

        Watchtower swaps the container out-of-band and never persists a new
        deployed_image_digest, and the caretaker does not run the status-poll
        path that would refresh it. So after a successful update the store can
        still hold the OLD deployed_image_digest with update_available=True.
        The guard must compare against the LIVE running digest from
        inspect_self (not the stale store value) so a completed update is never
        relaunched — even on a fresh boot where the per-boot loop guard resets.
        """
        scheduler, store, backend, _ccs, _http = scheduler_fixtures
        # Store is stale: deployed_image_digest still points at the old image
        # and update_available never cleared.
        store.list_all = AsyncMock(
            return_value=[
                self._self_record(
                    deployed_image_digest="sha256:old",
                    latest_registry_digest="sha256:new",
                )
            ]
        )
        store.put = AsyncMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.RUNNING, health="healthy")
        )
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))
        backend.trigger_self_update = AsyncMock()
        # The live container already runs the new image (watchtower finished).
        backend.inspect_self = AsyncMock(
            return_value=SelfInspect(
                container_id="self-cid",
                container_name=self.SELF_CONTAINER,
                image_ref="ghcr.io/damien-robotsix/robotsix-central-deploy:main",
                running_digest="sha256:new",
                networks=[],
            )
        )

        await scheduler.run_once()

        # Pending == live running digest → no re-trigger despite the stale store.
        backend.trigger_self_update.assert_not_awaited()
        assert scheduler._last_self_update_digest is None

    @pytest.mark.asyncio
    async def test_loop_guard_once_per_boot(self, scheduler_fixtures):
        """A stale update_available flag must not restart the plane twice per boot.

        Acceptance: a deliberately broken update_available flag cannot cause
        repeated self-restarts within one boot.
        """
        scheduler, store, backend, _ccs, _http = scheduler_fixtures
        store.put = AsyncMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.RUNNING, health="healthy")
        )
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))
        backend.trigger_self_update = AsyncMock(return_value="updater-cid")

        # Same pending digest across two passes in the same boot — the flag
        # never clears (e.g. the updater lost the race).
        store.list_all = AsyncMock(return_value=[self._self_record()])

        await scheduler.run_once()
        await scheduler.run_once()

        backend.trigger_self_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_launch_failure_emits_update_failed(self, scheduler_fixtures):
        from robotsix_central_deploy.caretaker.models import FindingKind

        scheduler, store, backend, _ccs, _http = scheduler_fixtures
        store.list_all = AsyncMock(return_value=[self._self_record()])
        store.put = AsyncMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.RUNNING, health="healthy")
        )
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))
        backend.trigger_self_update = AsyncMock(
            side_effect=RuntimeError("daemon unreachable")
        )

        report = await scheduler.run_once()

        kinds = [f.kind for f in report.findings]
        assert FindingKind.UPDATE_FAILED in kinds
        # Not recorded as an attempt: a transient launch failure retries next pass.
        assert scheduler._last_self_update_digest is None
