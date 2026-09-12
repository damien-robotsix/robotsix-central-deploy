"""Tests for the chat-agent deploy and update endpoints (chat_deploy.py).

Mirrors the source-side module split: chat_deploy.py owns ``POST
/chat/services/{name}/update``, ``POST /chat/services/{name}/deploy``
and ``POST /chat/deploy``.  These cases were migrated out of the flat
``test_chat_services.py`` / ``test_chat_agent.py`` aggregates so the
test side maps 1:1 to the modular chat routers.
"""

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
    DeployOutcome,
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
# Helpers (migrated from test_chat_services.py)
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
# Update — re-fetches deploy contract before recreating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_refreshes_contract_before_deploy(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Update re-fetches the compose contract so compose-only changes apply.

    A compose-only change (e.g. added robotsix.deploy.* proxy labels) has an
    unchanged image digest, so the update path must refresh the stored contract
    before recreating; the applied changes are surfaced in the response.
    """
    import robotsix_central_deploy.lifecycle.routers.chat_deploy as chat_deploy_mod
    from robotsix_central_deploy.lifecycle.deps import ContractRefreshResult

    cfg = _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)

    called: dict[str, str] = {}

    async def _fake_refresh(name, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        called["name"] = name
        return ContractRefreshResult(new_config=cfg, changed_fields=["mounts"])

    monkeypatch.setattr(chat_deploy_mod, "refresh_component_contract", _fake_refresh)

    outcome = DeployOutcome(
        deployed_digest="sha256:abc123def456",
        previous_digest="sha256:abc123def456",
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
    assert called["name"] == "test-svc"
    assert "Contract changes applied" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_contract_refresh_best_effort(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo-fetch/parse failure during refresh must not block an update."""
    from fastapi import HTTPException

    import robotsix_central_deploy.lifecycle.routers.chat_deploy as chat_deploy_mod

    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.RUNNING)

    async def _failing_refresh(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise HTTPException(status_code=422, detail="repo fetch failed")

    monkeypatch.setattr(chat_deploy_mod, "refresh_component_contract", _failing_refresh)

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
    assert "Contract changes applied" not in resp.json()["detail"]


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
    server_mod.app.state.registry._index.pop("test-svc", None)

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
        assert "already in progress" in resp.json()["error"]
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
    assert "pull failed" in resp.json()["error"]


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
    assert "Rate limit" in resp2.json()["error"]


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
    assert "not in the deploy allowlist" in resp.json()["error"]


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
        assert "already in progress" in resp.json()["error"]
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
    assert "image not found" in resp.json()["error"]


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
    assert "Rate limit" in resp2.json()["error"]


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
# Service deploy (POST /chat/services/{name}/deploy) — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_deploy_happy_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/services/{name}/deploy deploys a STOPPED component."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.STOPPED)

    outcome = DeployOutcome(
        deployed_digest="sha256:firstboot123",
        previous_digest="",
        state=ServiceState.RUNNING,
    )
    mock = MagicMock()
    mock.deploy = AsyncMock(return_value=outcome)
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/test-svc/deploy",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "test-svc"
    assert data["action"] == "deploy"
    assert data["deployed_digest"] == "sha256:firstboot123"
    assert data["previous_digest"] == ""
    assert data["current_state"] == "running"
    assert "Deploy completed" in data["detail"]

    # Verify the record was updated.
    stored = await server_mod.app.state.store.get("test-svc")
    assert stored is not None
    assert stored.state == ServiceState.RUNNING
    assert stored.deployed_image_digest == "sha256:firstboot123"

    # Verify audit entry.
    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    deploy_entries = [e for e in entries if e.action == "deploy"]
    assert len(deploy_entries) >= 1
    assert deploy_entries[-1].component == "test-svc"


# ---------------------------------------------------------------------------
# Service deploy — already running (idempotent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_deploy_already_running(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Deploy on an already RUNNING component returns 200 without re-deploying."""
    _register_component("test-svc")
    record = await _seed_service_record("test-svc", state=ServiceState.RUNNING)
    record.deployed_image_digest = "sha256:existing"
    record.previous_image_digest = "sha256:prev-existing"
    record.health = "healthy"
    await server_mod.app.state.store.put(record)

    mock = MagicMock()
    mock.deploy = AsyncMock()
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/test-svc/deploy",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "test-svc"
    assert data["deployed_digest"] == "sha256:existing"
    assert data["previous_digest"] == "sha256:prev-existing"
    assert data["current_state"] == "running"
    assert data["health"] == "healthy"
    assert "already running" in data["detail"].lower()

    # Backend.deploy must NOT have been called.
    mock.deploy.assert_not_called()

    # Verify audit entry.
    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    deploy_entries = [e for e in entries if e.action == "deploy"]
    assert len(deploy_entries) >= 1
    assert "already running" in deploy_entries[-1].detail.lower()


# ---------------------------------------------------------------------------
# Service deploy — component not found (404)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_deploy_service_not_found(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Deploy returns 404 when no ServiceRecord exists."""
    _register_component("test-svc")
    # No seeded ServiceRecord — _get_or_create_record raises 404.

    resp = await client.post(
        "/chat/services/test-svc/deploy",
        headers=auth_headers,
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Service deploy — not allowlisted (403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_deploy_not_allowlisted(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Deploy returns 403 when component is not chat-agent-mutatable."""
    _register_component("test-svc", mutatable=False)
    await _seed_service_record("test-svc", state=ServiceState.STOPPED)

    resp = await client.post(
        "/chat/services/test-svc/deploy",
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Service deploy — backend failure (500)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_deploy_backend_failure(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Backend.deploy raising an exception results in 500."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.STOPPED)

    mock = MagicMock()
    mock.deploy = AsyncMock(side_effect=RuntimeError("pull failed"))
    server_mod.app.state.backend = mock

    resp = await client.post(
        "/chat/services/test-svc/deploy",
        headers=auth_headers,
    )
    assert resp.status_code == 500, resp.text
    assert "pull failed" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Service deploy — rate limited (429)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_deploy_rate_limited(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Second deploy within cooldown window returns 429."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.STOPPED)

    outcome = DeployOutcome(
        deployed_digest="sha256:abc",
        previous_digest="",
        state=ServiceState.RUNNING,
    )
    mock = MagicMock()
    mock.deploy = AsyncMock(return_value=outcome)
    server_mod.app.state.backend = mock

    resp1 = await client.post(
        "/chat/services/test-svc/deploy",
        headers=auth_headers,
    )
    assert resp1.status_code == 200

    # Re-seed STOPPED record (first deploy transitioned to RUNNING).
    await _seed_service_record("test-svc", state=ServiceState.STOPPED)

    resp2 = await client.post(
        "/chat/services/test-svc/deploy",
        headers=auth_headers,
    )
    assert resp2.status_code == 429, resp2.text
    assert "Rate limit" in resp2.json()["error"]


# ---------------------------------------------------------------------------
# Service deploy — lock contention (409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_deploy_lock_contention(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Deploy returns 409 when a deploy is already in progress."""
    from robotsix_central_deploy.lifecycle.deploy_lock import try_acquire_deploy_lock

    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.STOPPED)

    acquired = await try_acquire_deploy_lock("test-svc")
    assert acquired

    try:
        resp = await client.post(
            "/chat/services/test-svc/deploy",
            headers=auth_headers,
        )
        assert resp.status_code == 409, resp.text
        assert "already in progress" in resp.json()["error"]
    finally:
        from robotsix_central_deploy.lifecycle.deploy_lock import release_deploy_lock

        release_deploy_lock("test-svc")


# ---------------------------------------------------------------------------
# Service deploy — missing auth (no longer 401)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_deploy_no_longer_401(
    client: AsyncClient,
) -> None:
    """Component-level auth was removed; deploy without auth headers no longer returns 401."""
    resp = await client.post("/chat/services/test-svc/deploy")
    assert resp.status_code != 401, resp.text


# ---------------------------------------------------------------------------
# Service deploy — audit entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_deploy_audit_entry(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Successful deploy writes an audit entry with 'deploy' action."""
    _register_component("test-svc")
    await _seed_service_record("test-svc", state=ServiceState.STOPPED)

    outcome = DeployOutcome(
        deployed_digest="sha256:firstboot",
        previous_digest="",
        state=ServiceState.RUNNING,
    )
    mock = MagicMock()
    mock.deploy = AsyncMock(return_value=outcome)
    server_mod.app.state.backend = mock

    await client.post(
        "/chat/services/test-svc/deploy",
        headers=auth_headers,
    )

    audit_store: ChatAgentAuditStore = server_mod.app.state.chat_agent_audit_store
    entries = await audit_store.list()
    deploy_entries = [e for e in entries if e.action == "deploy"]
    assert len(deploy_entries) >= 1
    entry = deploy_entries[-1]
    assert entry.component == "test-svc"
    assert "sha256:firstboot" in entry.detail


# ---------------------------------------------------------------------------
# Authentication — missing auth (no longer 401)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_no_longer_401(
    client: AsyncClient,
) -> None:
    """Component-level auth was removed; update without auth headers no longer returns 401."""
    resp = await client.post("/chat/services/test-svc/update")
    assert resp.status_code != 401, resp.text


@pytest.mark.asyncio
async def test_deploy_no_longer_401(
    client: AsyncClient,
) -> None:
    """Component-level auth was removed; deploy without auth headers no longer returns 401."""
    resp = await client.post("/chat/deploy", json={"name": "x", "repo": "r"})
    assert resp.status_code != 401, resp.text


# ---------------------------------------------------------------------------
# Audit logging — verify entries per action type
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Self-targeted central-deploy guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_central_deploy_routes_to_self_update_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/services/central-deploy/update must route through the
    dedicated self-update endpoint (chat_self.py), NOT the generic
    chat_services.py handler.  The self-update path uses the detached
    updater so the management plane never tears itself down from inside.

    With the NoopBackend, self-inspect raises NotImplementedError, so
    the self-update endpoint returns 503 (unsupported) rather than
    proceeding with an unsafe in-process deploy.
    """
    _register_component("central-deploy")
    await _seed_service_record("central-deploy", state=ServiceState.RUNNING)

    resp = await client.post(
        "/chat/services/central-deploy/update",
        headers=auth_headers,
    )
    # 503 = self-update path correctly engaged but backend doesn't support it.
    # If the generic chat_services handler were reached instead we'd see 200
    # (or 409 from the guard), not 503.
    assert resp.status_code == 503, resp.text
    data = resp.json()
    assert "self-update" in data.get("error", "").lower()


@pytest.mark.asyncio
async def test_deploy_service_central_deploy_self_target_returns_409(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /chat/services/central-deploy/deploy must reject self-targeted
    deploys with 409, directing to POST /chat/services/central-deploy/update."""
    _register_component("central-deploy")
    await _seed_service_record("central-deploy", state=ServiceState.STOPPED)

    resp = await client.post(
        "/chat/services/central-deploy/deploy",
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    data = resp.json()
    assert "Cannot deploy central-deploy" in data["error"]
    assert "/chat/services/central-deploy/update" in data["error"]


# ---------------------------------------------------------------------------
# Chat agent — update (migrated from test_chat_agent.py)
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
# Chat agent — deploy (migrated from test_chat_agent.py)
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
            "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
            return_value=repo_files,
        ),
        patch(
            "robotsix_central_deploy.onboard.parser.parse_compose",
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
            "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
            return_value=repo_files,
        ),
        patch(
            "robotsix_central_deploy.onboard.parser.parse_compose",
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
            "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
            return_value=repo_files,
        ),
        patch(
            "robotsix_central_deploy.onboard.parser.parse_compose",
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
