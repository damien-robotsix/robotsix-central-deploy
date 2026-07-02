from __future__ import annotations

from datetime import datetime, timezone

from .models import AuditFinding, VolumeGrowthRecord, VolumeSizeSnapshot

# Sidecar exclusion is enforced at the Docker/shell level in measure_volume_bytes.
# These constants document the patterns for reference and cross-checking.
SIDECAR_SUFFIXES: tuple[str, ...] = (".db-wal", ".db-shm", ".db-journal")


def compute_growth_records(
    current: dict[str, VolumeSizeSnapshot],
    previous: dict[str, VolumeSizeSnapshot],
    growth_threshold_pct: float,
    min_delta_bytes: int,
) -> tuple[list[VolumeGrowthRecord], list[AuditFinding]]:
    """Compare *current* vs *previous* snapshots.

    Returns (records, findings):
    - records: one VolumeGrowthRecord per key in *current*
    - findings: subset where both threshold guards were breached

    Growth guards (BOTH must be true to flag):
    1. delta_bytes > min_delta_bytes     (absolute-size guard — prevents tiny-baseline false positives)
    2. growth_pct > growth_threshold_pct (percent-growth guard)
    """
    records: list[VolumeGrowthRecord] = []
    findings: list[AuditFinding] = []
    now = datetime.now(tz=timezone.utc)

    for vol_name, snap in current.items():
        prev_snap = previous.get(vol_name)
        if prev_snap is None:
            # First scan for this volume — no delta available yet
            rec = VolumeGrowthRecord(
                volume_name=vol_name,
                component_id=snap.component_id,
                measured_at=snap.measured_at,
                size_bytes=snap.size_bytes,
            )
        else:
            delta = snap.size_bytes - prev_snap.size_bytes
            pct = (
                (delta / prev_snap.size_bytes * 100)
                if prev_snap.size_bytes > 0
                else 0.0
            )
            pct = round(pct, 2)
            flagged = delta > min_delta_bytes and pct > growth_threshold_pct
            rec = VolumeGrowthRecord(
                volume_name=vol_name,
                component_id=snap.component_id,
                measured_at=snap.measured_at,
                size_bytes=snap.size_bytes,
                prev_size_bytes=prev_snap.size_bytes,
                delta_bytes=delta,
                growth_pct=pct,
                flagged=flagged,
            )
            if flagged:
                findings.append(
                    AuditFinding(
                        volume_name=vol_name,
                        component_id=snap.component_id,
                        finding_at=now,
                        size_bytes=snap.size_bytes,
                        delta_bytes=delta,
                        growth_pct=pct,
                        detail=(
                            f"Volume {vol_name!r} grew by {delta:,} bytes "
                            f"({pct:.1f}%) since last scan"
                        ),
                    )
                )
        records.append(rec)

    return records, findings
