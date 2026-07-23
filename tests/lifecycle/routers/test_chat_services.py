"""Tests for the chat-agent service lifecycle endpoints (chat_services.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.models import (
    ActionType,
    DeployOutcome,
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


def _configure_deploy_allowlist(*names: str) -> None:
    """Add component names to the deploy allowlist."""
    server_mod.app.state.config.chat_agent_deployable_components = list(names)


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
    assert "Cannot restart from state" in resp.json()["detail"]


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
    assert "container vanished" in resp.json()["detail"]

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
    assert resp1.status_code == 200

    resp2 = await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )
    assert resp2.status_code == 429, resp2.text
    assert "Rate limit" in resp2.json()["detail"]


# ---------------------------------------------------------------------------
# Restart — not allowlisted (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Restart returns 403 when component is not chat-agent-mutatable."""
    # Register component but NOT mutatable.
    _register_component("test-svc", mutatable=False)
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)

    resp = await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Restart — component not registered at all (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_unregistered_component(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Restart returns 403 when no component config exists at all."""
    resp = await client.post(
        "/chat/services/test-svc/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Update — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_happy_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/services/{name}/update succeeds with 200."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)

    outcome = DeployOutcome(
        deployed_digest="sha256:abc123def456",
        previous_digest="sha256:111222333444",
        state=ServiceState.RUNNING,
    )
    mock = MagicMock()
    mock.deploy = AsyncMock(return_value=outcome)
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/test-svc/update",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "test-svc"
    assert data["deployed_digest"] == "sha256:abc123def456"
    assert data["previous_digest"] == "sha256:111222333444"
    assert data["current_state"] == "running"

    # Verify audit entry.
    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    update_entries = [e for e in entries if e.action == "update"]
    assert len(update_entries) >= 1
    assert update_entries[-1].component == "test-svc"


# ---------------------------------------------------------------------------
# Update — not allowlisted (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Update returns 403 when component is not chat-agent-mutatable."""
    _register_component("test-svc", mutatable=False)

    resp = await client.post(
        "/chat/services/test-svc/update",
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text


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
# Update — service not found (404)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_service_not_found(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Update returns 404 when no component config exists in the registry."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)
    # Remove from registry (but keep in config store, so allowlist passes).
    server_mod.app.state.registry._components.pop("test-svc", None)

    resp = await client.post(
        "/chat/services/test-svc/update",
        headers=auth_headers,
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Update — deploy lock contention (409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_lock_contention(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Update returns 409 when a deploy is already in progress."""
    from robotsix_central_deploy.lifecycle.deploy_lock import try_acquire_deploy_lock

    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)

    # Acquire the lock before making the request.
    acquired = await try_acquire_deploy_lock("test-svc")
    assert acquired

    try:
        resp = await client.post(
            "/chat/services/test-svc/update",
            headers=auth_headers,
        )
        assert resp.status_code == 409, resp.text
        assert "already in progress" in resp.json()["detail"]
    finally:
        from robotsix_central_deploy.lifecycle.deploy_lock import release_deploy_lock

        release_deploy_lock("test-svc")


# ---------------------------------------------------------------------------
# Update — backend failure (500)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_backend_failure(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Backend.deploy raising an exception results in 500."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)

    mock = MagicMock()
    mock.deploy = AsyncMock(side_effect=RuntimeError("pull failed"))
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/test-svc/update",
        headers=auth_headers,
    )
    assert resp.status_code == 500, resp.text
    assert "pull failed" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Update — rate limited (429)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_rate_limited(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Second update within cooldown window returns 429."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)

    outcome = DeployOutcome(
        deployed_digest="sha256:abc123",
        previous_digest="sha256:prev",
        state=ServiceState.RUNNING,
    )
    mock = MagicMock()
    mock.deploy = AsyncMock(return_value=outcome)
    server_mod.app.state.backend = mock

    resp1 = await client.post(
        "/chat/services/test-svc/update",
        headers=auth_headers,
    )
    assert resp1.status_code == 200

    resp2 = await client.post(
        "/chat/services/test-svc/update",
        headers=auth_headers,
    )
    assert resp2.status_code == 429, resp2.text
    assert "Rate limit" in resp2.json()["detail"]


# ---------------------------------------------------------------------------
# Update — sibling deploy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_sibling_deploy(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Update with siblings deploys siblings inline."""
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

    outcome = DeployOutcome(
        deployed_digest="sha256:abc123",
        previous_digest="sha256:prev",
        state=ServiceState.RUNNING,
    )
    mock = MagicMock()
    mock.deploy = AsyncMock(return_value=outcome)
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/test-svc/update",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "test-svc-worker" in data["updated_siblings"]

    # Both parent and sibling should have been deployed.
    assert mock.deploy.call_count == 2


# ---------------------------------------------------------------------------
# Deploy — happy path (persisted config)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_happy_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/deploy succeeds with a persisted ComponentConfig."""
    _register_component("test-svc")
    _configure_deploy_allowlist("test-svc")

    outcome = DeployOutcome(
        deployed_digest="sha256:deploy123",
        previous_digest="sha256:prev456",
        state=ServiceState.RUNNING,
    )
    mock = MagicMock()
    mock.deploy = AsyncMock(return_value=outcome)
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/deploy",
        json={"name": "test-svc", "repo": "https://github.com/org/test-svc"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "test-svc"
    assert data["deployed_digest"] == "sha256:deploy123"
    assert data["previous_digest"] == "sha256:prev456"
    assert data["current_state"] == "running"

    # Verify audit entry.
    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    deploy_entries = [e for e in entries if e.action == "deploy"]
    assert len(deploy_entries) >= 1
    assert deploy_entries[-1].component == "test-svc"


# ---------------------------------------------------------------------------
# Deploy — not in deploy allowlist (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Deploy returns 403 when name is not in chat_agent_deployable_components."""
    _register_component("test-svc")
    # Deliberately do NOT add to deploy allowlist.

    resp = await client.post(
        "/chat/deploy",
        json={"name": "test-svc", "repo": "https://github.com/org/test-svc"},
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text
    assert "not in the deploy allowlist" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Deploy — lock contention (409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_lock_contention(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Deploy returns 409 when a deploy is already in progress."""
    from robotsix_central_deploy.lifecycle.deploy_lock import try_acquire_deploy_lock

    _register_component("test-svc")
    _configure_deploy_allowlist("test-svc")

    acquired = await try_acquire_deploy_lock("test-svc")
    assert acquired

    try:
        resp = await client.post(
            "/chat/deploy",
            json={"name": "test-svc", "repo": "https://github.com/org/test-svc"},
            headers=auth_headers,
        )
        assert resp.status_code == 409, resp.text
        assert "already in progress" in resp.json()["detail"]
    finally:
        from robotsix_central_deploy.lifecycle.deploy_lock import release_deploy_lock

        release_deploy_lock("test-svc")


# ---------------------------------------------------------------------------
# Deploy — backend failure (500)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_backend_failure(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Backend.deploy raising an exception results in 500."""
    _register_component("test-svc")
    _configure_deploy_allowlist("test-svc")

    mock = MagicMock()
    mock.deploy = AsyncMock(side_effect=RuntimeError("image not found"))
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/deploy",
        json={"name": "test-svc", "repo": "https://github.com/org/test-svc"},
        headers=auth_headers,
    )
    assert resp.status_code == 500, resp.text
    assert "image not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Deploy — rate limited (429)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_rate_limited(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Second deploy within cooldown window returns 429."""
    _register_component("test-svc")
    _configure_deploy_allowlist("test-svc")

    outcome = DeployOutcome(
        deployed_digest="sha256:abc",
        previous_digest="sha256:prev",
        state=ServiceState.RUNNING,
    )
    mock = MagicMock()
    mock.deploy = AsyncMock(return_value=outcome)
    server_mod.app.state.backend = mock

    resp1 = await client.post(
        "/chat/deploy",
        json={"name": "test-svc", "repo": "https://github.com/org/test-svc"},
        headers=auth_headers,
    )
    assert resp1.status_code == 200

    resp2 = await client.post(
        "/chat/deploy",
        json={"name": "test-svc", "repo": "https://github.com/org/test-svc"},
        headers=auth_headers,
    )
    assert resp2.status_code == 429, resp2.text
    assert "Rate limit" in resp2.json()["detail"]


# ---------------------------------------------------------------------------
# Deploy — sibling deploy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_sibling_deploy(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Deploy with siblings deploys siblings inline."""
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

    _configure_deploy_allowlist("test-svc")

    outcome = DeployOutcome(
        deployed_digest="sha256:abc",
        previous_digest="sha256:prev",
        state=ServiceState.RUNNING,
    )
    mock = MagicMock()
    mock.deploy = AsyncMock(return_value=outcome)
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/deploy",
        json={"name": "test-svc", "repo": "https://github.com/org/test-svc"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "test-svc-worker" in data["deployed_siblings"]
    assert mock.deploy.call_count == 2


# ---------------------------------------------------------------------------
# Authentication — missing auth (401)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_missing_auth(
    client: AsyncClient,
) -> None:
    """Restart without auth headers returns 401."""
    resp = await client.post("/chat/services/test-svc/restart")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_update_missing_auth(
    client: AsyncClient,
) -> None:
    """Update without auth headers returns 401."""
    resp = await client.post("/chat/services/test-svc/update")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_deploy_missing_auth(
    client: AsyncClient,
) -> None:
    """Deploy without auth headers returns 401."""
    resp = await client.post("/chat/deploy", json={"name": "x", "repo": "r"})
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Authentication — invalid auth (401)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_invalid_auth(
    client: AsyncClient,
) -> None:
    """Restart with wrong API key returns 401."""
    resp = await client.post(
        "/chat/services/test-svc/restart",
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401, resp.text


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


@pytest.mark.asyncio
async def test_update_audit_entry(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Successful update writes an audit entry with 'update' action."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)

    outcome = DeployOutcome(
        deployed_digest="sha256:abc123",
        previous_digest="sha256:prev",
        state=ServiceState.RUNNING,
    )
    mock = MagicMock()
    mock.deploy = AsyncMock(return_value=outcome)
    server_mod.app.state.backend = mock

    await client.post(
        "/chat/services/test-svc/update",
        headers=auth_headers,
    )

    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    update_entries = [e for e in entries if e.action == "update"]
    assert len(update_entries) >= 1
    entry = update_entries[-1]
    assert entry.component == "test-svc"
    assert "sha256:abc123" in entry.detail


@pytest.mark.asyncio
async def test_deploy_audit_entry(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Successful deploy writes an audit entry with 'deploy' action."""
    _register_component("test-svc")
    _configure_deploy_allowlist("test-svc")

    outcome = DeployOutcome(
        deployed_digest="sha256:deploy123",
        previous_digest="sha256:prev",
        state=ServiceState.RUNNING,
    )
    mock = MagicMock()
    mock.deploy = AsyncMock(return_value=outcome)
    server_mod.app.state.backend = mock

    await client.post(
        "/chat/deploy",
        json={"name": "test-svc", "repo": "https://github.com/org/test-svc"},
        headers=auth_headers,
    )

    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    deploy_entries = [e for e in entries if e.action == "deploy"]
    assert len(deploy_entries) >= 1
    entry = deploy_entries[-1]
    assert entry.component == "test-svc"
    assert "sha256:deploy123" in entry.detail
