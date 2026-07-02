"""Tests for caretaker/scheduler.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from robotsix_central_deploy.caretaker.models import CaretakerReport
from robotsix_central_deploy.caretaker.scheduler import CaretakerScheduler
from robotsix_central_deploy.lifecycle.models import (
    ComponentInspect,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.loader import ComponentRegistry


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

    http_client = MagicMock(spec=httpx.AsyncClient)

    scheduler = CaretakerScheduler(
        config=config,
        backend=backend,
        registry=registry,
        service_store=service_store,
        component_config_store=component_config_store,
        volume_audit_scheduler=volume_audit_scheduler,
        settings_store=settings_store,
        http_client=http_client,
    )
    return scheduler, service_store, backend, component_config_store, http_client


class TestScheduler:
    @pytest.mark.asyncio
    async def test_run_once_calls_all_phases(self, scheduler_fixtures):
        scheduler, store, backend, ccs, http = scheduler_fixtures

        # No records → health/update produce nothing, volumes runs
        store.list_all = AsyncMock(return_value=[])
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))

        report = await scheduler.run_once()
        assert "update" in report.phases_run
        assert "health" in report.phases_run
        assert "volumes" in report.phases_run
        assert isinstance(report, CaretakerReport)

    @pytest.mark.asyncio
    async def test_run_once_routes_to_mill(self, scheduler_fixtures, monkeypatch):
        scheduler, store, backend, ccs, http = scheduler_fixtures

        store.list_all = AsyncMock(return_value=[])
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))

        # Force mill URL via env
        monkeypatch.setenv("MILL_INGEST_URL", "http://localhost:9999")
        http.post = AsyncMock(return_value=MagicMock(is_success=True))

        # Make phase_health emit a finding with repo_id
        record = ServiceRecord(
            name="svc",
            image="repo:v1",
            repo_id="my-repo",
        )
        store.list_all = AsyncMock(return_value=[record])
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.STOPPED, health="")
        )

        report = await scheduler.run_once()
        assert report.mill_reported >= 1
        assert report.local_only == 0
        assert report.mill_reachable is True

    @pytest.mark.asyncio
    async def test_run_once_mill_unreachable_fallback(
        self, scheduler_fixtures, monkeypatch
    ):
        scheduler, store, backend, ccs, http = scheduler_fixtures

        monkeypatch.setenv("MILL_INGEST_URL", "http://localhost:9999")
        http.post = AsyncMock(return_value=MagicMock(is_success=False, status_code=500))

        record = ServiceRecord(
            name="svc",
            image="repo:v1",
            repo_id="my-repo",
        )
        store.list_all = AsyncMock(return_value=[record])
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.STOPPED, health="")
        )
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))

        report = await scheduler.run_once()
        assert report.mill_reported == 0
        assert report.local_only >= 1
        assert report.mill_reachable is False

    @pytest.mark.asyncio
    async def test_run_once_no_findings_mill_reachable(
        self, scheduler_fixtures, monkeypatch
    ):
        scheduler, store, backend, ccs, http = scheduler_fixtures

        monkeypatch.setenv("MILL_INGEST_URL", "http://localhost:9999")
        store.list_all = AsyncMock(return_value=[])
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))

        report = await scheduler.run_once()
        # No findings → mill_reachable=True (no ingest attempted means we don't know
        # it's unreachable)
        assert report.mill_reachable is True
        assert report.mill_reported == 0
        assert report.local_only == 0

    @pytest.mark.asyncio
    async def test_run_once_untracked_local_only(self, scheduler_fixtures, monkeypatch):
        scheduler, store, backend, ccs, http = scheduler_fixtures

        monkeypatch.setenv("MILL_INGEST_URL", "http://localhost:9999")
        http.post = AsyncMock(return_value=MagicMock(is_success=True))

        # Component with no repo_id → local only
        record = ServiceRecord(
            name="svc",
            image="repo:v1",
            repo_id="",  # untracked
        )
        store.list_all = AsyncMock(return_value=[record])
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.STOPPED, health="")
        )
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))

        report = await scheduler.run_once()
        assert report.local_only >= 1
        assert report.mill_reported == 0
        http.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_repo_id_in_ingest_payload(self, scheduler_fixtures, monkeypatch):
        scheduler, store, backend, ccs, http = scheduler_fixtures

        monkeypatch.setenv("MILL_INGEST_URL", "http://localhost:9999")
        http.post = AsyncMock(return_value=MagicMock(is_success=True))

        record = ServiceRecord(
            name="svc",
            image="repo:v1",
            repo_id="specific-repo",
        )
        store.list_all = AsyncMock(return_value=[record])
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.STOPPED, health="")
        )
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))

        _report = await scheduler.run_once()
        # Verify the ingest payload contained the right repo_id
        # Find the call with repo_id="specific-repo"
        found = False
        for call in http.post.call_args_list:
            args, kwargs = call
            json_body = kwargs.get("json", {})
            if json_body.get("repo_id") == "specific-repo":
                found = True
                assert json_body["kind"] == "health"
                break
        assert found, "Expected ingest call with repo_id='specific-repo'"

    @pytest.mark.asyncio
    async def test_get_status(self, scheduler_fixtures):
        scheduler, store, backend, ccs, http = scheduler_fixtures
        status = await scheduler.get_status()
        assert "enabled" in status
        assert "last_run_at" in status
        assert "mill_reachable" in status
        assert "last_report" in status
        assert status["enabled"] is True
