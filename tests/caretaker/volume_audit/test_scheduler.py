import asyncio
import json
from datetime import timedelta
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
    async def test_run_once_backend_measure_failure_surfaced_as_finding(
        self, tmp_path, monkeypatch
    ):
        """A measure failure is reported as a finding; the scan still completes."""
        reported = []

        async def _fake_report(finding, path):
            reported.append(finding)

        monkeypatch.setattr(sched_mod, "report_finding", _fake_report)

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

        records = await sched.run_once()

        # Scan completed rather than raising; the failed volume yields no record.
        assert records == []
        # The failure is surfaced as a finding referencing the volume + error.
        assert len(reported) == 1
        assert reported[0].volume_name == "vol"
        assert "docker down" in reported[0].detail
        # A snapshot file is written (scan reached persistence).
        assert (tmp_path / "snapshots.json").exists()

    @pytest.mark.asyncio
    async def test_run_once_measure_failure_preserves_baseline_and_continues(
        self, tmp_path, monkeypatch
    ):
        """One volume failing does not stop measurement of the others, and the
        failed volume's prior baseline snapshot is preserved."""
        reported = []

        async def _fake_report(finding, path):
            reported.append(finding)

        monkeypatch.setattr(sched_mod, "report_finding", _fake_report)

        sched, backend, store = _make_scheduler(tmp_path)
        comp = ComponentConfig(
            id="svc",
            image="ghcr.io/test/image:latest",
            container_name="svc",
            named_volumes=["vol-ok", "vol-bad"],
        )
        store.all.return_value = [comp]

        async def _measure(name):
            if name == "vol-bad":
                raise RuntimeError("measure boom")
            return 5_000_000

        backend.measure_volume_bytes = AsyncMock(side_effect=_measure)

        # Seed a prior baseline for the failing volume.
        snap_path = tmp_path / "snapshots.json"
        snap_path.write_text(
            json.dumps(
                {
                    "vol-bad": {
                        "volume_name": "vol-bad",
                        "component_id": "svc",
                        "measured_at": "2025-01-01T00:00:00+00:00",
                        "size_bytes": 1_234_567,
                    }
                }
            )
        )

        records = await sched.run_once()

        # The healthy volume was still measured.
        assert {r.volume_name for r in records} == {"vol-ok"}
        # The failing volume produced exactly one measure finding.
        assert [f.volume_name for f in reported] == ["vol-bad"]
        # Its prior baseline is carried forward in the saved snapshot.
        saved = json.loads(snap_path.read_text())
        assert saved["vol-bad"]["size_bytes"] == 1_234_567

    @pytest.mark.asyncio
    async def test_run_once_measure_failed_surfaces_finding(
        self, tmp_path, monkeypatch
    ):
        """When measure_volume_bytes returns None (helper timeout / stream
        cut), a measurement-failed finding is reported instead of silently
        recording a bogus 0, and the last-known size is carried forward."""
        reported = []

        async def _fake_report(finding, path):
            reported.append(finding)

        monkeypatch.setattr(sched_mod, "report_finding", _fake_report)

        sched, backend, store = _make_scheduler(tmp_path)
        comp = ComponentConfig(
            id="svc",
            image="ghcr.io/test/image:latest",
            container_name="svc",
            named_volumes=["vol"],
        )
        store.all.return_value = [comp]
        backend.measure_volume_bytes = AsyncMock(return_value=None)

        # Seed a previous snapshot so the last-known size can carry forward.
        snap_path = tmp_path / "snapshots.json"
        snap_path.write_text(
            json.dumps(
                {
                    "vol": {
                        "volume_name": "vol",
                        "component_id": "svc",
                        "measured_at": "2025-01-01T00:00:00+00:00",
                        "size_bytes": 7_000_000,
                    }
                }
            )
        )

        records = await sched.run_once()

        # A measurement-failed finding is surfaced (not just a log warning).
        assert len(reported) == 1
        assert reported[0].kind == "measurement_failed"
        assert reported[0].volume_name == "vol"
        assert "could not be measured" in reported[0].detail

        # The volume stays visible with its last-known size (no bogus 0).
        assert len(records) == 1
        assert records[0].volume_name == "vol"
        assert records[0].size_bytes == 7_000_000

    @pytest.mark.asyncio
    async def test_failed_measurement_backs_off_then_resumes(
        self, tmp_path, monkeypatch
    ):
        """Regression (2026-09-07): the hourly scan re-ran the du helper on
        mill-mill-data every pass right after it had timed out, grinding IO
        on a loaded host.  After a failed measurement (None) the volume is
        not measured again inside the backoff window: the previous snapshot
        is carried forward with no new finding.  Once the window has passed
        the volume is measured again."""
        reported = []

        async def _fake_report(finding, path):
            reported.append(finding)

        monkeypatch.setattr(sched_mod, "report_finding", _fake_report)

        sched, backend, store = _make_scheduler(tmp_path)
        comp = ComponentConfig(
            id="svc",
            image="ghcr.io/test/image:latest",
            container_name="svc",
            named_volumes=["vol-big", "vol-ok"],
        )
        store.all.return_value = [comp]

        async def _measure(name):
            if name == "vol-big":
                return None
            return 5_000_000

        backend.measure_volume_bytes = AsyncMock(side_effect=_measure)
        (tmp_path / "snapshots.json").write_text(
            json.dumps(
                {
                    "vol-big": {
                        "volume_name": "vol-big",
                        "component_id": "svc",
                        "measured_at": "2025-01-01T00:00:00+00:00",
                        "size_bytes": 7_000_000,
                    }
                }
            )
        )

        # Scan 1: measurement fails -> finding + backoff armed.
        records = await sched.run_once()
        assert [f.kind for f in reported] == ["measurement_failed"]
        assert reported[0].volume_name == "vol-big"
        assert backend.measure_volume_bytes.await_count == 2
        assert "vol-big" in sched._measure_backoff_until
        assert {r.volume_name: r.size_bytes for r in records} == {
            "vol-big": 7_000_000,
            "vol-ok": 5_000_000,
        }

        # Scan 2 (inside the window): vol-big is NOT measured, its previous
        # snapshot is carried forward, and no second finding is emitted.
        backend.measure_volume_bytes.reset_mock()
        records = await sched.run_once()
        measured = [c.args[0] for c in backend.measure_volume_bytes.await_args_list]
        assert measured == ["vol-ok"]
        assert len(reported) == 1
        assert {r.volume_name: r.size_bytes for r in records} == {
            "vol-big": 7_000_000,
            "vol-ok": 5_000_000,
        }
        saved = json.loads((tmp_path / "snapshots.json").read_text())
        assert saved["vol-big"]["size_bytes"] == 7_000_000

        # Scan 3 (window elapsed): the volume is measured again.
        sched._measure_backoff_until["vol-big"] -= timedelta(
            seconds=sched._MEASURE_BACKOFF_S + 1
        )
        backend.measure_volume_bytes = AsyncMock(return_value=8_000_000)
        records = await sched.run_once()
        measured = [c.args[0] for c in backend.measure_volume_bytes.await_args_list]
        assert sorted(measured) == ["vol-big", "vol-ok"]
        assert "vol-big" not in sched._measure_backoff_until
        assert {r.volume_name: r.size_bytes for r in records} == {
            "vol-big": 8_000_000,
            "vol-ok": 8_000_000,
        }
        # Only the original measurement_failed finding; 7 MB -> 8 MB is under
        # the 10 MiB min-delta guard.
        assert len(reported) == 1

    @pytest.mark.asyncio
    async def test_raising_measurement_backs_off_too(self, tmp_path, monkeypatch):
        """A measurement that raises arms the same backoff as one that
        returns None."""
        monkeypatch.setattr(sched_mod, "report_finding", AsyncMock())
        sched, backend, store = _make_scheduler(tmp_path)
        comp = ComponentConfig(
            id="svc",
            image="ghcr.io/test/image:latest",
            container_name="svc",
            named_volumes=["vol"],
        )
        store.all.return_value = [comp]
        backend.measure_volume_bytes = AsyncMock(side_effect=RuntimeError("boom"))

        await sched.run_once()
        assert "vol" in sched._measure_backoff_until

        backend.measure_volume_bytes.reset_mock()
        await sched.run_once()
        backend.measure_volume_bytes.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_backoff_measures_every_scan(self, tmp_path, monkeypatch):
        """With the backoff constant disabled, a failed volume is retried on
        the very next scan (the pre-fix behaviour)."""
        monkeypatch.setattr(sched_mod, "report_finding", AsyncMock())
        monkeypatch.setattr(sched_mod.VolumeAuditScheduler, "_MEASURE_BACKOFF_S", 0)
        sched, backend, store = _make_scheduler(tmp_path)
        comp = ComponentConfig(
            id="svc",
            image="ghcr.io/test/image:latest",
            container_name="svc",
            named_volumes=["vol"],
        )
        store.all.return_value = [comp]
        backend.measure_volume_bytes = AsyncMock(return_value=None)

        await sched.run_once()
        await sched.run_once()
        assert backend.measure_volume_bytes.await_count == 2

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


