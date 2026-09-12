"""Tests for the chat-agent mutation-permission endpoints (chat_mutation.py).

Mirrors the source-side module split: chat_mutation.py owns
``POST /chat/services/{name}/enable-mutation`` and
``POST /chat/services/{name}/disable-mutation``.  These cases were
migrated out of the flat ``test_chat_agent.py`` aggregate so the test
side maps 1:1 to the modular chat routers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
        chat_agent_deployable_components=["chat", "auto-mail"],
    )


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


class _VolumeNoopBackend(NoopBackend):
    """NoopBackend that retains config-volume contents.

    The config endpoints read and write the component's own config file
    rather than a deploy-plane copy, so the tests need a backend that
    actually stores one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.volumes: dict[str, dict] = {}

    async def read_config_from_volume(self, volume_name: str) -> dict:
        return dict(self.volumes.get(volume_name, {}))

    async def write_config_to_volume(self, volume_name: str, data: dict) -> None:
        self.volumes[volume_name] = dict(data)


@pytest.fixture
def backend() -> NoopBackend:
    return _VolumeNoopBackend()


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
    """Empty headers — component-level auth was removed (auth-removal epic).

    The fleet edge (Traefik + tinyauth) is the only gate; every request
    arriving here is already authenticated, so tests send no credentials.
    The fixture is kept so existing test signatures stay stable.
    """
    return {}


# ---------------------------------------------------------------------------
# Enable / disable mutation
# ---------------------------------------------------------------------------


