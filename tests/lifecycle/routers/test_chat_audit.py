"""Tests for the chat-agent audit-log endpoint (chat_audit.py).

Mirrors the source-side module split: chat_audit.py owns ``GET
/chat/audit-log``.  These cases were migrated out of the flat
``test_chat_agent.py`` aggregate so the test side maps 1:1 to the
modular chat routers.
"""

from __future__ import annotations

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
# Audit log — GET /chat/audit-log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_audit_log(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
    backend: NoopBackend,
):
    """GET /chat/audit-log returns recent audit entries."""
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))

    # Perform a restart to generate an audit entry.
    await client.post("/chat/services/chat/restart", headers=auth_headers)

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
    assert entry["action"] == "restart"


@pytest.mark.asyncio
async def test_chat_audit_log_filtered(
    client: AsyncClient,
    auth_headers: dict[str, str],
    store: InMemoryStore,
    config_yaml_store: ConfigYamlStore,
    backend: NoopBackend,
):
    """GET /chat/audit-log?component=cognee filters by component."""
    await config_yaml_store.save_template("cognee", _CONFIG_TEMPLATE)
    await config_yaml_store.save_template("chat", _CONFIG_TEMPLATE)
    await store.put(ServiceRecord(name="chat", state=ServiceState.RUNNING))
    await store.put(ServiceRecord(name="cognee", state=ServiceState.RUNNING))

    await client.post("/chat/services/cognee/restart", headers=auth_headers)

    resp = await client.get(
        "/chat/audit-log?component=cognee",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    for entry in data["entries"]:
        assert entry["component"] == "cognee"
