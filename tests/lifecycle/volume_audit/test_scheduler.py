import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_central_deploy.registry.models import ComponentConfig
import robotsix_central_deploy.lifecycle.volume_audit.scheduler as sched_mod


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
        sched, backend, store = _make_scheduler(tmp_path)
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
        sched, backend, store = _make_scheduler(tmp_path)
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
    async def test_run_once_board_client_creation_failure(self, tmp_path, monkeypatch):
        """When _maybe_create_board_client raises, the exception propagates
        and snapshots are NOT saved."""
        sched, backend, store = _make_scheduler(tmp_path)
        comp = ComponentConfig(
            id="svc",
            image="ghcr.io/test/image:latest",
            container_name="svc",
            named_volumes=["vol"],
        )
        store.all.return_value = [comp]

        async def _raise(*args, **kwargs):
            raise RuntimeError("board init failed")

        monkeypatch.setattr(sched, "_maybe_create_board_client", _raise)

        with pytest.raises(RuntimeError, match="board init failed"):
            await sched.run_once()
        assert not (tmp_path / "snapshots.json").exists()

    @pytest.mark.asyncio
    async def test_run_once_board_client_close_failure(self, tmp_path, monkeypatch):
        """When board_client.close() raises, it is caught and logged,
        not propagated — scan completes and snapshots are saved."""
        sched, backend, store = _make_scheduler(tmp_path)
        comp = ComponentConfig(
            id="svc",
            image="ghcr.io/test/image:latest",
            container_name="svc",
            named_volumes=["vol"],
        )
        store.all.return_value = [comp]

        mock_client = MagicMock()
        mock_client.close = AsyncMock(side_effect=RuntimeError("close failed"))

        async def _fake_create_client():
            return mock_client

        monkeypatch.setattr(sched, "_maybe_create_board_client", _fake_create_client)

        # Should complete without raising
        records = await sched.run_once()
        assert len(records) == 1
        mock_client.close.assert_awaited_once()
        # Snapshots saved despite close failure
        assert (tmp_path / "snapshots.json").exists()

    @pytest.mark.asyncio
    async def test_run_once_multiple_findings_reuses_board_client(
        self, tmp_path, monkeypatch
    ):
        """When multiple volumes breach thresholds, the same board client
        instance is passed to every report_finding call."""
        called_with = []
        mock_client = MagicMock()

        async def _fake_report(finding, path, board_client=None):
            called_with.append((finding, board_client))

        async def _fake_create_client():
            return mock_client

        sched, backend, store = _make_scheduler(tmp_path)

        monkeypatch.setattr(sched_mod, "report_finding", _fake_report)
        monkeypatch.setattr(sched, "_maybe_create_board_client", _fake_create_client)
        store.all.return_value = [
            ComponentConfig(
                id="svc",
                image="ghcr.io/test/image:latest",
                container_name="svc",
                named_volumes=["vol-a", "vol-b"],
            )
        ]

        backend.measure_volume_bytes = AsyncMock(return_value=50_000_000)

        snap_path = tmp_path / "snapshots.json"
        snap_path.write_text(
            json.dumps(
                {
                    "vol-a": {
                        "volume_name": "vol-a",
                        "component_id": "svc",
                        "measured_at": "2025-01-01T00:00:00+00:00",
                        "size_bytes": 1_000_000,
                    },
                    "vol-b": {
                        "volume_name": "vol-b",
                        "component_id": "svc",
                        "measured_at": "2025-01-01T00:00:00+00:00",
                        "size_bytes": 1_000_000,
                    },
                }
            )
        )

        await sched.run_once()
        assert len(called_with) == 2
        assert {f.volume_name for f, _ in called_with} == {"vol-a", "vol-b"}
        # Same board client instance passed to both calls
        assert called_with[0][1] is mock_client
        assert called_with[1][1] is mock_client

    @pytest.mark.asyncio
    async def test_loop_cancellation_propagates(self, tmp_path, monkeypatch):
        """loop() re-raises CancelledError when the task is cancelled."""
        sched, backend, store = _make_scheduler(tmp_path)
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
        sched, backend, store = _make_scheduler(tmp_path)
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
