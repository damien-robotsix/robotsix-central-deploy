from datetime import datetime, timezone

from robotsix_central_deploy.caretaker.volume_audit.growth import (
    SIDECAR_SUFFIXES,
    compute_growth_records,
)
from robotsix_central_deploy.caretaker.volume_audit.models import VolumeSizeSnapshot

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _snap(vol: str, comp: str, size: int) -> VolumeSizeSnapshot:
    return VolumeSizeSnapshot(
        volume_name=vol, component_id=comp, measured_at=NOW, size_bytes=size
    )


class TestComputeGrowthRecords:
    def test_no_previous_snapshot(self):
        """First scan: no delta fields, never flagged."""
        current = {"vol": _snap("vol", "svc", 1_000_000)}
        records, findings = compute_growth_records(current, {}, 10.0, 1_000)
        assert len(records) == 1
        r = records[0]
        assert r.delta_bytes is None
        assert r.growth_pct is None
        assert r.flagged is False
        assert findings == []

    def test_min_delta_guard_suppresses_pct_alert(self):
        """delta > 0 but < min_delta_bytes → not flagged even if pct is huge."""
        prev = {"vol": _snap("vol", "svc", 100)}  # tiny baseline
        curr = {"vol": _snap("vol", "svc", 200)}  # 100% growth
        records, findings = compute_growth_records(
            curr,
            prev,
            growth_threshold_pct=10.0,
            min_delta_bytes=1_000_000,  # 1 MiB — delta of 100 bytes won't reach it
        )
        assert records[0].flagged is False
        assert findings == []

    def test_pct_below_threshold_not_flagged(self):
        """delta exceeds min_delta_bytes but pct is below threshold → no finding."""
        prev = {"vol": _snap("vol", "svc", 100_000_000)}  # 100 MiB
        curr = {"vol": _snap("vol", "svc", 100_500_000)}  # +500 KiB = 0.5%
        records, findings = compute_growth_records(
            curr,
            prev,
            growth_threshold_pct=10.0,
            min_delta_bytes=100_000,  # 100 KiB — 500 KiB exceeds this
        )
        assert records[0].flagged is False
        assert findings == []

    def test_both_guards_breached_produces_finding(self):
        """delta > min_delta_bytes AND pct > threshold → flagged + AuditFinding."""
        prev = {"vol": _snap("vol", "svc", 50_000_000)}  # 50 MiB
        curr = {"vol": _snap("vol", "svc", 100_000_000)}  # +50 MiB = 100%
        records, findings = compute_growth_records(
            curr,
            prev,
            growth_threshold_pct=10.0,
            min_delta_bytes=10_485_760,  # 10 MiB — 50 MiB exceeds this
        )
        assert records[0].flagged is True
        assert len(findings) == 1
        f = findings[0]
        assert f.volume_name == "vol"
        assert f.delta_bytes == 50_000_000
        assert f.growth_pct == 100.0

    def test_zero_previous_size_no_division_error(self):
        """prev_size_bytes == 0: pct computed as 0.0, not flagged."""
        prev = {"vol": _snap("vol", "svc", 0)}
        curr = {"vol": _snap("vol", "svc", 20_000_000)}
        records, findings = compute_growth_records(
            curr,
            prev,
            growth_threshold_pct=10.0,
            min_delta_bytes=1_000,
        )
        assert records[0].growth_pct == 0.0
        assert records[0].flagged is False
        assert findings == []

    def test_sidecar_suffix_constants_present(self):
        """Verify the documented sidecar suffixes are present for reference."""
        assert ".db-wal" in SIDECAR_SUFFIXES
        assert ".db-shm" in SIDECAR_SUFFIXES
        assert ".db-journal" in SIDECAR_SUFFIXES
