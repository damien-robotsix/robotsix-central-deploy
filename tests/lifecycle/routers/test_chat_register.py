"""Tests for the chat-agent service-registration endpoint (chat_register.py).

Mirrors the source-side module split: chat_register.py owns ``POST
/chat/services``.  These cases were migrated out of the flat
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
from robotsix_central_deploy.lifecycle.models import ExecutionBackendType
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.onboard.fetcher import RepoFiles
from robotsix_central_deploy.onboard.models import DerivedSpec
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
# Register helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
def _mock_parse_compose_for_register(
    compose_bytes: bytes, name: str, git_url: str
) -> DerivedSpec:
    """Return a minimal DerivedSpec suitable for the register endpoint tests.

    Fully constructed rather than ``model_construct``-ed: the register
    endpoint builds the stored ComponentConfig out of this spec, so a
    partially-populated stub would raise on the first defaulted field it
    reads instead of exercising the handler.
    """
    return DerivedSpec(
        name=name,
        git_url=git_url,
        image="ghcr.io/damien-robotsix/hexarchy:main",
        ports=[],
        volume_mounts=[],
        env={},
        claude_mount=False,
        host_docker_sock=False,
        siblings=[],
        config_volume="test-config-vol",
    )


_COMPOSE_WITH_HEADER = (
    b"# central-deploy-contract-version: 1\n"
    b"services:\n"
    b"  app:\n"
    b"    image: ghcr.io/damien-robotsix/hexarchy:main\n"
)


# ---------------------------------------------------------------------------
# Register — POST /chat/services
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_register_happy_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
    component_config_store: ComponentConfigStore,
    monkeypatch,
):
    """POST /chat/services registers a new component and it appears in GET /chat/components."""
    from robotsix_central_deploy.onboard import fetcher as fetcher_mod
    from robotsix_central_deploy.onboard import parser as parser_mod

    monkeypatch.setattr(
        fetcher_mod,
        "fetch_repo_files",
        lambda git_url, timeout_sec=30, github_token=None: RepoFiles(
            compose_bytes=_COMPOSE_WITH_HEADER,
            config_schema_json=b'{"type": "object"}',
            config_json=None,
            config_json_template=None,
        ),
    )
    monkeypatch.setattr(parser_mod, "parse_compose", _mock_parse_compose_for_register)

    # Enable registration in the server config.
    from robotsix_central_deploy.lifecycle import app as server_mod

    server_mod.app.state.config.chat_agent_registration_enabled = True

    resp = await client.post(
        "/chat/services",
        json={
            "name": "hexarchy",
            "image": "ghcr.io/damien-robotsix/hexarchy:main",
            "owner_repo": "https://github.com/damien-robotsix/hexarchy",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "hexarchy"
    assert data["action"] == "register"
    assert data["image"] == "ghcr.io/damien-robotsix/hexarchy:main"
    assert data["owner_repo"] == "https://github.com/damien-robotsix/hexarchy"
    assert data["existed"] is False

    # Verify it appears in the component roster.
    roster_resp = await client.get("/chat/components", headers=auth_headers)
    assert roster_resp.status_code == 200
    # The new component won't show in the roster unless allow_chat_access is set,
    # but it should be in the component_config_store.
    stored = component_config_store.get("hexarchy")
    assert stored is not None
    assert stored.id == "hexarchy"
    assert stored.image == "ghcr.io/damien-robotsix/hexarchy:main"


@pytest.mark.asyncio
async def test_chat_register_idempotent(
    client: AsyncClient,
    auth_headers: dict[str, str],
    component_config_store: ComponentConfigStore,
    monkeypatch,
):
    """Re-registering the same component id returns the existing entry."""
    from robotsix_central_deploy.onboard import fetcher as fetcher_mod
    from robotsix_central_deploy.onboard import parser as parser_mod

    monkeypatch.setattr(
        fetcher_mod,
        "fetch_repo_files",
        lambda git_url, timeout_sec=30, github_token=None: RepoFiles(
            compose_bytes=_COMPOSE_WITH_HEADER,
            config_schema_json=b'{"type": "object"}',
            config_json=None,
            config_json_template=None,
        ),
    )
    monkeypatch.setattr(parser_mod, "parse_compose", _mock_parse_compose_for_register)

    from robotsix_central_deploy.lifecycle import app as server_mod

    server_mod.app.state.config.chat_agent_registration_enabled = True

    # First registration.
    resp1 = await client.post(
        "/chat/services",
        json={
            "name": "hexarchy",
            "image": "ghcr.io/damien-robotsix/hexarchy:main",
            "owner_repo": "https://github.com/damien-robotsix/hexarchy",
        },
        headers=auth_headers,
    )
    assert resp1.status_code == 200
    assert resp1.json()["existed"] is False

    # Second registration with a different image — should return existing.
    resp2 = await client.post(
        "/chat/services",
        json={
            "name": "hexarchy",
            "image": "ghcr.io/damien-robotsix/hexarchy:other-tag",
            "owner_repo": "https://github.com/other/hexarchy",
        },
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["existed"] is True
    # The stored entry is unchanged.
    assert data2["image"] == "ghcr.io/damien-robotsix/hexarchy:main"
    assert data2["owner_repo"] == "https://github.com/damien-robotsix/hexarchy"


@pytest.mark.asyncio
async def test_chat_register_not_enabled_returns_403(
    client: AsyncClient,
    auth_headers: dict[str, str],
):
    """Registration returns 403 when the toggle is off."""
    from robotsix_central_deploy.lifecycle import app as server_mod

    server_mod.app.state.config.chat_agent_registration_enabled = False

    resp = await client.post(
        "/chat/services",
        json={
            "name": "hexarchy",
            "image": "ghcr.io/damien-robotsix/hexarchy:main",
            "owner_repo": "https://github.com/damien-robotsix/hexarchy",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 403
    assert "registration is not enabled" in resp.json()["error"]


@pytest.mark.asyncio
async def test_chat_register_invalid_name_returns_422(
    client: AsyncClient,
    auth_headers: dict[str, str],
):
    """Registration with an invalid component name returns 422."""
    from robotsix_central_deploy.lifecycle import app as server_mod

    server_mod.app.state.config.chat_agent_registration_enabled = True

    resp = await client.post(
        "/chat/services",
        json={
            "name": "INVALID_NAME",
            "image": "ghcr.io/damien-robotsix/hexarchy:main",
            "owner_repo": "https://github.com/damien-robotsix/hexarchy",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_register_appears_in_service_list(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch,
):
    """Registered component appears in the GET /services inventory."""
    from robotsix_central_deploy.onboard import fetcher as fetcher_mod
    from robotsix_central_deploy.onboard import parser as parser_mod

    monkeypatch.setattr(
        fetcher_mod,
        "fetch_repo_files",
        lambda git_url, timeout_sec=30, github_token=None: RepoFiles(
            compose_bytes=_COMPOSE_WITH_HEADER,
            config_schema_json=b'{"type": "object"}',
            config_json=None,
            config_json_template=None,
        ),
    )
    monkeypatch.setattr(parser_mod, "parse_compose", _mock_parse_compose_for_register)

    from robotsix_central_deploy.lifecycle import app as server_mod

    server_mod.app.state.config.chat_agent_registration_enabled = True

    await client.post(
        "/chat/services",
        json={
            "name": "hexarchy",
            "image": "ghcr.io/damien-robotsix/hexarchy:main",
            "owner_repo": "https://github.com/damien-robotsix/hexarchy",
        },
        headers=auth_headers,
    )

    # The component should appear in the service list.
    resp = await client.get("/services", headers=auth_headers)
    assert resp.status_code == 200
    services = resp.json()
    names = [s["name"] for s in services["services"]]
    assert "hexarchy" in names


@pytest.mark.asyncio
async def test_chat_register_audit_entry(
    client: AsyncClient,
    auth_headers: dict[str, str],
    audit_store: ChatAgentAuditStore,
    monkeypatch,
):
    """Registration writes an audit entry."""
    from robotsix_central_deploy.onboard import fetcher as fetcher_mod
    from robotsix_central_deploy.onboard import parser as parser_mod

    monkeypatch.setattr(
        fetcher_mod,
        "fetch_repo_files",
        lambda git_url, timeout_sec=30, github_token=None: RepoFiles(
            compose_bytes=_COMPOSE_WITH_HEADER,
            config_schema_json=b'{"type": "object"}',
            config_json=None,
            config_json_template=None,
        ),
    )
    monkeypatch.setattr(parser_mod, "parse_compose", _mock_parse_compose_for_register)

    from robotsix_central_deploy.lifecycle import app as server_mod

    server_mod.app.state.config.chat_agent_registration_enabled = True

    await client.post(
        "/chat/services",
        json={
            "name": "hexarchy",
            "image": "ghcr.io/damien-robotsix/hexarchy:main",
            "owner_repo": "https://github.com/damien-robotsix/hexarchy",
        },
        headers=auth_headers,
    )

    entries = await audit_store.list(limit=5, component="hexarchy")
    assert len(entries) >= 1
    entry = entries[0]
    assert entry.action == "register"
    assert entry.component == "hexarchy"
    assert "ghcr.io/damien-robotsix/hexarchy:main" in entry.detail
    assert "https://github.com/damien-robotsix/hexarchy" in entry.detail


@pytest.mark.asyncio
async def test_chat_register_applies_default_image_tag_when_missing(
    client: AsyncClient,
    auth_headers: dict[str, str],
    component_config_store: ComponentConfigStore,
    monkeypatch,
):
    """When image has no tag, ':latest' is appended."""
    from robotsix_central_deploy.onboard import fetcher as fetcher_mod
    from robotsix_central_deploy.onboard import parser as parser_mod

    monkeypatch.setattr(
        fetcher_mod,
        "fetch_repo_files",
        lambda git_url, timeout_sec=30, github_token=None: RepoFiles(
            compose_bytes=_COMPOSE_WITH_HEADER,
            config_schema_json=b'{"type": "object"}',
            config_json=None,
            config_json_template=None,
        ),
    )
    monkeypatch.setattr(parser_mod, "parse_compose", _mock_parse_compose_for_register)

    from robotsix_central_deploy.lifecycle import app as server_mod

    server_mod.app.state.config.chat_agent_registration_enabled = True

    resp = await client.post(
        "/chat/services",
        json={
            "name": "no-tag-svc",
            "image": "ghcr.io/damien-robotsix/no-tag-svc",
            "owner_repo": "https://github.com/damien-robotsix/no-tag-svc",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["image"] == "ghcr.io/damien-robotsix/no-tag-svc:latest"

    stored = component_config_store.get("no-tag-svc")
    assert stored is not None
    assert stored.image == "ghcr.io/damien-robotsix/no-tag-svc:latest"


@pytest.mark.asyncio
async def test_chat_register_rejects_missing_contract_header(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch,
):
    """Agent register of a repo without the contract-version header is refused (422)."""
    from robotsix_central_deploy.onboard import fetcher as fetcher_mod

    # Compose bytes WITHOUT the required contract-version header.
    compose_no_header = (
        b"services:\n  app:\n    image: ghcr.io/damien-robotsix/hexarchy:main\n"
    )

    monkeypatch.setattr(
        fetcher_mod,
        "fetch_repo_files",
        lambda git_url, timeout_sec=30, github_token=None: RepoFiles(
            compose_bytes=compose_no_header,
            config_schema_json=b'{"type": "object"}',
            config_json=None,
            config_json_template=None,
        ),
    )
    # Do NOT mock parse_compose — the real function must reject the
    # missing header so we have parity with the manual /onboard/preflight path.

    from robotsix_central_deploy.lifecycle import app as server_mod

    server_mod.app.state.config.chat_agent_registration_enabled = True

    resp = await client.post(
        "/chat/services",
        json={
            "name": "hexarchy",
            "image": "ghcr.io/damien-robotsix/hexarchy:main",
            "owner_repo": "https://github.com/damien-robotsix/hexarchy",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "compose validation failed" in body["error"]
    violations = body["violations"]
    assert any("central-deploy-contract-version" in v for v in violations), (
        f"Expected contract-version violation in: {violations}"
    )


# ---------------------------------------------------------------------------
# Register — contract-derived config (ports/volumes/schema)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_register_stores_contract_ports_and_volumes(
    client: AsyncClient,
    auth_headers: dict[str, str],
    component_config_store: ComponentConfigStore,
    config_yaml_store: ConfigYamlStore,
    monkeypatch,
):
    """Registration builds the stored config from the contract, not from the request body.

    Regression: the endpoint validated the contract and then persisted only
    ``id``/``image``/``git_url``. A component registered that way deployed with
    no port — so ``traefik_labels`` emitted nothing and its public URL 404'd
    while the container reported healthy — and with no volume mounts, so its
    data sat in the container's writable layer until the next redeploy erased
    it.
    """
    from robotsix_central_deploy.onboard import fetcher as fetcher_mod
    from robotsix_central_deploy.onboard import parser as parser_mod
    from robotsix_central_deploy.onboard.models import PortMapping, VolumeMount
    from robotsix_central_deploy.registry.traefik_labels import traefik_labels

    def _spec_with_contract(
        compose_bytes: bytes, name: str, git_url: str
    ) -> DerivedSpec:
        return DerivedSpec(
            name=name,
            git_url=git_url,
            image="ghcr.io/damien-robotsix/hexarchy:main",
            ports=[PortMapping(host=8300, container=8080, protocol="tcp")],
            volume_mounts=[
                VolumeMount(
                    host="config-data", container="/home/app/config", read_only=False
                ),
                VolumeMount(
                    host="db-data", container="/home/app/data", read_only=False
                ),
            ],
            env={},
            claude_mount=False,
            host_docker_sock=False,
            siblings=[],
            config_volume="config-data",
        )

    monkeypatch.setattr(
        fetcher_mod,
        "fetch_repo_files",
        lambda git_url, timeout_sec=30, github_token=None: RepoFiles(
            compose_bytes=_COMPOSE_WITH_HEADER,
            config_schema_json=b'{"type": "object"}',
            config_json=None,
            config_json_template=None,
        ),
    )
    monkeypatch.setattr(parser_mod, "parse_compose", _spec_with_contract)

    from robotsix_central_deploy.lifecycle import app as server_mod

    server_mod.app.state.config.chat_agent_registration_enabled = True

    resp = await client.post(
        "/chat/services",
        json={
            "name": "hexarchy",
            "image": "ghcr.io/damien-robotsix/hexarchy:main",
            "owner_repo": "https://github.com/damien-robotsix/hexarchy",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    stored = component_config_store.get("hexarchy")
    assert stored is not None
    assert [(p.host, p.container) for p in stored.ports] == [(8300, 8080)]
    # Volumes are namespaced by component so two repos cannot claim one name.
    assert [m.container for m in stored.mounts] == [
        "/home/app/config",
        "/home/app/data",
    ]
    assert stored.named_volumes == ["hexarchy-config-data", "hexarchy-db-data"]
    assert stored.config_volume == "hexarchy-config-data"

    # The point of carrying the port: the edge can now route the component.
    labels = traefik_labels(stored, "deploy.robotsix.net", "central-deploy-proxy")
    assert labels["traefik.enable"] == "true"
    assert (
        labels["traefik.http.routers.hexarchy.rule"]
        == "Host(`hexarchy.deploy.robotsix.net`)"
    )
    assert labels["traefik.http.services.hexarchy.loadbalancer.server.port"] == "8080"

    # The schema is stored too, so the first deploy has a config document to
    # seed rather than leaving the component on its in-image defaults.
    assert await config_yaml_store.get_template("hexarchy") == {"type": "object"}
