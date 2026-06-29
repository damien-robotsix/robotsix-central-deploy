from unittest.mock import MagicMock

import pytest

import robotsix_central_deploy.lifecycle.server as server_mod


class TestVolumeAuditEndpoint:
    @pytest.mark.asyncio
    async def test_disabled_returns_disabled_response(self, client):
        """When volume_audit_enabled=False (default), endpoint returns enabled=false."""
        resp = await client.get(
            "/volumes/audit", headers={"X-API-Key": "test-key"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["volumes"] == []

    @pytest.mark.asyncio
    async def test_enabled_returns_scheduler_response(self, client, monkeypatch):
        """When enabled, endpoint delegates to app.state.volume_audit_scheduler."""
        fake_scheduler = MagicMock()
        fake_scheduler.get_audit_response.return_value = server_mod.VolumeAuditResponse(
            enabled=True, volumes=[], recent_findings=[]
        )
        # Starlette State uses __setattr__ rather than __dict__ — set directly.
        server_mod.app.state.__setattr__("volume_audit_scheduler", fake_scheduler)
        # Patch app.state.config to enable the subsystem
        server_mod.app.state.__setattr__(
            "config",
            server_mod.app.state.config.model_copy(
                update={"volume_audit_enabled": True}
            ),
        )
        resp = await client.get(
            "/volumes/audit", headers={"X-API-Key": "test-key"}
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True
        fake_scheduler.get_audit_response.assert_called_once()
