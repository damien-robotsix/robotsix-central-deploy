"""Audit-finding reporter that persists volume-audit results to a JSON log."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .models import AuditFinding

logger = logging.getLogger(__name__)

_MAX_FINDINGS = 100


async def report_finding(
    finding: AuditFinding,
    findings_path: Path,
) -> None:
    """Report a volume-audit finding.

    Logs at WARNING level and appends to a local JSON file (the backing
    store for ``GET /volumes/audit``). Nothing else: the caretaker never
    files tickets (operator decision, 2026-09-01) — the operator, the
    fleet monitor, and the chat agent read findings from here.
    """
    logger.warning(
        "Volume audit finding: %s — %s",
        finding.volume_name,
        finding.detail,
    )

    # --- local JSON (always) ---
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = (
            findings_path.read_text(encoding="utf-8")
            if findings_path.exists()
            else "[]"
        )
        existing: list[dict[str, Any]] = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        existing = []
    existing.append(finding.model_dump(mode="json"))
    if len(existing) > _MAX_FINDINGS:
        existing = existing[-_MAX_FINDINGS:]
    findings_path.write_text(
        json.dumps(existing, indent=2, default=str), encoding="utf-8"
    )
