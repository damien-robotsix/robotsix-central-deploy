from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class VolumeSizeSnapshot(BaseModel):
    """A single point-in-time size measurement for one volume."""

    volume_name: str = Field(description="Docker volume name as reported by the daemon")
    component_id: str = Field(
        description="Managed-component slug this volume belongs to"
    )
    measured_at: datetime = Field(
        description="UTC timestamp when the size measurement was taken"
    )
    size_bytes: int = Field(
        description="Total bytes consumed by the volume at measurement time"
    )


class VolumeGrowthRecord(BaseModel):
    """Growth record derived from comparing two consecutive snapshots."""

    volume_name: str = Field(description="Docker volume name as reported by the daemon")
    component_id: str = Field(
        description="Managed-component slug this volume belongs to"
    )
    measured_at: datetime = Field(description="UTC timestamp of the current snapshot")
    size_bytes: int = Field(
        description="Bytes consumed by the volume in the current snapshot"
    )
    prev_size_bytes: int | None = Field(
        default=None,
        description="Bytes consumed in the previous snapshot; None on first measurement",
    )
    delta_bytes: int | None = Field(
        default=None,
        description="Difference between current and previous size; None on first measurement",
    )
    growth_pct: float | None = Field(
        default=None,
        description="Percentage growth since the previous snapshot; None on first measurement",
    )
    flagged: bool = Field(
        default=False,
        description="True when both the absolute and percentage growth thresholds are breached",
    )


class AuditFinding(BaseModel):
    """A threshold-breach finding produced by a scan pass."""

    volume_name: str = Field(description="Docker volume name as reported by the daemon")
    component_id: str = Field(
        description="Managed-component slug this volume belongs to"
    )
    finding_at: datetime = Field(
        description="UTC timestamp when the threshold breach was detected"
    )
    size_bytes: int = Field(description="Bytes consumed by the volume at finding time")
    delta_bytes: int = Field(description="Absolute growth since the previous audit")
    growth_pct: float = Field(description="Percentage growth since the previous audit")
    detail: str = Field(
        description="Human-readable description of the breach with context"
    )


class VolumeAuditResponse(BaseModel):
    """Payload for GET /volumes/audit."""

    enabled: bool = Field(description="Whether the volume audit subsystem is active")
    last_scan_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the most recent scan; None if never scanned",
    )
    volumes: list[VolumeGrowthRecord] = Field(
        default_factory=list,
        description="Growth records for all tracked volumes from the latest scan",
    )
    recent_findings: list[AuditFinding] = Field(
        default_factory=list,
        description="Recent threshold-breach findings from the latest scan",
    )
