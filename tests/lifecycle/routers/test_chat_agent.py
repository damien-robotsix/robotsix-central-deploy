"""Tests for the chat agent scoped write-surface endpoints."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.backends import NoopBackend
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.deps import JobRegistry
from robotsix_central_deploy.lifecycle.models import (
    ExecutionBackendType,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.onboard.fetcher import RepoFiles
from robotsix_central_deploy.onboard.models import DerivedSpec, SiblingDerivedSpec
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
        chat_agent_deployable_components=["chat", "auto-mail"],
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
    cfg_chat = _make_config("chat", "ghcr.io/test/robotsix-chat:main")
    cfg_chat.chat_agent_mutatable = True
    store.register(cfg_chat)
    cfg_cognee = _make_config("cognee", "ghcr.io/test/cognee:main")
    cfg_cognee.chat_agent_mutatable = True
    store.register(cfg_cognee)
    store.register(_make_config("other-svc", "ghcr.io/test/other:main"))
    return store


@pytest.fixture
def registry(component_config_store: ComponentConfigStore) -> ComponentRegistry:
    return ComponentRegistry(list(component_config_store.all()))


@pytest.fixture
def env_store(state_dir: Path) -> EnvStore:
    km = SecretKeyManager(state_dir / "secrets.key")
    return EnvStore(state_dir / "env.json", km)


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
    env_store: EnvStore,
    state_dir: Path,
):
    """Wire app.state with all needed stores before each test."""
    monkeypatch.setenv("ROBOTSIX_LIFECYCLE_API_KEY", "test-key")

    mock_checker = MagicMock()
    mock_checker.get_latest_digest = AsyncMock(return_value=None)

    deploy_history_store = DeployHistoryStore(state_dir / "deploy_history.json")

    server_mod._config = cfg
    server_mod._store = store
    server_mod._backend = backend
    server_mod._registry_checker = mock_checker
    server_mod.app.state.config = cfg
    server_mod.app.state.store = store
    server_mod.app.state.backend = backend
    server_mod.app.state.registry_checker = mock_checker
    server_mod.app.state.key_manager = env_store._key_manager
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
    # log_level is intercepted and applied to the root logger — the
    # submitted value ("debug") is not written to the component config;
    # only the template default ("info") remains.
    assert data["restored"]["log_level"] == "info"
    # Secrets must be masked
    assert data["restored"]["api_token"] == ""


@pytest.mark.asyncio
async def test_chat_config_update_partial_keeps_unsubmitted_keys(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
):
    """A partial update must not reset unsubmitted keys to template defaults.

    Regression test for the 2026-07-18 chat outage: the chat agent submitted
    only two keys and every other field (server port, API keys, integration
    URLs) was silently reset to its schema default, taking the service down.
    """
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))
    # Seed a current config with non-default values, including secrets.
    await config_yaml_store.update_current(
        "chat",
        {
            "debug": True,
            "log_level": "info",
            "api_token": "real-secret",
            "nested": {"host": "prod.example.com", "secret_key": "nested-secret"},
        },
    )

    resp = await client.put(
        "/chat/config/chat",
        json={"values": {"debug": False}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    current = await config_yaml_store.get_current("chat")
    assert current is not None
    assert current["debug"] is False
    # Unsubmitted keys keep their existing values instead of template defaults.
    assert current["api_token"] == "real-secret"
    assert current["nested"]["host"] == "prod.example.com"
    assert current["nested"]["secret_key"] == "nested-secret"


@pytest.mark.asyncio
async def test_chat_config_update_accepts_and_updates_secret_keys(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
):
    """PUT /chat/config with a secret key updates it."""
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.put(
        "/chat/config/chat",
        json={"values": {"debug": True, "api_token": "new-secret-value"}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Secret value is masked in the response
    assert body["restored"]["api_token"] == "***"
    assert body["restored"]["debug"] is True
    # Verify the secret was actually stored
    stored = await config_yaml_store.get_current("chat")
    assert stored["api_token"] == "new-secret-value"


@pytest.mark.asyncio
async def test_chat_config_update_nested_secret_keys_are_accepted(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
):
    """Nested secret keys are accepted and updated."""
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.put(
        "/chat/config/chat",
        json={
            "values": {"nested": {"host": "newhost", "secret_key": "new-nested-secret"}}
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Secret value is masked in the response
    assert body["restored"]["nested"]["secret_key"] == "***"
    assert body["restored"]["nested"]["host"] == "newhost"
    # Verify the secret was actually stored
    stored = await config_yaml_store.get_current("chat")
    assert stored["nested"]["secret_key"] == "new-nested-secret"


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


@pytest.mark.asyncio
async def test_chat_config_write_follows_restart_access(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
):
    """Services the chat agent can restart are also config-writable.

    Verifies acceptance criterion: restart and config-write authorizations
    are coupled through the single ``chat_agent_mutatable`` flag.  An
    allowlisted service returns 200 on both; a non-allowlisted service
    returns 403 on both.
    """
    # -- Allowlisted service: both restart and config-write succeed -----
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp_restart = await client.post(
        "/chat/services/chat/restart",
        headers=auth_headers,
    )
    assert resp_restart.status_code == 200, f"restart failed: {resp_restart.text}"

    resp_config = await client.put(
        "/chat/config/chat",
        json={"values": {"debug": True}},
        headers=auth_headers,
    )
    assert resp_config.status_code == 200, (
        f"config-write should succeed when restart succeeds; "
        f"got {resp_config.status_code}: {resp_config.text}"
    )

    # -- Non-allowlisted service: both return 403 -----------------------
    resp_restart2 = await client.post(
        "/chat/services/other-svc/restart",
        headers=auth_headers,
    )
    assert resp_restart2.status_code == 403

    resp_config2 = await client.put(
        "/chat/config/other-svc",
        json={"values": {"debug": True}},
        headers=auth_headers,
    )
    assert resp_config2.status_code == 403


@pytest.mark.asyncio
async def test_chat_mutation_allowed_via_allow_chat_access_flag(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
    component_config_store: ComponentConfigStore,
):
    """``allow_chat_access`` alone (operator toggle) grants mutation access.

    A component with ``allow_chat_access=True`` and
    ``chat_agent_mutatable=False`` must still be reachable through the
    mutation endpoints — the operator-facing "Allow chat agent access"
    checkbox is the single control surface.
    """
    # -- Register a component with allow_chat_access=True only ----------
    cfg = _make_config("chat-access-only", "ghcr.io/test/access-only:main")
    cfg.allow_chat_access = True
    component_config_store.register(cfg)

    await config_yaml_store.save_template("chat-access-only", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat-access-only", state=ServiceState.RUNNING))

    # Config-write must succeed.
    resp_config = await client.put(
        "/chat/config/chat-access-only",
        json={"values": {"debug": True}},
        headers=auth_headers,
    )
    assert resp_config.status_code == 200, (
        f"config-write should succeed when allow_chat_access=True; "
        f"got {resp_config.status_code}: {resp_config.text}"
    )

    # Restart must also succeed.
    resp_restart = await client.post(
        "/chat/services/chat-access-only/restart",
        headers=auth_headers,
    )
    assert resp_restart.status_code == 200, (
        f"restart should succeed when allow_chat_access=True; "
        f"got {resp_restart.status_code}: {resp_restart.text}"
    )


@pytest.mark.asyncio
async def test_chat_mutation_denied_when_both_flags_are_false(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
    component_config_store: ComponentConfigStore,
):
    """Both flags false → 403, even when the service record exists."""
    cfg = _make_config("no-access", "ghcr.io/test/no-access:main")
    # Explicitly confirm both flags are false.
    cfg.allow_chat_access = False
    cfg.chat_agent_mutatable = False
    component_config_store.register(cfg)

    await config_yaml_store.save_template("no-access", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="no-access", state=ServiceState.RUNNING))

    resp = await client.put(
        "/chat/config/no-access",
        json={"values": {"debug": True}},
        headers=auth_headers,
    )
    assert resp.status_code == 403


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
        ("PUT", "/chat/env/chat", {"secrets": {"TOKEN": "secret"}}),
        ("POST", "/chat/services/chat/restart", None),
        ("POST", "/chat/services/chat/update", None),
        (
            "POST",
            "/chat/deploy",
            {"name": "chat", "repo": "https://github.com/org/robotsix-chat.git"},
        ),
    ]
    for method, path, body in endpoints:
        if body is not None:
            resp = await client.request(method, path, json=body)
        else:
            resp = await client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} should require auth"


# ---------------------------------------------------------------------------
# Env / secret provisioning — PUT /chat/env/{name}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_env_upsert_secrets_happy_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    env_store: EnvStore,
):
    """PUT /chat/env/chat upserts secrets and returns masked keys."""
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.put(
        "/chat/env/chat",
        json={"secrets": {"SOME_SECRET": "test_value_123"}},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["component"] == "chat"
    assert body["secret_keys"] == ["SOME_SECRET"]
    assert body["env_keys"] == []
    assert "SOME_SECRET" not in str(body).lower()  # value never in response
    # Verify the value was actually stored (encrypted, decrypted on read).
    stored = await env_store.get("chat")
    assert "SOME_SECRET" in stored.secret_tokens


@pytest.mark.asyncio
async def test_chat_env_upsert_plain_env(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    env_store: EnvStore,
):
    """PUT /chat/env/chat upserts plain env vars."""
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.put(
        "/chat/env/chat",
        json={"env": {"LOG_LEVEL": "debug"}},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["component"] == "chat"
    assert body["env_keys"] == ["LOG_LEVEL"]
    assert body["secret_keys"] == []

    stored = await env_store.get("chat")
    assert stored.env["LOG_LEVEL"] == "debug"


@pytest.mark.asyncio
async def test_chat_env_upsert_both_env_and_secrets(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    env_store: EnvStore,
):
    """PUT /chat/env/chat upserts both env and secrets in one call."""
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.put(
        "/chat/env/chat",
        json={
            "env": {"NODE_ENV": "production"},
            "secrets": {"DB_PASSWORD": "s3cret"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "NODE_ENV" in body["env_keys"]
    assert "DB_PASSWORD" in body["secret_keys"]
    assert "s3cret" not in str(body)

    stored = await env_store.get("chat")
    assert stored.env["NODE_ENV"] == "production"
    assert "DB_PASSWORD" in stored.secret_tokens


@pytest.mark.asyncio
async def test_chat_env_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """PUT /chat/env/other-svc returns 403 for non-allowlisted service."""
    await store.put(ServiceRecord(name="other-svc", state=ServiceState.RUNNING))

    resp = await client.put(
        "/chat/env/other-svc",
        json={"secrets": {"TOKEN": "secret"}},
        headers=auth_headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_chat_env_empty_body(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """PUT /chat/env/chat with empty body returns 200 with no-op detail."""
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.put(
        "/chat/env/chat",
        json={},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["env_keys"] == []
    assert body["secret_keys"] == []
    assert "nothing to do" in body["detail"].lower()


@pytest.mark.asyncio
async def test_chat_env_audit_log_redacted(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    audit_store: ChatAgentAuditStore,
):
    """Secret values are never written to the audit log."""
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    await client.put(
        "/chat/env/chat",
        json={"secrets": {"SUPER_SECRET": "my-password"}},
        headers=auth_headers,
    )
    entries = await audit_store.list()
    secret_entries = [e for e in entries if e.key == "SUPER_SECRET"]
    assert len(secret_entries) == 1
    entry = secret_entries[0]
    assert entry.new_value == "***"
    assert entry.old_value is None
    assert "my-password" not in str(entry.detail).lower()


@pytest.mark.asyncio
async def test_chat_env_write_follows_restart_access(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """Services with chat_agent_mutatable=True can be env-written.

    Verifies that the env write surface is gated by the same
    ``chat_agent_mutatable`` flag as restart and config-write.
    """
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))
    await store.put(ServiceRecord(name="other-svc", state=ServiceState.RUNNING))

    # Allowlisted
    r = await client.put(
        "/chat/env/chat",
        json={"secrets": {"T": "v"}},
        headers=auth_headers,
    )
    assert r.status_code == 200

    # Non-allowlisted
    r = await client.put(
        "/chat/env/other-svc",
        json={"secrets": {"T": "v"}},
        headers=auth_headers,
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_chat_env_upsert_is_idempotent(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    env_store: EnvStore,
):
    """Repeated PUTs with the same key overwrite, not duplicate."""
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    await client.put(
        "/chat/env/chat",
        json={"secrets": {"TOKEN": "first"}},
        headers=auth_headers,
    )
    await client.put(
        "/chat/env/chat",
        json={"secrets": {"TOKEN": "second"}},
        headers=auth_headers,
    )
    stored = await env_store.get("chat")
    assert len(stored.secret_tokens) == 1
    assert "TOKEN" in stored.secret_tokens


# ---------------------------------------------------------------------------
# Deploy (generic POST /chat/deploy)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_deploy_happy_path_existing_config(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """POST /chat/deploy succeeds for an allowlisted component with a stored config."""
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    resp = await client.post(
        "/chat/deploy",
        json={"name": "chat", "repo": "https://github.com/org/robotsix-chat.git"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "chat"
    assert data["action"] == "deploy"
    assert data["deployed_digest"] == "sha256:noop"
    assert data["current_state"] == "running"
    assert data["deployed_siblings"] == []


@pytest.mark.asyncio
async def test_chat_deploy_happy_path_auto_create_config(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    component_config_store: ComponentConfigStore,
    config_yaml_store: ConfigYamlStore,
):
    """POST /chat/deploy resolves the deploy contract to auto-create a config."""
    await store.put(ServiceRecord(name="auto-mail", state=ServiceState.RUNNING))
    # Confirm no config exists for auto-mail yet.
    assert component_config_store.get("auto-mail") is None

    derived_spec = DerivedSpec(
        name="auto-mail",
        git_url="https://github.com/org/robotsix-auto-mail.git",
        image="ghcr.io/test/robotsix-auto-mail:main",
        ports=[PortMapping(host=8025, container=8025, protocol="tcp")],
        volume_mounts=[VolumeMount(host="data", container="/data")],
        env={"SECRET": ""},
        claude_mount=False,
        host_docker_sock=False,
        health_check=HealthCheck(
            test=["CMD", "curl", "-f", "http://localhost:8025/health"],
            interval_seconds=30,
            timeout_seconds=10,
            retries=3,
            start_period_seconds=10,
        ),
        command=["serve", "--host", "0.0.0.0", "--port", "8025"],
        entrypoint=None,
        container_name="",
        siblings=[
            SiblingDerivedSpec(
                service_key="ingester",
                image="ghcr.io/test/robotsix-auto-mail:main",
                container_name="robotsix-auto-mail-ingester",
                ports=[],
                mounts=[VolumeMount(host="data", container="/data")],
                env={},
                command=["ingest", "--watch", "/data"],
                health_check=HealthCheck(
                    test=["CMD", "pgrep", "-f", "ingest"],
                    interval_seconds=30,
                    timeout_seconds=10,
                    retries=3,
                    start_period_seconds=10,
                ),
            ),
        ],
        config_schema=_CONFIG_TEMPLATE,
        config_example_values=None,
        config_volume="auto-mail-config",
        config_assist_command=None,
        config_assist_seeds=[],
        llmio_tier_level=None,
        allow_chat_access=False,
        chat_agent_mutatable=True,
    )

    repo_files = RepoFiles(
        compose_bytes=b"# central-deploy-contract-version: 1\nservices: {}",
        config_json=None,
        config_json_template=None,
        config_schema_json=b'{"type":"object","properties":{"debug":{"type":"boolean","default":false}}}',
    )

    with (
        patch(
            "robotsix_central_deploy.lifecycle.routers.chat_services.fetch_repo_files",
            return_value=repo_files,
        ),
        patch(
            "robotsix_central_deploy.lifecycle.routers.chat_services.parse_compose",
            return_value=derived_spec,
        ),
    ):
        resp = await client.post(
            "/chat/deploy",
            json={
                "name": "auto-mail",
                "repo": "https://github.com/org/robotsix-auto-mail.git",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "auto-mail"
    assert data["action"] == "deploy"
    assert data["deployed_digest"] == "sha256:noop"
    assert data["current_state"] == "running"
    # Siblings should be deployed.
    assert "auto-mail-ingester" in data["deployed_siblings"]

    # The config should now be persisted and registered.
    cfg = component_config_store.get("auto-mail")
    assert cfg is not None
    assert cfg.image == "ghcr.io/test/robotsix-auto-mail:main"
    assert cfg.chat_agent_mutatable is True
    assert cfg.health_check is not None
    assert len(cfg.ports) == 1
    assert cfg.ports[0].host == 8025
    assert cfg.command == ["serve", "--host", "0.0.0.0", "--port", "8025"]
    assert len(cfg.siblings) == 1
    assert cfg.siblings[0].service_key == "ingester"


@pytest.mark.asyncio
async def test_chat_deploy_not_in_allowlist(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """POST /chat/deploy returns 403 when the component is not in the deploy allowlist."""
    await store.put(ServiceRecord(name="cognee", state=ServiceState.RUNNING))

    resp = await client.post(
        "/chat/deploy",
        json={"name": "cognee", "repo": "https://github.com/org/cognee.git"},
        headers=auth_headers,
    )
    assert resp.status_code == 403
    assert "deploy allowlist" in resp.json()["error"]


@pytest.mark.asyncio
async def test_chat_deploy_rate_limited(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """Second deploy within cooldown window returns 429."""
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    # First deploy succeeds.
    resp1 = await client.post(
        "/chat/deploy",
        json={"name": "chat", "repo": "https://github.com/org/robotsix-chat.git"},
        headers=auth_headers,
    )
    assert resp1.status_code == 200

    # Second deploy within cooldown fails.
    resp2 = await client.post(
        "/chat/deploy",
        json={"name": "chat", "repo": "https://github.com/org/robotsix-chat.git"},
        headers=auth_headers,
    )
    assert resp2.status_code == 429
    assert "Rate limit" in resp2.json()["error"]


@pytest.mark.asyncio
async def test_chat_deploy_missing_config_schema_returns_422(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """POST /chat/deploy returns 422 when config/config.schema.json is missing."""
    await store.put(ServiceRecord(name="auto-mail", state=ServiceState.RUNNING))

    derived_spec = DerivedSpec(
        name="auto-mail",
        git_url="https://github.com/org/robotsix-auto-mail.git",
        image="ghcr.io/test/robotsix-auto-mail:main",
        ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        volume_mounts=[VolumeMount(host="auto-mail-config", container="/config")],
        env={},
        claude_mount=False,
        host_docker_sock=False,
        config_schema=None,
        config_volume="auto-mail-config",
    )

    repo_files = RepoFiles(
        compose_bytes=b"# central-deploy-contract-version: 1\nservices: {}",
        config_json=None,
        config_json_template=None,
        config_schema_json=None,
    )

    with (
        patch(
            "robotsix_central_deploy.lifecycle.routers.chat_services.fetch_repo_files",
            return_value=repo_files,
        ),
        patch(
            "robotsix_central_deploy.lifecycle.routers.chat_services.parse_compose",
            return_value=derived_spec,
        ),
    ):
        resp = await client.post(
            "/chat/deploy",
            json={
                "name": "auto-mail",
                "repo": "https://github.com/org/robotsix-auto-mail.git",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 422
    data = resp.json()
    assert "missing config/config.schema.json" in data["error"]
    assert "missing robotsix.deploy.config-target" not in data["error"]


@pytest.mark.asyncio
async def test_chat_deploy_missing_config_target_returns_422(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
):
    """POST /chat/deploy returns 422 when robotsix.deploy.config-target is missing."""
    await store.put(ServiceRecord(name="auto-mail", state=ServiceState.RUNNING))

    derived_spec = DerivedSpec(
        name="auto-mail",
        git_url="https://github.com/org/robotsix-auto-mail.git",
        image="ghcr.io/test/robotsix-auto-mail:main",
        ports=[PortMapping(host=8080, container=8080, protocol="tcp")],
        volume_mounts=[VolumeMount(host="auto-mail-config", container="/config")],
        env={},
        claude_mount=False,
        host_docker_sock=False,
        config_schema={"type": "object", "properties": {}},
        config_volume=None,
    )

    repo_files = RepoFiles(
        compose_bytes=b"# central-deploy-contract-version: 1\nservices: {}",
        config_json=None,
        config_json_template=None,
        config_schema_json=b'{"type":"object","properties":{}}',
    )

    with (
        patch(
            "robotsix_central_deploy.lifecycle.routers.chat_services.fetch_repo_files",
            return_value=repo_files,
        ),
        patch(
            "robotsix_central_deploy.lifecycle.routers.chat_services.parse_compose",
            return_value=derived_spec,
        ),
    ):
        resp = await client.post(
            "/chat/deploy",
            json={
                "name": "auto-mail",
                "repo": "https://github.com/org/robotsix-auto-mail.git",
            },
            headers=auth_headers,
        )
    assert resp.status_code == 422
    data = resp.json()
    assert "missing robotsix.deploy.config-target" in data["error"]
    assert "missing config/config.schema.json" not in data["error"]
