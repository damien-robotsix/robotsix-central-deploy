from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .models import AuditFinding

logger = logging.getLogger(__name__)

_MAX_FINDINGS = 100


def report_finding(finding: AuditFinding, findings_path: Path) -> None:
    """Placeholder report_finding implementation.

    Logs the finding at WARNING level and appends it to a local JSON file.
    The findings file is the backing store for the recent_findings list in
    GET /volumes/audit.

    TODO: Replace or extend with real board-filing integration when the
    board seam is available — the audit logic never needs to change, only
    this function.
    """
    logger.warning(
        "Volume audit finding: %s — %s",
        finding.volume_name,
        finding.detail,
    )
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
    # Keep the most recent N findings only
    if len(existing) > _MAX_FINDINGS:
        existing = existing[-_MAX_FINDINGS:]
    findings_path.write_text(
        json.dumps(existing, indent=2, default=str), encoding="utf-8"
    )
