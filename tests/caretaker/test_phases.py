"""Tests for caretaker/phases.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# Import lifecycle models FIRST to break the circular import:
#   caretaker.phases → lifecycle.models → lifecycle.__init__ → app → deps → caretaker.scheduler → caretaker.phases
# By loading lifecycle.models first (which pulls in the full lifecycle package
# including deps/scheduler), caretaker.phases finds lifecycle already loaded
# and the chain does not restart.
from robotsix_central_deploy.lifecycle.models import (
    ComponentInspect,
    DeployOutcome,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import ComponentConfig
from robotsix_central_deploy.lifecycle.volume_audit.models import (
    AuditFinding,
    VolumeAuditResponse,
)

from robotsix_central_deploy.caretaker.models import FindingKind
from robotsix_central_deploy.caretaker.phases import (
    phase_health,
    phase_update,
    phase_volumes,
)

from datetime import datetime, timezone


def _make_record(name="svc", update_available=True):
    return ServiceRecord(
        name=name,
        image="repo:v1",
        deployed_image_digest="sha256:abc",
        latest_registry_digest="sha256:def",
        update_available=update_available,
        repo_id="test-repo",
    )


def _make_config(id="svc", caretaker_auto_update=True, repo_id="test-repo"):
    return ComponentConfig(
        id=id,
        image="repo:v1",
        container_name=id,
        caretaker_auto_update=caretaker_auto_update,
        repo_id=repo_id,
    )


class TestPhaseUpdate:
    @pytest.mark.asyncio
    async def test_deploys_eligible(self):
        store = MagicMock()
        record = _make_record()
        store.list_all = AsyncMock(return_value=[record])
        store.put = AsyncMock()

        backend = MagicMock()
        outcome = DeployOutcome(
            deployed_digest="sha256:def",
            previous_digest="sha256:abc",
            state=ServiceState.RUNNING,
        )
        backend.deploy = AsyncMock(return_value=outcome)

        registry = ComponentRegistry([])
        ccs = MagicMock(spec=ComponentConfigStore)
        cfg = _make_config()
        ccs.get = MagicMock(return_value=cfg)

        findings = await phase_update(registry, store, backend, ccs)
        assert len(findings) == 1
        assert findings[0].kind == FindingKind.UPDATE_APPLIED
        assert findings[0].repo_id == "test-repo"
        backend.deploy.assert_called_once()
        # image_ref must be a pullable repo@digest reference — a bare
        # "sha256:…" digest resolves as repository "sha256" and 404s.
        image_ref = backend.deploy.call_args[0][2]
        assert image_ref == "repo@sha256:def"
        assert record.update_available is False

    @pytest.mark.asyncio
    async def test_deploy_falls_back_to_tag_without_digest(self):
        store = MagicMock()
        record = _make_record()
        record.latest_registry_digest = ""
        store.list_all = AsyncMock(return_value=[record])
        store.put = AsyncMock()
        backend = MagicMock()
        backend.deploy = AsyncMock(
            return_value=DeployOutcome(
                deployed_digest="sha256:def",
                previous_digest="sha256:abc",
                state=ServiceState.RUNNING,
            )
        )
        registry = ComponentRegistry([])
        ccs = MagicMock(spec=ComponentConfigStore)
        ccs.get = MagicMock(return_value=_make_config())

        await phase_update(registry, store, backend, ccs)
        assert backend.deploy.call_args[0][2] == "repo:v1"

    @pytest.mark.asyncio
    async def test_skips_opted_out(self):
        store = MagicMock()
        record = _make_record()
        store.list_all = AsyncMock(return_value=[record])
        backend = MagicMock()
        backend.deploy = AsyncMock()
        registry = ComponentRegistry([])
        ccs = MagicMock(spec=ComponentConfigStore)
        ccs.get = MagicMock(return_value=_make_config(caretaker_auto_update=False))

        findings = await phase_update(registry, store, backend, ccs)
        assert len(findings) == 0
        backend.deploy.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_no_update(self):
        store = MagicMock()
        record = _make_record(update_available=False)
        store.list_all = AsyncMock(return_value=[record])
        backend = MagicMock()
        backend.deploy = AsyncMock()
        registry = ComponentRegistry([])
        ccs = MagicMock(spec=ComponentConfigStore)
        ccs.get = MagicMock(return_value=_make_config())

        findings = await phase_update(registry, store, backend, ccs)
        assert len(findings) == 0
        backend.deploy.assert_not_called()

    @pytest.mark.asyncio
    async def test_emits_failed_on_exception(self):
        store = MagicMock()
        record = _make_record()
        store.list_all = AsyncMock(return_value=[record])
        backend = MagicMock()
        backend.deploy = AsyncMock(side_effect=RuntimeError("boom"))
        registry = ComponentRegistry([])
        ccs = MagicMock(spec=ComponentConfigStore)
        ccs.get = MagicMock(return_value=_make_config())

        findings = await phase_update(registry, store, backend, ccs)
        assert len(findings) == 1
        assert findings[0].kind == FindingKind.UPDATE_FAILED
        assert findings[0].severity == "error"
        assert "boom" in findings[0].detail

    @pytest.mark.asyncio
    async def test_skips_siblings(self):
        store = MagicMock()
        record = _make_record(name="svc-sib")
        record.component_id = "svc"  # sibling
        store.list_all = AsyncMock(return_value=[record])
        backend = MagicMock()
        backend.deploy = AsyncMock()
        registry = ComponentRegistry([])
        ccs = MagicMock(spec=ComponentConfigStore)

        findings = await phase_update(registry, store, backend, ccs)
        assert len(findings) == 0
        backend.deploy.assert_not_called()


class TestPhaseHealth:
    @pytest.mark.asyncio
    async def test_finding_stopped(self):
        store = MagicMock()
        record = _make_record(update_available=False)
        record.repo_id = "test-repo"
        store.list_all = AsyncMock(return_value=[record])
        backend = MagicMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.STOPPED, health="")
        )
        registry = ComponentRegistry([])
        ccs = MagicMock(spec=ComponentConfigStore)

        findings = await phase_health(registry, store, backend, ccs)
        assert len(findings) == 1
        assert findings[0].kind == FindingKind.HEALTH
        assert findings[0].component_id == "svc"
        assert findings[0].repo_id == "test-repo"

    @pytest.mark.asyncio
    async def test_finding_unhealthy(self):
        store = MagicMock()
        record = _make_record(update_available=False)
        store.list_all = AsyncMock(return_value=[record])
        backend = MagicMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(
                state=ServiceState.RUNNING, health="unhealthy"
            )
        )
        registry = ComponentRegistry([])
        ccs = MagicMock(spec=ComponentConfigStore)

        findings = await phase_health(registry, store, backend, ccs)
        assert len(findings) == 1
        assert findings[0].kind == FindingKind.HEALTH
        assert "unhealthy" in findings[0].detail

    @pytest.mark.asyncio
    async def test_no_finding_running(self):
        store = MagicMock()
        record = _make_record(update_available=False)
        store.list_all = AsyncMock(return_value=[record])
        backend = MagicMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.RUNNING, health="")
        )
        registry = ComponentRegistry([])
        ccs = MagicMock(spec=ComponentConfigStore)

        findings = await phase_health(registry, store, backend, ccs)
        assert len(findings) == 0

    @pytest.mark.asyncio
    async def test_sibling_checked(self):
        store = MagicMock()
        parent = _make_record(name="parent", update_available=False)
        parent.repo_id = "parent-repo"
        sibling = ServiceRecord(
            name="parent-sib",
            image="repo:v1",
            component_id="parent",
        )
        store.list_all = AsyncMock(return_value=[sibling])
        store.get = AsyncMock(return_value=parent)

        backend = MagicMock()
        backend.status = AsyncMock(
            return_value=ComponentInspect(state=ServiceState.STOPPED, health="")
        )
        registry = ComponentRegistry([])
        ccs = MagicMock(spec=ComponentConfigStore)

        findings = await phase_health(registry, store, backend, ccs)
        assert len(findings) == 1
        assert findings[0].component_id == "parent-sib"
        # repo_id from parent
        assert findings[0].repo_id == "parent-repo"


class TestPhaseVolumes:
    @pytest.mark.asyncio
    async def test_growth(self):
        from robotsix_central_deploy.lifecycle.config import LifecycleConfig
        from robotsix_central_deploy.registry.settings_store import SystemSettings

        vas = MagicMock()
        vas.run_once = AsyncMock()
        finding = AuditFinding(
            volume_name="vol1",
            component_id="svc",
            finding_at=datetime.now(tz=timezone.utc),
            size_bytes=1000,
            delta_bytes=500,
            growth_pct=50.0,
            detail="Grew 50%",
        )
        vas.get_audit_response = MagicMock(
            return_value=VolumeAuditResponse(
                enabled=True,
                volumes=[],
                recent_findings=[finding],
            )
        )

        backend = MagicMock()
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))

        ccs = MagicMock(spec=ComponentConfigStore)
        comp = ComponentConfig(
            id="svc",
            image="x",
            container_name="x",
            named_volumes=["vol1"],
            stateful_volumes=["vol1"],
            repo_id="test-repo",
        )
        ccs.all = MagicMock(return_value=[comp])
        ccs.get = MagicMock(return_value=comp)

        config = LifecycleConfig(disk_path="/")  # type: ignore[call-arg]
        settings = SystemSettings(disk_warn_pct=10.0)

        findings = await phase_volumes(vas, backend, ccs, config, settings)
        growth = [f for f in findings if f.kind == FindingKind.VOLUME_GROWTH]
        assert len(growth) >= 1
        assert growth[0].repo_id == "test-repo"

    @pytest.mark.asyncio
    async def test_orphan(self):
        from robotsix_central_deploy.lifecycle.config import LifecycleConfig
        from robotsix_central_deploy.registry.settings_store import SystemSettings
        from robotsix_central_deploy.lifecycle.models import VolumeStat, DockerDfStats

        vas = MagicMock()
        vas.run_once = AsyncMock()
        vas.get_audit_response = MagicMock(
            return_value=VolumeAuditResponse(
                enabled=True, volumes=[], recent_findings=[]
            )
        )

        backend = MagicMock()
        backend.disk_df = AsyncMock(
            return_value=DockerDfStats(
                volumes=[VolumeStat(name="orphan_vol", size_bytes=42, in_use=False)]
            )
        )

        ccs = MagicMock(spec=ComponentConfigStore)
        ccs.all = MagicMock(return_value=[])

        config = LifecycleConfig(disk_path="/")  # type: ignore[call-arg]
        settings = SystemSettings(disk_warn_pct=10.0)

        findings = await phase_volumes(vas, backend, ccs, config, settings)
        orphans = [f for f in findings if f.kind == FindingKind.VOLUME_ORPHAN]
        assert len(orphans) >= 1
        assert orphans[0].component_id == ""
        assert orphans[0].repo_id == ""

    @pytest.mark.asyncio
    async def test_disk_warning(self, monkeypatch, tmp_path):
        from robotsix_central_deploy.lifecycle.config import LifecycleConfig
        from robotsix_central_deploy.registry.settings_store import SystemSettings

        vas = MagicMock()
        vas.run_once = AsyncMock()
        vas.get_audit_response = MagicMock(
            return_value=VolumeAuditResponse(
                enabled=True, volumes=[], recent_findings=[]
            )
        )

        backend = MagicMock()
        backend.disk_df = AsyncMock(return_value=MagicMock(volumes=[]))

        ccs = MagicMock(spec=ComponentConfigStore)
        ccs.all = MagicMock(return_value=[])

        config = LifecycleConfig(disk_path=str(tmp_path))  # type: ignore[call-arg]
        settings = SystemSettings(
            disk_warn_pct=99.0
        )  # Very high threshold — almost everything triggers

        import shutil

        # Mock disk_usage to return low free space
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda path: MagicMock(total=1000, used=995, free=5),
        )

        findings = await phase_volumes(vas, backend, ccs, config, settings)
        disk = [f for f in findings if f.kind == FindingKind.DISK]
        assert len(disk) >= 1
        assert "99.5" in disk[0].detail or "99." in disk[0].detail  # pct used
        assert disk[0].component_id == ""