# ---------------------------------------------------------------------------
# slow-volume cadence
# ---------------------------------------------------------------------------


def _write_snapshot(tmp_path: Path, vol: str, age: timedelta, took: float | None):
    from datetime import UTC, datetime

    snap = {
        "volume_name": vol,
        "component_id": "mycomp",
        "measured_at": (datetime.now(tz=UTC) - age).isoformat(),
        "size_bytes": 40_000_000_000,
    }
    if took is not None:
        snap["measured_in_seconds"] = took
    (tmp_path / "snapshots.json").write_text(json.dumps({vol: snap}), encoding="utf-8")


def _one_volume_store(store, vol: str):
    store.all.return_value = [
        ComponentConfig(
            id="mycomp",
            image="ghcr.io/test/image:latest",
            container_name="mycomp",
            named_volumes=[vol],
        )
    ]


@pytest.mark.asyncio
async def test_slow_volume_is_not_remeasured_within_the_slow_interval(tmp_path):
    """A volume whose last du took longer than the slow threshold is carried
    forward (not re-measured) while its snapshot is younger than the slow
    interval — including on the scan a restart fires immediately
    (mill-mill-data, 2026-09-09: 10-30 min of du every hour, 40 % IO stall)."""
    sched, backend, store = _make_scheduler(tmp_path)
    _one_volume_store(store, "mill-mill-data")
    _write_snapshot(tmp_path, "mill-mill-data", timedelta(hours=1), took=900.0)

    records = await sched.run_once()

    backend.measure_volume_bytes.assert_not_called()
    assert len(records) == 1
    assert records[0].size_bytes == 40_000_000_000
    saved = json.loads((tmp_path / "snapshots.json").read_text())
    assert saved["mill-mill-data"]["measured_in_seconds"] == 900.0


