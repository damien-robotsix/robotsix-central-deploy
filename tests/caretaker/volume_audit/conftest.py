"""Shared fixtures for volume_audit tests."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.lifecycle.config import LifecycleConfig


@pytest.fixture(autouse=True)
def _setup_app(monkeypatch):
    """Ensure app.state.config is set before each test."""
    monkeypatch.setenv("ROBOTSIX_LIFECYCLE_API_KEY", "test-key")
    cfg = LifecycleConfig(api_key="test-key")
    server_mod.app.state.config = cfg


@pytest.fixture
async def client():
    transport = ASGITransport(app=server_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
