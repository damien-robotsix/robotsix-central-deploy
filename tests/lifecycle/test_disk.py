"""Tests for the ``GET /disk`` endpoint."""

from __future__ import annotations

from collections import namedtuple
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.lifecycle.backend import NoopBackend
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.models import ExecutionBackendType
from robotsix_central_deploy.lifecycle.store import InMemoryStore

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch):
    """Wire a fresh store/backend/config into the server module before each test."""
    monkeypatch.setenv("ROBOTSIX_LIFECYCLE_API_KEY", "test-key")
    cfg = LifecycleConfig(  # type: ignore[call-arg]
        store_backend="memory",
        execution_backend=ExecutionBackendType.NOOP,
        api_key="test-key",
    )
    store = InMemoryStore()
    backend = NoopBackend()

    mock_checker = MagicMock()
    mock_checker.get_latest_digest = AsyncMock(return_value=None)

    server_mod._config = cfg
    server_mod._store = store
    server_mod._backend = backend
    server_mod._registry_checker = mock_checker
    server_mod.app.state.config = cfg
    server_mod.app.state.store = store
    server_mod.app.state.backend = backend
    server_mod.app.state.registry_checker = mock_checker


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestDiskEndpoint:
    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.get("/disk")
        assert resp.status_code == 401

    async def test_returns_disk_fields(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        monkeypatch.setattr(
            server_mod.shutil,
            "disk_usage",
            lambda path: DiskUsage(total=100_000_000, used=60_000_000, free=40_000_000),
        )
        resp = await client.get("/disk", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "total_bytes" in data
        assert "used_bytes" in data
        assert "free_bytes" in data
        assert "warn_threshold_pct" in data
        assert "docker" in data
        assert data["total_bytes"] == 100_000_000
        assert data["used_bytes"] == 60_000_000
        assert data["free_bytes"] == 40_000_000

    async def test_docker_df_is_zeros_for_noop(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        monkeypatch.setattr(
            server_mod.shutil,
            "disk_usage",
            lambda path: DiskUsage(total=1000, used=500, free=500),
        )
        resp = await client.get("/disk", headers=auth_headers)
        assert resp.status_code == 200
        docker = resp.json()["docker"]
        assert docker["images_size_bytes"] == 0
        assert docker["build_cache_size_bytes"] == 0

    async def test_no_warning_when_free_above_threshold(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        # free=10 GiB, warn=5 GiB → free > threshold
        monkeypatch.setattr(
            server_mod.shutil,
            "disk_usage",
            lambda path: DiskUsage(
                total=100_000_000_000,
                used=89_263_000_000,  # ~10.7 GiB free
                free=10_737_000_000,
            ),
        )
        server_mod.app.state.config.disk_warn_pct = 10.0
        resp = await client.get("/disk", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["warn_threshold_pct"] == 10.0

    async def test_warning_when_free_below_threshold(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        # free=2 GiB, warn=5 GiB → free < threshold
        monkeypatch.setattr(
            server_mod.shutil,
            "disk_usage",
            lambda path: DiskUsage(
                total=100_000_000_000,
                used=97_853_000_000,
                free=2_147_000_000,
            ),
        )
        server_mod.app.state.config.disk_warn_pct = 10.0
        resp = await client.get("/disk", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["warn_threshold_pct"] == 10.0

    async def test_disk_path_used(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        called_with = []

        def fake_disk_usage(path):
            called_with.append(path)
            return DiskUsage(total=1000, used=500, free=500)

        monkeypatch.setattr(server_mod.shutil, "disk_usage", fake_disk_usage)
        server_mod.app.state.config.disk_path = "/host_root"

        resp = await client.get("/disk", headers=auth_headers)
        assert resp.status_code == 200
        assert called_with == ["/host_root"]

    async def test_reclaim_build_cache_noop(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.post("/disk/reclaim", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["space_reclaimed_bytes"] == 0

    async def test_reclaim_build_cache_returns_bytes(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        async def _fake_prune(self) -> int:
            return 2_684_354_560  # 2.5 GiB

        monkeypatch.setattr(server_mod.NoopBackend, "prune_builds", _fake_prune)
        resp = await client.post("/disk/reclaim", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["space_reclaimed_bytes"] == 2_684_354_560

    async def test_reclaim_requires_auth(self, client: AsyncClient):
        resp = await client.post("/disk/reclaim")
        assert resp.status_code in (401, 403)
