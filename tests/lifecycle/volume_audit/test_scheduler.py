import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_central_deploy.registry.models import ComponentConfig
from robotsix_central_deploy.lifecycle.volume_audit.scheduler import (
    VolumeAuditScheduler,
)


def _make_scheduler(
    tmp_path: Path, enabled: bool = True
) -> tuple[VolumeAuditScheduler, MagicMock, MagicMock]:
    """Build a VolumeAuditScheduler with mocked backend and component config store."""
    from robotsix_central_deploy.lifecycle.config import LifecycleConfig

    cfg = LifecycleConfig(
        volume_audit_enabled=enabled,
        volume_audit_snapshot_path=str(tmp_path / "snapshots.json"),
        volume_audit_findings_path=str(tmp_path / "findings.json"),
        volume_audit_growth_threshold_pct=10.0,
        volume_audit_min_delta_bytes=10_485_760,
    )
    backend = MagicMock()
    backend.measure_volume_bytes = AsyncMock(return_value=1_000_000)
    comp_config_store = MagicMock()
    sched = VolumeAuditScheduler(cfg, backend, comp_config_store)
    return sched, backend, comp_config_store


class TestVolumeAuditScheduler:
    @pytest.mark.asyncio
    async def test_run_once_no_volumes_returns_empty(self, tmp_path):
        sched, backend, store = _make_scheduler(tmp_path)
        store.all.return_value = []
        records = await sched.run_once()
        assert records == []
        backend.measure_volume_bytes.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_once_measures_all_named_volumes(self, tmp_path):
        sched, backend, store = _make_scheduler(tmp_path)
        comp = ComponentConfig(
            id="mycomp",
            image="ghcr.io/test/image:latest",
            container_name="mycomp",
            named_volumes=["vol-a", "vol-b"],
        )
        store.all.return_value = [comp]
        records = await sched.run_once()
        assert len(records) == 2
        assert {r.volume_name for r in records} == {"vol-a", "vol-b"}
        assert backend.measure_volume_bytes.call_count == 2

    @pytest.mark.asyncio
    async def test_run_once_emits_finding_on_threshold_breach(
        self, tmp_path, monkeypatch
    ):
        """When a scan pass detects threshold-level growth, report_finding is called."""
        import robotsix_central_deploy.lifecycle.volume_audit.scheduler as sched_mod

        called_with = []

        async def _fake_report(finding, path, board_client=None):
            called_with.append(finding)

        monkeypatch.setattr(sched_mod, "report_finding", _fake_report)

        sched, backend, store = _make_scheduler(tmp_path)
        comp = ComponentConfig(
            id="svc",
            image="ghcr.io/test/image:latest",
            container_name="svc",
            named_volumes=["vol"],
        )
        store.all.return_value = [comp]

        # Seed a previous snapshot small enough that the mock's 20 MiB return will
        # breach thresholds.
        backend.measure_volume_bytes = AsyncMock(return_value=20_000_000)  # 20 MiB

        # Write a prior snapshot at 1 MiB so delta = 19 MiB > 10 MiB (min_delta)
        # and pct ≫ 10%
        snap_path = tmp_path / "snapshots.json"
        snap_path.write_text(
            json.dumps(
                {
                    "vol": {
                        "volume_name": "vol",
                        "component_id": "svc",
                        "measured_at": "2025-01-01T00:00:00+00:00",
                        "size_bytes": 1_000_000,  # 1 MiB
                    }
                }
            )
        )

        await sched.run_once()
        assert len(called_with) == 1
        assert called_with[0].volume_name == "vol"

    def test_get_audit_response_before_scan(self, tmp_path):
        """Before any scan, response has empty volumes and None last_scan_at."""
        sched, _, _ = _make_scheduler(tmp_path)
        resp = sched.get_audit_response()
        assert resp.enabled is True
        assert resp.last_scan_at is None
        assert resp.volumes == []
