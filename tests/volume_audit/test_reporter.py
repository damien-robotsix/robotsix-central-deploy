import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_central_deploy.volume_audit.models import AuditFinding
from robotsix_central_deploy.volume_audit.reporter import _MAX_FINDINGS, report_finding


def _make_finding(
    volume_name: str = "test-vol",
    component_id: str = "test-comp",
    detail: str = "Growth detected",
) -> AuditFinding:
    return AuditFinding(
        volume_name=volume_name,
        component_id=component_id,
        finding_at=datetime(2025, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
        size_bytes=20_000_000,
        delta_bytes=10_000_000,
        growth_pct=100.0,
        detail=detail,
    )


class TestReportFindingLocalJson:
    @pytest.mark.asyncio
    async def test_creates_file_when_path_does_not_exist(self, tmp_path):
        """When findings_path does not exist, the file is created cleanly."""
        findings_path = tmp_path / "nonexistent" / "findings.json"
        finding = _make_finding()

        await report_finding(finding, findings_path)

        assert findings_path.exists()
        data = json.loads(findings_path.read_text())
        assert len(data) == 1
        assert data[0]["volume_name"] == "test-vol"
        assert data[0]["delta_bytes"] == 10_000_000

    @pytest.mark.asyncio
    async def test_creates_correct_json_content(self, tmp_path):
        """The JSON file is created with the correct content."""
        findings_path = tmp_path / "findings.json"
        finding = _make_finding()

        await report_finding(finding, findings_path)

        data = json.loads(findings_path.read_text())
        assert len(data) == 1
        entry = data[0]
        assert entry["volume_name"] == "test-vol"
        assert entry["component_id"] == "test-comp"
        assert entry["size_bytes"] == 20_000_000
        assert entry["delta_bytes"] == 10_000_000
        assert entry["growth_pct"] == 100.0
        assert entry["detail"] == "Growth detected"
        assert "finding_at" in entry

    @pytest.mark.asyncio
    async def test_appends_to_existing_file(self, tmp_path):
        """Subsequent calls append to the existing file."""
        findings_path = tmp_path / "findings.json"

        await report_finding(_make_finding(volume_name="vol-a"), findings_path)
        await report_finding(_make_finding(volume_name="vol-b"), findings_path)

        data = json.loads(findings_path.read_text())
        assert len(data) == 2
        assert data[0]["volume_name"] == "vol-a"
        assert data[1]["volume_name"] == "vol-b"

    @pytest.mark.asyncio
    async def test_prunes_oldest_when_exceeding_max(self, tmp_path):
        """When findings exceed _MAX_FINDINGS, oldest entries are pruned."""
        findings_path = tmp_path / "findings.json"
        # Seed with exactly _MAX_FINDINGS entries
        seed = [
            _make_finding(volume_name=f"vol-{i}").model_dump(mode="json")
            for i in range(_MAX_FINDINGS)
        ]
        findings_path.parent.mkdir(parents=True, exist_ok=True)
        findings_path.write_text(json.dumps(seed, default=str))

        # Add one more — should push total to _MAX_FINDINGS, dropping vol-0
        await report_finding(_make_finding(volume_name="vol-new"), findings_path)

        data = json.loads(findings_path.read_text())
        assert len(data) == _MAX_FINDINGS
        # vol-0 should be gone; vol-new should be last
        assert data[0]["volume_name"] == "vol-1"
        assert data[-1]["volume_name"] == "vol-new"

    @pytest.mark.asyncio
    async def test_handles_corrupted_json_file(self, tmp_path):
        """A corrupted JSON file is treated as empty and overwritten."""
        findings_path = tmp_path / "findings.json"
        findings_path.write_text("this is not json")

        await report_finding(_make_finding(), findings_path)

        data = json.loads(findings_path.read_text())
        assert len(data) == 1


class TestReportFindingBoardTicket:
    @pytest.mark.asyncio
    async def test_calls_create_ticket_with_correct_args(self):
        """When a board_client is provided, create_ticket is called correctly."""
        findings_path = Path("/tmp/findings.json")
        board_client = MagicMock()
        board_client.create_ticket = AsyncMock(
            return_value={"id": "TICKET-1", "title": "test"}
        )
        finding = _make_finding()

        await report_finding(finding, findings_path, board_client=board_client)

        board_client.create_ticket.assert_awaited_once()
        call_kwargs = board_client.create_ticket.call_args.kwargs
        assert call_kwargs["title"] == "Volume audit: test-vol (test-comp)"
        assert "test-vol" in call_kwargs["description"]
        assert "test-comp" in call_kwargs["description"]
        assert call_kwargs["kind"] == "task"
        assert call_kwargs["source"] == "volume-audit"

    @pytest.mark.asyncio
    async def test_no_board_client_no_ticket(self):
        """When board_client is None, no ticket is filed."""
        findings_path = Path("/tmp/findings.json")

        # Should not raise
        await report_finding(_make_finding(), findings_path, board_client=None)

    @pytest.mark.asyncio
    async def test_board_client_error_is_logged_not_raised(self, caplog):
        """When create_ticket raises, the error is logged, not propagated."""
        findings_path = Path("/tmp/findings.json")
        board_client = MagicMock()
        board_client.create_ticket = AsyncMock(side_effect=RuntimeError("board down"))

        # Should not raise
        await report_finding(_make_finding(), findings_path, board_client=board_client)

        assert "Failed to file board ticket" in caplog.text
