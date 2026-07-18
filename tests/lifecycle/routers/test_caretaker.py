"""Integration tests for the caretaker status endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock

from httpx import AsyncClient

from robotsix_central_deploy.lifecycle import server as server_mod


class TestCaretakerStatus:
    async def test_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/caretaker/status")
        assert resp.status_code == 401

    async def test_returns_500_when_scheduler_not_initialised(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        server_mod.app.state.caretaker_scheduler = None
        resp = await client.get("/caretaker/status", headers=auth_headers)
        assert resp.status_code == 500
        data = resp.json()
        assert "not initialised" in data["error"]

    async def test_returns_status_when_scheduler_available(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        expected_status = {
            "enabled": True,
            "last_run_at": "2025-01-15T10:30:00",
            "mill_reachable": True,
            "mill_reachable_detail": "ok",
            "last_report": {"tickets_filed": 3},
        }
        mock_scheduler = AsyncMock()
        mock_scheduler.get_status = AsyncMock(return_value=expected_status)
        server_mod.app.state.caretaker_scheduler = mock_scheduler

        resp = await client.get("/caretaker/status", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == expected_status