@pytest.mark.asyncio
async def test_slow_volume_is_remeasured_once_the_slow_interval_elapsed(tmp_path):
    sched, backend, store = _make_scheduler(tmp_path)
    _one_volume_store(store, "mill-mill-data")
    _write_snapshot(tmp_path, "mill-mill-data", timedelta(hours=7), took=900.0)

    await sched.run_once()

    backend.measure_volume_bytes.assert_awaited_once_with("mill-mill-data")
    saved = json.loads((tmp_path / "snapshots.json").read_text())
    assert saved["mill-mill-data"]["size_bytes"] == 1_000_000
    assert saved["mill-mill-data"]["measured_in_seconds"] is not None


@pytest.mark.asyncio
async def test_fast_volume_keeps_the_hourly_cadence(tmp_path):
    """A quick measurement (or a legacy snapshot without the duration field)
    is re-measured every scan exactly as before."""
    sched, backend, store = _make_scheduler(tmp_path)
    _one_volume_store(store, "chat-chat-config")
    _write_snapshot(tmp_path, "chat-chat-config", timedelta(minutes=30), took=2.5)
    await sched.run_once()
    assert backend.measure_volume_bytes.await_count == 1

    _write_snapshot(tmp_path, "chat-chat-config", timedelta(minutes=30), took=None)
    await sched.run_once()
    assert backend.measure_volume_bytes.await_count == 2
