"""Caretaker domain models — findings, reports, and enumerations."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel


class FindingKind(str, Enum):
    """Kinds of findings the caretaker can emit.

    ``update_applied`` / ``update_failed`` — registry-check-driven
    image updates that succeeded or errored.

    ``health`` — a component health-check is failing.

    ``volume_growth`` — a named volume exceeded its configured
    growth threshold.

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

    component_id: str = ""  # "" for host/orphan findings
    repo_id: str = ""  # "" = untracked, local-only
    kind: FindingKind
    title: str
    detail: str
    severity: Literal["warning", "error"] = "warning"


class CaretakerReport(BaseModel):
    """Aggregate result of a full caretaker pass.

    Collects every finding emitted by all enabled phases, together
    with timing and mill-reporting counters.  ``mill_reported``
    counts findings that were successfully forwarded to the
    central mill; ``local_only`` counts findings that were
    detected but could not be reported (e.g. mill unreachable,
    untracked repo, or explicitly opted out of remote reporting).
    """

    started_at: datetime
    finished_at: datetime
    findings: list[CaretakerFinding] = []
    phases_run: list[str] = []
    mill_reported: int = 0
    local_only: int = 0
    errors: list[str] = []
    mill_reachable: bool = True
    mill_reachable_detail: str = ""
