from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import AuditFinding

if TYPE_CHECKING:
    from robotsix_board_agent.client import BoardClient

logger = logging.getLogger(__name__)

_MAX_FINDINGS = 100


async def report_finding(
    finding: AuditFinding,
    findings_path: Path,
    board_client: BoardClient | None = None,
) -> None:
    """Report a volume-audit finding.

    Always logs at WARNING level and appends to a local JSON file (the
    backing store for ``GET /volumes/audit``).

    When *board_client* is provided, also files a ticket on the robotsix
    board so the finding surfaces in the triage/planning system.  The
    caller is responsible for creating and closing the client — typically
    one client per scan pass, reused for all findings in that pass.
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
    except json.JSONDecodeError, OSError:
        existing = []
    existing.append(finding.model_dump(mode="json"))
    if len(existing) > _MAX_FINDINGS:
        existing = existing[-_MAX_FINDINGS:]
    findings_path.write_text(
        json.dumps(existing, indent=2, default=str), encoding="utf-8"
    )

    # --- board ticket (when client provided) ---
    if board_client is not None:
        try:
            title = f"Volume audit: {finding.volume_name} ({finding.component_id})"
            description = (
                f"**Volume:** `{finding.volume_name}`\n"
                f"**Component:** `{finding.component_id}`\n"
                f"**Detected at:** {finding.finding_at.isoformat()}\n"
                f"**Size:** {finding.size_bytes:,} bytes\n"
                f"**Delta:** {finding.delta_bytes:+,} bytes\n"
                f"**Growth:** {finding.growth_pct:+.1f}%\n\n"
                f"> {finding.detail}"
            )
            result = await board_client.create_ticket(
                title=title,
                description=description,
                kind="task",
                source="volume-audit",
            )
            logger.info(
                "Filed board ticket %s for volume-audit finding on %s",
                result.get("id", "?"),
                finding.volume_name,
            )
        except Exception as exc:
            logger.error(
                "Failed to file board ticket for finding on %s: %s",
                finding.volume_name,
                exc,
            )
