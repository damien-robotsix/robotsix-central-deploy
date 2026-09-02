"""Caretaker domain models — findings, reports, and enumerations."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class FindingKind(str, Enum):
    """Kinds of findings the caretaker can emit.

    ``update_applied`` / ``update_failed`` — registry-check-driven
    image updates that succeeded or errored.

    ``health`` — a component health-check is failing.

    ``volume_growth`` — a named volume exceeded its configured
    growth threshold.

    ``volume_measurement`` — a named volume's size could not be
    measured this scan (du helper timeout / Docker stream cut); its
    growth is unknown until a later scan succeeds.

    ``volume_orphan`` — a named volume is not attached to any
    running component.

    ``disk`` — host disk usage crossed a warning or critical
    threshold.

    ``port_collision`` — two components are configured with the
    same host port.
    """

    UPDATE_APPLIED = "update_applied"
    UPDATE_FAILED = "update_failed"
    HEALTH = "health"
    VOLUME_GROWTH = "volume_growth"
    VOLUME_MEASUREMENT = "volume_measurement"
    VOLUME_ORPHAN = "volume_orphan"
    DISK = "disk"
    PORT_COLLISION = "port_collision"


class CaretakerFinding(BaseModel):
    """A single issue identified during a caretaker pass.

    ``component_id`` is the managed-component slug; it is empty
    for host-level or orphan-volume findings that aren't tied to
    a specific component.  ``repo_id`` is the upstream repository
    identifier and is empty when the finding originated locally
    (untracked, no matching onboarded repo).
    """

    component_id: str = Field(
        default="",
        description="Managed-component slug; empty for host-level or orphan-volume findings",
    )
    repo_id: str = Field(
        default="",
        description="Upstream repository identifier; empty when the finding is untracked",
    )
    kind: FindingKind = Field(
        description="Category of the finding (update_applied, update_failed, health, volume_growth, volume_orphan, disk, port_collision)"
    )
    title: str = Field(description="Short human-readable summary of the finding")
    detail: str = Field(
        description="Extended explanation with context and remediation hints"
    )
    severity: Literal["warning", "error"] = Field(
        default="warning",
        description="Severity level: 'warning' for actionable issues, 'error' for failures requiring attention",
    )


class CaretakerReport(BaseModel):
    """Aggregate result of a full caretaker pass.

    Collects every finding emitted by all enabled phases, together with
    timing. Findings are recorded locally (log + findings JSONL) only —
    the caretaker never files tickets (operator decision, 2026-09-01).
    """

    started_at: datetime = Field(
        description="UTC timestamp when the caretaker pass began"
    )
    finished_at: datetime = Field(
        description="UTC timestamp when the caretaker pass completed"
    )
    findings: list[CaretakerFinding] = Field(
        default_factory=list,
        description="Every finding emitted by all enabled phases during this pass",
    )
    phases_run: list[str] = Field(
        default_factory=list,
        description="Names of the caretaker phases that executed in this pass",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Non-fatal errors encountered during the pass",
    )
