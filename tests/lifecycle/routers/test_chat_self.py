"""Tests for the chat-agent self-management endpoints (chat_self.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.models import SelfInspect
from robotsix_central_deploy.registry.chat_agent_audit_store import ChatAgentAuditStore
from robotsix_central_deploy.registry.models import ComponentConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _self_inspect(**kwargs: object) -> SelfInspect:
    """Create a SelfInspect with sensible defaults."""
    defaults: dict[str, object] = {
        "container_id": "abc123def456",
        "container_name": "central-deploy",
        "image_ref": "ghcr.io/test/central-deploy:main",
    }
    defaults.update(kwargs)
    return SelfInspect(**defaults)  # type: ignore[arg-type]


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
# Restart — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_self_restart_success(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/services/central-deploy/restart succeeds with 202."""
    _register_central_deploy()

    mock = MagicMock()
    mock.inspect_self = AsyncMock(return_value=_self_inspect())
    mock.trigger_self_restart = AsyncMock(return_value="abc123def456")
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/central-deploy/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["container_id"] == "abc123def456"
    assert data["name"] == "central-deploy"
    assert data["action"] == "self-restart"

    # Verify audit entry was written.
    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    restart_entries = [e for e in entries if e.action == "self-restart"]
    assert len(restart_entries) == 1
    assert restart_entries[0].component == "central-deploy"


# ---------------------------------------------------------------------------
# Restart — NotImplementedError (503)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_self_restart_not_implemented(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """inspect_self raising NotImplementedError yields 503."""
    _register_central_deploy()

    mock = MagicMock()
    mock.inspect_self = AsyncMock(side_effect=NotImplementedError)
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/central-deploy/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 503, resp.text


# ---------------------------------------------------------------------------
# Restart — RuntimeError (502)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_self_restart_runtime_error(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """trigger_self_restart raising RuntimeError yields 502."""
    _register_central_deploy()

    mock = MagicMock()
    mock.inspect_self = AsyncMock(return_value=_self_inspect())
    mock.trigger_self_restart = AsyncMock(
        side_effect=RuntimeError("container not found")
    )
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/central-deploy/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 502, resp.text
    assert "container not found" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Restart — not allowed (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_self_restart_not_allowed(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Restart returns 403 when central-deploy is not chat-agent-mutatable."""
    # Deliberately do NOT register central-deploy.
    resp = await client.post(
        "/chat/services/central-deploy/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Restart — rate limited (429)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_self_restart_rate_limited(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Second restart within cooldown window returns 429."""
    _register_central_deploy()

    mock = MagicMock()
    mock.inspect_self = AsyncMock(return_value=_self_inspect())
    mock.trigger_self_restart = AsyncMock(return_value="abc123def456")
    server_mod.app.state.backend = mock

    # First call succeeds.
    resp1 = await client.post(
        "/chat/services/central-deploy/restart",
        headers=auth_headers,
    )
    assert resp1.status_code == 202

    # Second call within cooldown fails.
    resp2 = await client.post(
        "/chat/services/central-deploy/restart",
        headers=auth_headers,
    )
    assert resp2.status_code == 429
    assert "Rate limit" in resp2.json()["error"]


# ---------------------------------------------------------------------------
# Update — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_self_update_success(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/services/central-deploy/update succeeds with 202."""
    _register_central_deploy()

    mock = MagicMock()
    mock.inspect_self = AsyncMock(return_value=_self_inspect())
    mock.trigger_self_update = AsyncMock(return_value="updater123")
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/central-deploy/update",
        headers=auth_headers,
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["updater_container_id"] == "updater123"
    assert data["name"] == "central-deploy"
    assert data["action"] == "self-update"

    # Verify audit entry was written.
    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    update_entries = [e for e in entries if e.action == "self-update"]
    assert len(update_entries) == 1
    assert update_entries[0].component == "central-deploy"


# ---------------------------------------------------------------------------
# Update — NotImplementedError (503)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_self_update_not_implemented(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """inspect_self raising NotImplementedError yields 503."""
    _register_central_deploy()

    mock = MagicMock()
    mock.inspect_self = AsyncMock(side_effect=NotImplementedError)
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/central-deploy/update",
        headers=auth_headers,
    )
    assert resp.status_code == 503, resp.text


# ---------------------------------------------------------------------------
# Update — RuntimeError (502)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_self_update_runtime_error(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """trigger_self_update raising RuntimeError yields 502."""
    _register_central_deploy()

    mock = MagicMock()
    mock.inspect_self = AsyncMock(return_value=_self_inspect())
    mock.trigger_self_update = AsyncMock(
        side_effect=RuntimeError("watchtower launch failed")
    )
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/central-deploy/update",
        headers=auth_headers,
    )
    assert resp.status_code == 502, resp.text
    assert "watchtower launch failed" in resp.json()["error"]
