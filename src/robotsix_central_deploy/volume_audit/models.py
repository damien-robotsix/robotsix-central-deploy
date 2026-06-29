from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class VolumeSizeSnapshot(BaseModel):
    """A single point-in-time size measurement for one volume."""

    volume_name: str
    component_id: str
    measured_at: datetime
    size_bytes: int


class VolumeGrowthRecord(BaseModel):
    """Growth record derived from comparing two consecutive snapshots."""

    volume_name: str
    component_id: str
    measured_at: datetime
    size_bytes: int
    prev_size_bytes: int | None = None
    delta_bytes: int | None = None
    growth_pct: float | None = None
    flagged: bool = False  # True when both guards are breached


class AuditFinding(BaseModel):
    """A threshold-breach finding produced by a scan pass."""

    volume_name: str
    component_id: str
    finding_at: datetime
    size_bytes: int
    delta_bytes: int
    growth_pct: float
    detail: str


class VolumeAuditResponse(BaseModel):
    """Payload for GET /volumes/audit."""

    enabled: bool
    last_scan_at: datetime | None = None
    volumes: list[VolumeGrowthRecord] = []
    recent_findings: list[AuditFinding] = []
