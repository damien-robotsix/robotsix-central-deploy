from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import AuditFinding

if TYPE_CHECKING:
    from ..lifecycle.config import LifecycleConfig

logger = logging.getLogger(__name__)

_MAX_FINDINGS = 100


async def report_finding(
    finding: AuditFinding,
    findings_path: Path,
    config: LifecycleConfig | None = None,
) -> None:
    """Report a volume-audit finding.

    Always logs at WARNING level and appends to a local JSON file (the
    backing store for ``GET /volumes/audit``).

    When *config* is provided and board API settings are configured
    (``board_api_url``, ``board_api_token``, ``board_repo_id``), also
    files a ticket on the robotsix board so the finding surfaces in the
    triage/planning system.
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

    # --- board ticket (when configured) ---
    if (
        config
        and config.board_api_url
        and config.board_api_token
        and config.board_repo_id
    ):
        try:
            from robotsix_board_agent.client import BoardClient
            from robotsix_board_agent.config import BoardAgentSettings

            settings = BoardAgentSettings(
                board_api_url=config.board_api_url,
                board_api_token=config.board_api_token,
                board_repo_id=config.board_repo_id,
            )
            client = BoardClient(settings)
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
                result = await client.create_ticket(
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
            finally:
                await client.close()
        except Exception as exc:
            logger.error(
                "Failed to file board ticket for finding on %s: %s",
                finding.volume_name,
                exc,
            )
