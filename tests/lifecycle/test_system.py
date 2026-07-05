"""Integration tests for the system status endpoint."""

from __future__ import annotations


from httpx import AsyncClient


from robotsix_central_deploy.lifecycle.models import (
    ServiceRecord,
    ServiceState,
)

# Import the server module itself (not just symbols) so we can set its globals.
from robotsix_central_deploy.lifecycle import server as server_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_store(*names: str, image: str = "", deployed_digest: str = "") -> None:
    """Populate the server's store with records for testing."""
    s = server_mod.app.state.store
    assert s is not None
    for name in names:
        rec = ServiceRecord(
            name=name, state=ServiceState.STOPPED, image=image or f"{name}:latest"
        )
        if deployed_digest:
            rec.deployed_image_digest = deployed_digest
        await s.put(rec)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------


class TestGetStatus:
    async def test_not_found(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/services/nonexistent", headers=auth_headers)
        assert resp.status_code == 404

    async def test_returns_status(self, client: AsyncClient, auth_headers: dict):
        await _seed_store("svc-a")
        resp = await client.get("/services/svc-a", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "svc-a"
        assert data["state"] in {e.value for e in ServiceState}
        assert "image" in data


# ---------------------------------------------------------------------------
# GET /services/{name}/health
# ---------------------------------------------------------------------------
