import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import robotsix_central_deploy.caretaker.volume_audit.scheduler as sched_mod
from robotsix_central_deploy.registry.models import ComponentConfig


def _make_scheduler(
    tmp_path: Path, enabled: bool = True
) -> tuple[sched_mod.VolumeAuditScheduler, MagicMock, MagicMock]:
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
    sched = sched_mod.VolumeAuditScheduler(cfg, backend, comp_config_store)
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
        called_with = []

        async def _fake_report(finding, path):
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

    # ------------------------------------------------------------------
    # Error-path tests
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_run_once_backend_measure_failure(self, tmp_path):
        """When measure_volume_bytes raises, the exception propagates."""
        sched, backend, store = _make_scheduler(tmp_path)
        comp = ComponentConfig(
            id="svc",
            image="ghcr.io/test/image:latest",
            container_name="svc",
            named_volumes=["vol"],
        )
        store.all.return_value = [comp]
        backend.measure_volume_bytes = AsyncMock(
            side_effect=RuntimeError("docker down")
        )
        with pytest.raises(RuntimeError, match="docker down"):
            await sched.run_once()
        # Snapshots should NOT be saved (error before persistence)
        assert not (tmp_path / "snapshots.json").exists()

    @pytest.mark.asyncio
    async def test_run_once_corrupt_snapshot_file(self, tmp_path):
        """Corrupt (non-JSON) snapshot file falls back to empty dict."""
        sched, _backend, store = _make_scheduler(tmp_path)
        comp = ComponentConfig(
            id="svc",
            image="ghcr.io/test/image:latest",
            container_name="svc",
            named_volumes=["vol"],
        )
        store.all.return_value = [comp]
        (tmp_path / "snapshots.json").write_text("not json {{{")
        records = await sched.run_once()
        assert len(records) == 1
        # New snapshot should be written over the corrupt one
        assert json.loads((tmp_path / "snapshots.json").read_text())

    @pytest.mark.asyncio
    async def test_run_once_snapshot_wrong_schema(self, tmp_path):
        """Snapshot file with valid JSON but wrong schema falls back to empty."""
        sched, _backend, store = _make_scheduler(tmp_path)
        comp = ComponentConfig(
            id="svc",
            image="ghcr.io/test/image:latest",
            container_name="svc",
            named_volumes=["vol"],
        )
        store.all.return_value = [comp]
        (tmp_path / "snapshots.json").write_text(
            json.dumps({"vol": {"wrong_field": 123}})
        )
        records = await sched.run_once()
        assert len(records) == 1

    @pytest.mark.asyncio
    async def test_loop_cancellation_propagates(self, tmp_path, monkeypatch):
        """loop() re-raises CancelledError when the task is cancelled."""
        sched, _backend, store = _make_scheduler(tmp_path)
        store.all.return_value = []

        call_count = 0

        async def _fake_run_once():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError()
            return []

        monkeypatch.setattr(sched, "run_once", _fake_run_once)

        with pytest.raises(asyncio.CancelledError):
            await sched.loop(interval_seconds=0)

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_loop_error_skip_continues(self, tmp_path, monkeypatch):
        """loop() catches Exceptions from run_once(), logs them,
        and continues to the next iteration."""
        sched, _backend, store = _make_scheduler(tmp_path)
        store.all.return_value = []

        call_count = 0

        async def _fake_run_once():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("scan failed")
            if call_count >= 3:
                raise asyncio.CancelledError()
            return []

        monkeypatch.setattr(sched, "run_once", _fake_run_once)

        with pytest.raises(asyncio.CancelledError):
            await sched.loop(interval_seconds=0)

        # Should have called run_once 3 times: error → success → cancel
        assert call_count == 3


async def test_run_once_passes_are_serialized(tmp_path):
    """The background loop and the caretaker's phase_volumes both scan at
    startup; concurrent passes put two du helpers on the same large volume
    (2026-09-02, mill-mill-data). run_once must serialize."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from robotsix_central_deploy.caretaker.volume_audit.scheduler import (
        VolumeAuditScheduler,
    )

    config = MagicMock()
    config.volume_audit_snapshot_path = str(tmp_path / "snap.json")
    config.volume_audit_findings_path = str(tmp_path / "findings.json")
    config.volume_audit_growth_threshold_pct = 10.0
    config.volume_audit_min_delta_bytes = 1
    config.board_api_url = ""

    comp = MagicMock()
    comp.id = "svc"
    comp.named_volumes = ["vol-a"]
    store = MagicMock()
    store.all.return_value = [comp]

    in_flight = 0
    max_in_flight = 0

    async def _measure(_name):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return 123

    backend = MagicMock()
    backend.measure_volume_bytes = AsyncMock(side_effect=_measure)

    scheduler = VolumeAuditScheduler(config, backend, store)
    await asyncio.gather(scheduler.run_once(), scheduler.run_once())

    assert backend.measure_volume_bytes.await_count == 2  # both passes ran
    assert max_in_flight == 1  # never concurrently
