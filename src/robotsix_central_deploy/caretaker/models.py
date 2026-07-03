"""Caretaker domain models — findings, reports, and enumerations."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel


class FindingKind(str, Enum):
    UPDATE_APPLIED = "update_applied"
    UPDATE_FAILED = "update_failed"
    HEALTH = "health"
    VOLUME_GROWTH = "volume_growth"
    VOLUME_ORPHAN = "volume_orphan"
    DISK = "disk"
    PORT_COLLISION = "port_collision"


class CaretakerFinding(BaseModel):
    component_id: str = ""  # "" for host/orphan findings
    repo_id: str = ""  # "" = untracked, local-only
    kind: FindingKind
    title: str
    detail: str
    severity: Literal["warning", "error"] = "warning"


class CaretakerReport(BaseModel):
    started_at: datetime
    finished_at: datetime
    findings: list[CaretakerFinding] = []
    phases_run: list[str] = []
    mill_reported: int = 0
    local_only: int = 0
    errors: list[str] = []
    mill_reachable: bool = True