async def test_enable_mutation_happy_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
    component_config_store: ComponentConfigStore,
    audit_store: ChatAgentAuditStore,
):
    """POST /chat/services/{name}/enable-mutation sets chat_agent_mutatable=True."""
    # other-svc starts with chat_agent_mutatable=False.
    cfg_before = component_config_store.get("other-svc")
    assert cfg_before is not None
    assert cfg_before.chat_agent_mutatable is False

    resp = await client.post(
        "/chat/services/other-svc/enable-mutation",
        json={},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "other-svc"
    assert data["action"] == "enable-mutation"
    assert data["previous"] is False
    assert data["current"] is True
    assert data["ttl_seconds"] is None

    # Verify the flag was persisted.
    cfg_after = component_config_store.get("other-svc")
    assert cfg_after is not None
    assert cfg_after.chat_agent_mutatable is True

    # Verify audit entry.
    entries = await audit_store.list(limit=5, component="other-svc")
    assert len(entries) >= 1
    entry = entries[0]
    assert entry.action == "enable-mutation"
    assert entry.component == "other-svc"
    assert "False → True" in entry.detail


@pytest.mark.asyncio
async def test_disable_mutation_happy_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
    component_config_store: ComponentConfigStore,
    audit_store: ChatAgentAuditStore,
):
    """POST /chat/services/{name}/disable-mutation sets chat_agent_mutatable=False."""
    # chat starts with chat_agent_mutatable=True (from fixture).
    cfg_before = component_config_store.get("chat")
    assert cfg_before is not None
    assert cfg_before.chat_agent_mutatable is True

    resp = await client.post(
        "/chat/services/chat/disable-mutation",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "chat"
    assert data["action"] == "disable-mutation"
    assert data["previous"] is True
    assert data["current"] is False

    # Verify the flag was persisted.
    cfg_after = component_config_store.get("chat")
    assert cfg_after is not None
    assert cfg_after.chat_agent_mutatable is False

    # Verify audit entry.
    entries = await audit_store.list(limit=5, component="chat")
    enable_entries = [e for e in entries if e.action == "disable-mutation"]
    assert len(enable_entries) >= 1
    entry = enable_entries[0]
    assert "True → False" in entry.detail


@pytest.mark.asyncio
async def test_enable_mutation_idempotent(
    client: AsyncClient,
    auth_headers: dict[str, str],
    component_config_store: ComponentConfigStore,
    audit_store: ChatAgentAuditStore,
):
    """Enabling an already-enabled stub is idempotent."""
    # chat already has chat_agent_mutatable=True.
    resp = await client.post(
        "/chat/services/chat/enable-mutation",
        json={},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["previous"] is True
    assert data["current"] is True
    assert "idempotent" in data["detail"].lower()

    # Flag unchanged.
    cfg = component_config_store.get("chat")
    assert cfg is not None
    assert cfg.chat_agent_mutatable is True

    # Audit entry still written.
    entries = await audit_store.list(limit=5, component="chat")
    enable_entries = [e for e in entries if e.action == "enable-mutation"]
    assert len(enable_entries) >= 1


@pytest.mark.asyncio
async def test_disable_mutation_idempotent(
    client: AsyncClient,
    auth_headers: dict[str, str],
    component_config_store: ComponentConfigStore,
    audit_store: ChatAgentAuditStore,
):
    """Disabling an already-disabled stub is idempotent."""
    # other-svc already has chat_agent_mutatable=False.
    resp = await client.post(
        "/chat/services/other-svc/disable-mutation",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["previous"] is False
    assert data["current"] is False
    assert "idempotent" in data["detail"].lower()

    # Flag unchanged.
    cfg = component_config_store.get("other-svc")
    assert cfg is not None
    assert cfg.chat_agent_mutatable is False

    # Audit entry still written.
    entries = await audit_store.list(limit=5, component="other-svc")
    disable_entries = [e for e in entries if e.action == "disable-mutation"]
    assert len(disable_entries) >= 1


@pytest.mark.asyncio
async def test_enable_mutation_with_ttl(
    client: AsyncClient,
    auth_headers: dict[str, str],
    component_config_store: ComponentConfigStore,
    audit_store: ChatAgentAuditStore,
):
    """Enabling with ttl_seconds auto-disables after the TTL."""
    # Use a short TTL for the test.
    resp = await client.post(
        "/chat/services/other-svc/enable-mutation",
        json={"ttl_seconds": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["previous"] is False
    assert data["current"] is True
    assert data["ttl_seconds"] == 1

    # Immediately after, the flag should be True.
    cfg = component_config_store.get("other-svc")
    assert cfg is not None
    assert cfg.chat_agent_mutatable is True

    # Wait for the TTL to fire (plus a small buffer).
    await asyncio.sleep(1.5)

    # After the TTL, the flag should be False.
    cfg = component_config_store.get("other-svc")
    assert cfg is not None
    assert cfg.chat_agent_mutatable is False

    # Verify an auto-disable audit entry was written.
    entries = await audit_store.list(limit=10, component="other-svc")
    auto_entries = [
        e
        for e in entries
        if e.action == "disable-mutation" and "Auto-disabled" in e.detail
    ]
    assert len(auto_entries) >= 1


@pytest.mark.asyncio
async def test_enable_mutation_nonexistent_component(
    client: AsyncClient,
    auth_headers: dict[str, str],
):
    """Enabling mutation on a non-existent component returns 404."""
    resp = await client.post(
        "/chat/services/no-such-component/enable-mutation",
        json={},
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_disable_mutation_nonexistent_component(
    client: AsyncClient,
    auth_headers: dict[str, str],
):
    """Disabling mutation on a non-existent component returns 404."""
    resp = await client.post(
        "/chat/services/no-such-component/disable-mutation",
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mutation_not_gated_by_allowlist(
    client: AsyncClient,
    auth_headers: dict[str, str],
    component_config_store: ComponentConfigStore,
):
    """Enable/disable endpoints work even when chat_agent_mutatable is False.

    This is the key design property: the grant endpoint must NOT be
    gated behind the very toggle it sets.
    """
    # other-svc has chat_agent_mutatable=False but the enable endpoint
    # should still succeed.
    resp_enable = await client.post(
        "/chat/services/other-svc/enable-mutation",
        json={},
        headers=auth_headers,
    )
    assert resp_enable.status_code == 200

    # Now disable it again.
    resp_disable = await client.post(
        "/chat/services/other-svc/disable-mutation",
        headers=auth_headers,
    )
    assert resp_disable.status_code == 200


@pytest.mark.asyncio
async def test_enable_mutation_unlocks_test_deploy(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    component_config_store: ComponentConfigStore,
):
    """After enable-mutation, test-deploy no longer returns 403."""
    # other-svc starts with chat_agent_mutatable=False.
    # test-deploy should return 403.
    await store.put(ServiceRecord(name="other-svc", state=ServiceState.RUNNING))

    # First, verify test-deploy would fail with 403.
    resp_fail = await client.post(
        "/chat/deploy/test",
        json={"stub_name": "other-svc", "website": "http://localhost:9999/health"},
        headers=auth_headers,
    )
    assert resp_fail.status_code == 403
    assert "not permitted to mutate" in resp_fail.json()["error"]

    # Enable mutation.
    resp_enable = await client.post(
        "/chat/services/other-svc/enable-mutation",
        json={},
        headers=auth_headers,
    )
    assert resp_enable.status_code == 200

    # Now test-deploy should proceed past the 403 (it will fail at the
    # probe stage because there's no real container, but the gate is open).
    resp_test = await client.post(
        "/chat/deploy/test",
        json={"stub_name": "other-svc", "website": "http://localhost:9999/health"},
        headers=auth_headers,
    )
    # No longer 403 — should be a different error (deploy or probe phase).
    assert resp_test.status_code != 403, (
        f"Expected non-403 after enable, got {resp_test.status_code}: {resp_test.text}"
    )
