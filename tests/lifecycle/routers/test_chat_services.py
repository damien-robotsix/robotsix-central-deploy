"""Tests for the chat-agent restart endpoint (chat_restart.py).

Mirrors the source-side module split: the chat_services.py monolith was
split into modular routers and the deploy/update cases were migrated out
to ``test_chat_deploy.py``.  This file now maps 1:1 to the
``chat_restart.py`` module (``POST /chat/services/{name}/restart``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.models import (
    ActionType,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.registry.chat_agent_audit_store import ChatAgentAuditStore
from robotsix_central_deploy.registry.models import ComponentConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_component(
    id: str = "test-svc",
    *,
    mutatable: bool = True,
    image: str = "test-svc:latest",
    container_name: str | None = None,
) -> ComponentConfig:
    """Register a component in the app's config store and registry."""
    cfg = ComponentConfig(
        id=id,
        image=image,
        container_name=container_name or id,
    )
    cfg.chat_agent_mutatable = mutatable
    server_mod.app.state.component_config_store.register(cfg)
    server_mod.app.state.registry.register(cfg)
    return cfg


async def _seed_service_record(
    name: str = "test-svc",
    state: ServiceState = ServiceState.RUNNING,
    image: str = "test-svc:latest",
) -> ServiceRecord:
    """Create and persist a ServiceRecord in the store."""
    record = ServiceRecord(name=name, state=state, image=image)
    await server_mod.app.state.store.put(record)
    return record


# ---------------------------------------------------------------------------
# Restart — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_happy_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/services/{name}/restart succeeds: RUNNING → RESTARTING → RUNNING."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)

    mock = MagicMock()
    mock.restart = AsyncMock(return_value=ServiceState.RUNNING)
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "test-svc"
    assert data["previous_state"] == "running"
    assert data["current_state"] == "running"

    # Verify the record was updated.
    stored = await server_mod.app.state.store.get("test-svc")
    assert stored is not None
    assert stored.state == ServiceState.RUNNING

    # Verify audit entry.
    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    restart_entries = [e for e in entries if e.action == ActionType.RESTART]
    assert len(restart_entries) >= 1
    assert restart_entries[-1].component == "test-svc"


# ---------------------------------------------------------------------------
# Restart — idempotent (already RESTARTING)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_idempotent_already_restarting(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Restart while already RESTARTING returns 200 immediately."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.RESTARTING)

    mock = MagicMock()
    mock.restart = AsyncMock(return_value=ServiceState.RUNNING)
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["current_state"] == "restarting"
    assert "already in progress" in data.get("detail", "").lower()

    # Backend.restart must NOT have been called.
    mock.restart.assert_not_called()


# ---------------------------------------------------------------------------
# Restart — invalid state transition (409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_invalid_state_transition(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Restart from STOPPED returns 409 (only RUNNING → RESTARTING is valid)."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.STOPPED)

    mock = MagicMock()
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    assert "Cannot restart from state" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Restart — backend failure (500)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_backend_failure(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Backend.restart raising an exception results in 500 and FAILED state."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)

    mock = MagicMock()
    mock.restart = AsyncMock(side_effect=RuntimeError("container vanished"))
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 500, resp.text
    assert "container vanished" in resp.json()["error"]

    # Record should be in FAILED state.
    stored = await server_mod.app.state.store.get("test-svc")
    assert stored is not None
    assert stored.state == ServiceState.FAILED
    assert "container vanished" in stored.last_error


# ---------------------------------------------------------------------------
# Restart — sibling fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_sibling_fanout(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Restarting a parent with siblings fans out to siblings best-effort."""
    from robotsix_central_deploy.registry.models import ServiceConfig

    sibling = ServiceConfig(
        service_key="worker",
        image="test-svc-worker:latest",
        container_name="test-svc-worker",
    )
    cfg = ComponentConfig(
        id="test-svc",
        image="test-svc:latest",
        container_name="test-svc",
        siblings=[sibling],
    )
    cfg.chat_agent_mutatable = True
    server_mod.app.state.component_config_store.register(cfg)
    server_mod.app.state.registry.register(cfg)

    await _seed_service_record("test-svc", state=ServiceState.RUNNING)
    await _seed_service_record("test-svc-worker", state=ServiceState.RUNNING)

    mock = MagicMock()
    mock.restart = AsyncMock(return_value=ServiceState.RUNNING)
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    # Backend.restart should have been called for both parent and sibling.
    assert mock.restart.call_count == 2
    called_names = {c.args[0].name for c in mock.restart.call_args_list}
    assert called_names == {"test-svc", "test-svc-worker"}


# ---------------------------------------------------------------------------
# Restart — rate limited (429)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_rate_limited(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Second restart within cooldown window returns 429."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)

    mock = MagicMock()
    mock.restart = AsyncMock(return_value=ServiceState.RUNNING)
    server_mod.app.state.backend = mock

    resp1 = await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )
    assert resp1.status_code == 200, resp1.text

    resp2 = await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )
    assert resp2.status_code == 429, resp2.text
    assert "Rate limit" in resp2.json()["error"]


# ---------------------------------------------------------------------------
# Restart — not allowlisted (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Restart returns 403 when component is not chat-agent-mutatable."""
    _register_component("test-svc", mutatable=False)
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)

    resp = await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Restart — component not registered at all (404)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_unregistered_component(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Restart returns 404 when the component is not registered."""
    # No component registered at all.

    resp = await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Restart — service not found (404)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_service_not_found(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Restart returns 404 when no ServiceRecord exists in the store."""
    _register_component("test-svc")
    # No seeded ServiceRecord — _get_or_create_record raises 404.

    resp = await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Unknown component — 404 names the registered id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_component_404_suggests_the_registered_id(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """The chat plane phrases its 404 like the operator plane's.

    Addressing a component by its repository name is the common mistake; a
    bare "not found" reads as a permissions problem instead.
    """
    _register_component("invest")

    resp = await client.post(
        "/chat/services/robotsix-invest/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 404, resp.text
    # register_error_handlers wraps a str detail as {"error": ..., "detail": ""}.
    message = resp.json()["error"]
    assert "robotsix-invest" in message
    assert "did you mean 'invest'?" in message


# ---------------------------------------------------------------------------
# Authentication — missing auth (no longer 401)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_no_longer_401(
    client: AsyncClient,
) -> None:
    """Component-level auth was removed; restart without auth headers no longer returns 401."""
    resp = await client.post("/chat/services/test-svc/restart")
    assert resp.status_code != 401, resp.text


@pytest.mark.asyncio
async def test_restart_invalid_auth_no_longer_401(
    client: AsyncClient,
) -> None:
    """Component-level auth was removed; a wrong API key no longer yields 401."""
    resp = await client.post(
        "/chat/services/test-svc/restart",
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code != 401, resp.text


# ---------------------------------------------------------------------------
# Audit logging — verify entries per action type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_audit_entry(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Successful restart writes an audit entry with RESTART action."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)

    mock = MagicMock()
    mock.restart = AsyncMock(return_value=ServiceState.RUNNING)
    server_mod.app.state.backend = mock

    await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )

    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    restart_entries = [e for e in entries if e.action == ActionType.RESTART]
    assert len(restart_entries) >= 1
    entry = restart_entries[-1]
    assert entry.component == "test-svc"
    assert "running" in entry.detail.lower()
