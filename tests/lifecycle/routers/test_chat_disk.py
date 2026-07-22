"""Tests for the chat-agent disk reclaim endpoint (chat_disk.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.models import DockerDfStats
from robotsix_central_deploy.registry.chat_agent_audit_store import ChatAgentAuditStore
from robotsix_central_deploy.registry.models import ComponentConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_central_deploy(*, mutatable: bool = True) -> None:
    """Register central-deploy in the app's component config store."""
    cfg = ComponentConfig(
        id="central-deploy",
        image="ghcr.io/test/central-deploy:main",
        container_name="central-deploy",
    )
    cfg.chat_agent_mutatable = mutatable
    server_mod.app.state.component_config_store.register(cfg)


# ---------------------------------------------------------------------------
# Happy path — build_cache only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_disk_reclaim_build_cache(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/disk/reclaim with build_cache=True succeeds."""
    _register_central_deploy()

    mock = MagicMock()
    mock.prune_builds = AsyncMock(return_value=1024)
    mock.prune_images = AsyncMock(return_value=0)
    mock.disk_df = AsyncMock(return_value=DockerDfStats())
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/disk/reclaim",
        headers=auth_headers,
        json={"build_cache": True},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["space_reclaimed_bytes"] == 1024
    assert data["name"] == "central-deploy"
    assert data["action"] == "disk-reclaim"

    # Verify audit entry was written.
    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    reclaim_entries = [e for e in entries if e.action == "disk-reclaim"]
    assert len(reclaim_entries) == 1
    assert reclaim_entries[0].component == "central-deploy"


# ---------------------------------------------------------------------------
# Happy path — dangling_images only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_disk_reclaim_dangling_images(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/disk/reclaim with dangling_images=True succeeds."""
    _register_central_deploy()

    mock = MagicMock()
    mock.prune_builds = AsyncMock(return_value=0)
    mock.prune_images = AsyncMock(return_value=2048)
    mock.disk_df = AsyncMock(return_value=DockerDfStats())
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/disk/reclaim",
        headers=auth_headers,
        json={"dangling_images": True},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["space_reclaimed_bytes"] == 2048

    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    reclaim_entries = [e for e in entries if e.action == "disk-reclaim"]
    assert len(reclaim_entries) == 1


# ---------------------------------------------------------------------------
# Happy path — both targets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_disk_reclaim_both(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/disk/reclaim with both targets succeeds."""
    _register_central_deploy()

    mock = MagicMock()
    mock.prune_builds = AsyncMock(return_value=1024)
    mock.prune_images = AsyncMock(return_value=2048)
    mock.disk_df = AsyncMock(return_value=DockerDfStats())
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/disk/reclaim",
        headers=auth_headers,
        json={"build_cache": True, "dangling_images": True},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["space_reclaimed_bytes"] == 3072
    assert data["disk_snapshot"] is not None


# ---------------------------------------------------------------------------
# Neither target selected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_disk_reclaim_nothing(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/disk/reclaim with no targets returns zero reclaimed."""
    _register_central_deploy()

    mock = MagicMock()
    mock.prune_builds = AsyncMock(return_value=0)
    mock.prune_images = AsyncMock(return_value=0)
    mock.disk_df = AsyncMock(return_value=DockerDfStats())
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/disk/reclaim",
        headers=auth_headers,
        json={},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["space_reclaimed_bytes"] == 0
    assert "nothing requested" in data["detail"]


# ---------------------------------------------------------------------------
# Not allowed (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_disk_reclaim_not_allowed(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Disk reclaim returns 403 when central-deploy is not registered."""
    # Deliberately do NOT register central-deploy.
    resp = await client.post(
        "/chat/disk/reclaim",
        headers=auth_headers,
        json={"build_cache": True},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Rate limited (429)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_disk_reclaim_rate_limited(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Second reclaim within cooldown window returns 429."""
    _register_central_deploy()

    mock = MagicMock()
    mock.prune_builds = AsyncMock(return_value=0)
    mock.prune_images = AsyncMock(return_value=0)
    mock.disk_df = AsyncMock(return_value=DockerDfStats())
    server_mod.app.state.backend = mock

    # First call succeeds.
    resp1 = await client.post(
        "/chat/disk/reclaim",
        headers=auth_headers,
        json={"build_cache": True},
    )
    assert resp1.status_code == 200

    # Second call within cooldown fails.
    resp2 = await client.post(
        "/chat/disk/reclaim",
        headers=auth_headers,
        json={"build_cache": True},
    )
    assert resp2.status_code == 429
    assert "Rate limit" in resp2.json()["error"]


# ---------------------------------------------------------------------------
# Unauthorized (401)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_disk_reclaim_unauthorized(
    client: AsyncClient,
) -> None:
    """Disk reclaim without auth header returns 401."""
    _register_central_deploy()
    resp = await client.post(
        "/chat/disk/reclaim",
        json={"build_cache": True},
    )
    assert resp.status_code == 401, resp.text
