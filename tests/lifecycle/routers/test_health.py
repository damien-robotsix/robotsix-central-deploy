"""Integration tests for the health endpoint."""

from __future__ import annotations

from httpx import AsyncClient

# Import the server module itself (not just symbols) so we can set its globals.
import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.models import (
    ServiceRecord,
    ServiceState,
)

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


class _FakeResolver:
    """Minimal stand-in for GhcrCredentialResolver's health-relevant surface."""

    def __init__(self, failures: int) -> None:
        self._failures = failures

    @property
    def consecutive_mint_failures(self) -> int:
        return self._failures


class _BackendWithResolver:
    """Backend stub exposing a ghcr_credentials resolver for the health route."""

    def __init__(self, failures: int) -> None:
        self._resolver = _FakeResolver(failures)

    @property
    def ghcr_credentials(self) -> _FakeResolver:
        return self._resolver


class TestHealth:
    async def test_health_returns_ok(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        # Default NoopBackend has no ghcr_credentials -> no finding.
        assert "findings" not in body

    async def test_health_finding_when_mint_failures_reach_threshold(
        self, client: AsyncClient
    ):
        server_mod.app.state.backend = _BackendWithResolver(3)
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["findings"] == [
            {
                "kind": "github_app_mint_failure",
                "detail": "3 consecutive GitHub App token mint failures",
            }
        ]

    async def test_health_no_finding_below_threshold(self, client: AsyncClient):
        server_mod.app.state.backend = _BackendWithResolver(2)
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    async def test_health_ok_when_backend_lacks_resolver(self, client: AsyncClient):
        # Default NoopBackend exposes no ghcr_credentials; endpoint stays healthy.
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# GET /services
# ---------------------------------------------------------------------------
