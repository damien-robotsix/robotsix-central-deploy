"""Unit tests for volume_audit.reporter.report_finding."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from robotsix_central_deploy.caretaker.volume_audit.models import AuditFinding
from robotsix_central_deploy.caretaker.volume_audit.reporter import report_finding


def _make_finding(**overrides) -> AuditFinding:
    """Build a minimal AuditFinding for test use."""
    defaults: dict = {
        "volume_name": "test-vol",
        "component_id": "test-comp",
        "finding_at": datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
        "size_bytes": 50_000_000,
        "delta_bytes": 10_000_000,
        "growth_pct": 25.0,
        "detail": "Volume grew beyond threshold",
    }
    defaults.update(overrides)
    return AuditFinding(**defaults)


class TestReportFinding:
    @pytest.mark.asyncio
    async def test_writes_finding_to_json_file(self, tmp_path: Path):
        """report_finding writes the finding as JSON to the findings file."""
        findings_path = tmp_path / "findings.json"
        finding = _make_finding()

        await report_finding(finding, findings_path)

        assert findings_path.exists()
        data = json.loads(findings_path.read_text())
        assert len(data) == 1
        assert data[0]["volume_name"] == "test-vol"
        assert data[0]["component_id"] == "test-comp"
        assert data[0]["size_bytes"] == 50_000_000

    @pytest.mark.asyncio
    async def test_appends_to_existing_file(self, tmp_path: Path):
        """New findings are appended, not overwritten."""
        findings_path = tmp_path / "findings.json"
        findings_path.write_text(
            json.dumps([{"volume_name": "existing", "component_id": "old"}])
        )

        await report_finding(_make_finding(volume_name="new-vol"), findings_path)

        data = json.loads(findings_path.read_text())
        assert len(data) == 2
        assert data[0]["volume_name"] == "existing"
        assert data[1]["volume_name"] == "new-vol"

    @pytest.mark.asyncio
    async def test_creates_parent_directories(self, tmp_path: Path):
        """When the parent directory is missing, it is created automatically."""
        findings_path = tmp_path / "deep" / "nested" / "findings.json"

        await report_finding(_make_finding(), findings_path)

        assert findings_path.exists()
        data = json.loads(findings_path.read_text())
        assert len(data) == 1

    @pytest.mark.asyncio
    async def test_recovers_from_corrupted_json(self, tmp_path: Path):
        """A file with invalid JSON is treated as empty and overwritten."""
        findings_path = tmp_path / "findings.json"
        findings_path.write_text("this is not valid {{{ json")

        await report_finding(_make_finding(volume_name="recovered"), findings_path)

        data = json.loads(findings_path.read_text())
        assert len(data) == 1
        assert data[0]["volume_name"] == "recovered"

    @pytest.mark.asyncio
    async def test_recovers_from_read_oserror(self, tmp_path: Path, monkeypatch):
        """When read_text raises OSError, recovery resets to empty list."""
        findings_path = tmp_path / "findings.json"
        # Pre-create the file so exists() returns True, then monkeypatch
        # read_text to raise OSError on the first call.
        findings_path.write_text("[]")
        original_read_text = Path.read_text

        call_count = 0

        def _failing_read_text(self, *args, **kwargs):
            nonlocal call_count
            if self == findings_path:
                call_count += 1
                if call_count == 1:
                    raise OSError("simulated read failure")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _failing_read_text)

        await report_finding(_make_finding(volume_name="after-oserror"), findings_path)

        data = json.loads(findings_path.read_text())
        assert len(data) == 1
        assert data[0]["volume_name"] == "after-oserror"

    @pytest.mark.asyncio
    async def test_truncates_at_max_findings(self, tmp_path: Path, monkeypatch):
        """When existing findings exceed _MAX_FINDINGS, oldest are dropped."""
        monkeypatch.setattr(
            "robotsix_central_deploy.caretaker.volume_audit.reporter._MAX_FINDINGS", 5
        )

        findings_path = tmp_path / "findings.json"
        # Seed with 7 existing findings (exceeds patched max of 5)
        existing = [{"volume_name": f"vol-{i}", "component_id": "x"} for i in range(7)]
        findings_path.write_text(json.dumps(existing))

        await report_finding(_make_finding(volume_name="newest"), findings_path)

        data = json.loads(findings_path.read_text())
        assert len(data) == 5
        # 7 initial + 1 appended = 8; truncation keeps last 5
        names = [d["volume_name"] for d in data]
        assert names == ["vol-3", "vol-4", "vol-5", "vol-6", "newest"]

    @pytest.mark.asyncio
    async def test_finding_is_recorded_and_never_becomes_a_ticket(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """The reporter records locally and files NO ticket anywhere.

        The caretaker's ticket-filing capability was removed outright
        (operator decision, 2026-09-01): a finding lands in the log and the
        findings JSON — the signature takes no client and performs no HTTP.
        """
        import inspect

        findings_path = tmp_path / "findings.json"

        with caplog.at_level("WARNING"):
            await report_finding(_make_finding(), findings_path)

        assert findings_path.exists()
        data = json.loads(findings_path.read_text())
        assert len(data) == 1
        assert any("Volume audit finding" in r.message for r in caplog.records)
        # The ticket seam is gone at the signature level — nothing to inject.
        params = inspect.signature(report_finding).parameters
        assert set(params) == {"finding", "findings_path"}
