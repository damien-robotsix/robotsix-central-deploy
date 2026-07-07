"""Tests for the chat agent scoped write-surface endpoints."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.lifecycle.backends import NoopBackend
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.deps import JobRegistry
from robotsix_central_deploy.lifecycle.models import (
    ExecutionBackendType,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.registry.chat_agent_audit_store import ChatAgentAuditStore
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.config_yaml_store import ConfigYamlStore
from robotsix_central_deploy.registry.deploy_history_store import DeployHistoryStore
from robotsix_central_deploy.registry.env_store import EnvStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import (
    ComponentConfig,
    HealthCheck,
    PortMapping,
    VolumeMount,
)
from robotsix_central_deploy.registry.secret_key import SecretKeyManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    component_id: str = "chat",
    image: str = "repo:v1",
) -> ComponentConfig:
    return ComponentConfig(
        id=component_id,
        image=image,
        container_name=component_id,
        ports=[PortMapping(host=8080, container=8080)],
        mounts=[VolumeMount(host="/data", container="/data")],
        env={"KEY": "val"},
        health_check=HealthCheck(
            test=["CMD", "curl", "-f", "http://localhost:8080/health"],
            interval_seconds=30,
            timeout_seconds=10,
            retries=3,
            start_period_seconds=10,
        ),
        config_volume="test-config-vol",
    )


# A minimal JSON Schema template for config testing.
_CONFIG_TEMPLATE: dict = {
    "type": "object",
    "properties": {
        "debug": {"type": "boolean", "default": False},
        "log_level": {"type": "string", "default": "info"},
        "api_token": {
            "type": "string",
            "format": "password",
            "writeOnly": True,
        },
        "nested": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "localhost"},
                "secret_key": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "_chat_test"
    d.mkdir(exist_ok=True)
    return d


@pytest.fixture
def cfg() -> LifecycleConfig:
    return LifecycleConfig(  # type: ignore[call-arg]
        store_backend="memory",
        execution_backend=ExecutionBackendType.NOOP,
        api_key="test-key",
    )


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def backend() -> NoopBackend:
    return NoopBackend()


@pytest.fixture
def config_yaml_store(state_dir: Path) -> ConfigYamlStore:
    return ConfigYamlStore(state_dir / "config_yaml.json")


@pytest.fixture
def audit_store(state_dir: Path) -> ChatAgentAuditStore:
    return ChatAgentAuditStore(state_dir / "chat_agent_audit.json")


@pytest.fixture
def component_config_store(state_dir: Path) -> ComponentConfigStore:
    store = ComponentConfigStore(state_dir / "component_configs.json")
    store.register(_make_config("chat", "ghcr.io/test/robotsix-chat:main"))
    store.register(_make_config("cognee", "ghcr.io/test/cognee:main"))
    store.register(_make_config("other-svc", "ghcr.io/test/other:main"))
    return store


@pytest.fixture
def registry(component_config_store: ComponentConfigStore) -> ComponentRegistry:
    return ComponentRegistry(list(component_config_store.all()))


@pytest.fixture(autouse=True)
def _wire_app_state(
    monkeypatch,
    cfg: LifecycleConfig,
    store: InMemoryStore,
    backend: NoopBackend,
    config_yaml_store: ConfigYamlStore,
    audit_store: ChatAgentAuditStore,
    component_config_store: ComponentConfigStore,
    registry: ComponentRegistry,
    state_dir: Path,
):
    """Wire app.state with all needed stores before each test."""
    monkeypatch.setenv("ROBOTSIX_LIFECYCLE_API_KEY", "test-key")

    mock_checker = MagicMock()
    mock_checker.get_latest_digest = AsyncMock(return_value=None)

    km = SecretKeyManager(state_dir / "secrets.key")
    env_store = EnvStore(state_dir / "env.json", km)
    deploy_history_store = DeployHistoryStore(state_dir / "deploy_history.json")

    server_mod._config = cfg
    server_mod._store = store
    server_mod._backend = backend
    server_mod._registry_checker = mock_checker
    server_mod.app.state.config = cfg
    server_mod.app.state.store = store
    server_mod.app.state.backend = backend
    server_mod.app.state.registry_checker = mock_checker
    server_mod.app.state.key_manager = km
    server_mod.app.state.env_store = env_store
    server_mod.app.state.config_yaml_store = config_yaml_store
    server_mod.app.state.deploy_history_store = deploy_history_store
    server_mod.app.state.chat_agent_audit_store = audit_store
    server_mod.app.state.chat_agent_rate_limits = {}
    server_mod.app.state.component_config_store = component_config_store
    server_mod.app.state.registry = registry
    server_mod.app.state.job_registry = JobRegistry()


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


# ---------------------------------------------------------------------------
# Config update — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_config_update_happy_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
):
    """PUT /chat/config/chat with valid non-secret keys succeeds."""
    # Seed the config template
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    # Seed a service record
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.put(
        "/chat/config/chat",
        json={"values": {"debug": True, "log_level": "debug"}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["component"] == "chat"
    assert "restored" in data
    assert data["restored"]["debug"] is True
    assert data["restored"]["log_level"] == "debug"
    # Secrets must be masked
    assert data["restored"]["api_token"] == ""


@pytest.mark.asyncio
async def test_chat_config_update_rejects_secret_keys(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
):
    """PUT /chat/config with a secret key in the body returns 403."""
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.put(
        "/chat/config/chat",
        json={"values": {"debug": True, "api_token": "leaked-secret"}},
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text
    assert "api_token" in resp.json()["error"]


@pytest.mark.asyncio
async def test_chat_config_update_secret_in_nested_object(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
):
    """Nested secret keys are also rejected with 403."""
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.put(
        "/chat/config/chat",
        json={"values": {"nested": {"host": "newhost", "secret_key": "bad"}}},
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text
    assert "secret_key" in resp.json()["error"]


@pytest.mark.asyncio
async def test_chat_config_update_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
):
    """PUT /chat/config/other-svc returns 403 for non-allowlisted service."""
    await config_yaml_store.save_template("other-svc", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="other-svc", state=ServiceState.RUNNING))

    resp = await client.put(
        "/chat/config/other-svc",
        json={"values": {"debug": True}},
        headers=auth_headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_chat_config_update_no_schema_returns_404(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """PUT /chat/config/chat with no stored template returns 404."""
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.put(
        "/chat/config/chat",
        json={"values": {"debug": True}},
        headers=auth_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Config rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_config_rollback_happy_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
):
    """POST /chat/config/chat/rollback restores previous snapshot."""
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    # First update to create a rollback snapshot.
    await client.put(
        "/chat/config/chat",
        json={"values": {"debug": True, "log_level": "debug"}},
        headers=auth_headers,
    )

    # Then rollback.
    resp = await client.post(
        "/chat/config/chat/rollback",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["component"] == "chat"
    # Restored should have the template defaults (since the "previous" was the template).
    assert data["restored"]["debug"] is False
    assert data["restored"]["log_level"] == "info"


@pytest.mark.asyncio
async def test_chat_config_rollback_no_previous(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
):
    """POST /chat/config/rollback with no stored snapshot returns 404."""
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.post(
        "/chat/config/chat/rollback",
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_chat_config_rollback_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
):
    """POST /chat/config/other-svc/rollback returns 403."""
    resp = await client.post(
        "/chat/config/other-svc/rollback",
        headers=auth_headers,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_restart_happy_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """POST /chat/services/chat/restart succeeds."""
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.post(
        "/chat/services/chat/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "chat"
    assert data["action"] == "restart"
    assert data["previous_state"] == "running"
    # NoopBackend restart transitions to RUNNING.
    assert data["current_state"] == "running"


@pytest.mark.asyncio
async def test_chat_restart_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """POST /chat/services/other-svc/restart returns 403."""
    await store.put(ServiceRecord(name="other-svc", state=ServiceState.RUNNING))

    resp = await client.post(
        "/chat/services/other-svc/restart",
        headers=auth_headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_chat_restart_rate_limited(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """Second restart within cooldown window returns 429."""
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    # First restart succeeds.
    resp1 = await client.post(
        "/chat/services/chat/restart",
        headers=auth_headers,
    )
    assert resp1.status_code == 200

    # Second restart within cooldown fails.
    resp2 = await client.post(
        "/chat/services/chat/restart",
        headers=auth_headers,
    )
    assert resp2.status_code == 429
    assert "Rate limit" in resp2.json()["error"]


# ---------------------------------------------------------------------------
# Update (deploy)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_update_happy_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    backend: NoopBackend,
):
    """POST /chat/services/chat/update succeeds."""
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.post(
        "/chat/services/chat/update",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "chat"
    assert data["action"] == "update"
    assert data["deployed_digest"] == "sha256:noop"
    assert data["current_state"] == "running"


@pytest.mark.asyncio
async def test_chat_update_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """POST /chat/services/other-svc/update returns 403."""
    await store.put(ServiceRecord(name="other-svc", state=ServiceState.RUNNING))

    resp = await client.post(
        "/chat/services/other-svc/update",
        headers=auth_headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_chat_update_rate_limited(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """Second update within cooldown window returns 429."""
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    # First update succeeds.
    resp1 = await client.post(
        "/chat/services/chat/update",
        headers=auth_headers,
    )
    assert resp1.status_code == 200

    # Second update within cooldown fails.
    resp2 = await client.post(
        "/chat/services/chat/update",
        headers=auth_headers,
    )
    assert resp2.status_code == 429
    assert "Rate limit" in resp2.json()["error"]


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_audit_log(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
):
    """GET /chat/audit-log returns recent audit entries."""
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    # Perform a config update to generate an audit entry.
    await client.put(
        "/chat/config/chat",
        json={"values": {"debug": True}},
        headers=auth_headers,
    )

    # Read the audit log.
    resp = await client.get(
        "/chat/audit-log",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["entries"]) >= 1
    entry = data["entries"][0]
    assert entry["component"] == "chat"
    assert entry["action"] == "config_update"
    assert entry["key"] == "debug"
    assert entry["new_value"] is True


@pytest.mark.asyncio
async def test_chat_audit_log_filtered(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
):
    """GET /chat/audit-log?component=cognee filters by component."""
    await config_yaml_store.save_template("cognee", _CONFIG_TEMPLATE)
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))
    await store.put(ServiceRecord(name="cognee", state=ServiceState.RUNNING))

    await client.put(
        "/chat/config/cognee",
        json={"values": {"debug": True}},
        headers=auth_headers,
    )

    resp = await client.get(
        "/chat/audit-log?component=cognee",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    for entry in data["entries"]:
        assert entry["component"] == "cognee"


# ---------------------------------------------------------------------------
# Auth required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoints_require_auth(
    client: AsyncClient,
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
):
    """All chat write endpoints return 401 without auth."""
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    endpoints = [
        ("PUT", "/chat/config/chat", {"values": {"debug": True}}),
        ("POST", "/chat/config/chat/rollback", None),
        ("POST", "/chat/services/chat/restart", None),
        ("POST", "/chat/services/chat/update", None),
    ]
    for method, path, body in endpoints:
        if body is not None:
            resp = await client.request(method, path, json=body)
        else:
            resp = await client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} should require auth"
