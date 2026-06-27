"""Integration tests for the onboard-from-git API endpoints.

Uses ``httpx.AsyncClient`` against a FastAPI test transport with mocked
fetch/compose functions so no real git clone or Docker daemon is needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.backend import NoopBackend
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.onboard.models import DerivedSpec, ParseError
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import PortMapping, VolumeMount

# Import the server module itself so we can set its globals.
from robotsix_central_deploy.lifecycle import server as server_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_derived_spec(name: str = "test-svc", image: str = "ghcr.io/org/test-svc:main") -> DerivedSpec:
    return DerivedSpec(
        name=name,
        git_url="https://github.com/org/test-svc.git",
        image=image,
        ports=[PortMapping(host=8080, container=8080)],
        volume_mounts=[VolumeMount(host="test_data", container="/data")],
        stateful_volumes=["test_data"],
        env={"KEY": "val"},
        claude_mount=False,
    )


async def _seed_store_record(name: str = "svc-a") -> None:
    s = server_mod.app.state.store
    await s.put(ServiceRecord(name=name, state=ServiceState.STOPPED, image="repo:v1"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch):
    """Wire a fresh store/backend/config/registry/config_store before each test."""
    monkeypatch.setenv("ROBOTSIX_LIFECYCLE_API_KEY", "test-key")
    cfg = LifecycleConfig(  # type: ignore[call-arg]
        store_backend="memory",
        execution_backend="noop",
        api_key="test-key",
    )
    store = InMemoryStore()
    backend = NoopBackend()
    registry = ComponentRegistry([])
    config_store = ComponentConfigStore(Path("/tmp/test_component_configs.json"))  # noqa: S108

    # Remove any stale test file
    if config_store._path.exists():
        config_store._path.unlink()

    mock_checker = MagicMock()
    mock_checker.get_latest_digest = MagicMock(return_value=None)

    # Set both the module-level globals and app.state so all code paths work.
    server_mod._config = cfg
    server_mod._store = store
    server_mod._backend = backend
    server_mod._registry_checker = mock_checker
    server_mod.app.state.config = cfg
    server_mod.app.state.store = store
    server_mod.app.state.backend = backend
    server_mod.app.state.registry = registry
    server_mod.app.state.registry_checker = mock_checker
    server_mod.app.state.component_config_store = config_store


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


# ---------------------------------------------------------------------------
# POST /onboard/preflight
# ---------------------------------------------------------------------------


class TestOnboardPreflight:
    async def test_returns_spec_on_success(self, client: AsyncClient, auth_headers: dict):
        spec = _make_derived_spec("cool-app")

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_compose_bytes",
                return_value=b"fake compose bytes",
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={"git_url": "https://github.com/org/cool-app.git", "name": "cool-app"},
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "spec" in data
        assert data["spec"]["name"] == "cool-app"
        assert data["spec"]["image"] == "ghcr.io/org/test-svc:main"
        assert data["spec"]["git_url"] == "https://github.com/org/test-svc.git"

    async def test_non_https_url_returns_422(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/onboard/preflight",
            json={"git_url": "http://github.com/org/repo.git", "name": "my-app"},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        data = resp.json()
        assert "error" in data
        assert "https" in data["error"].lower()

    async def test_parse_error_returns_422_with_violations(
        self, client: AsyncClient, auth_headers: dict,
    ):
        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_compose_bytes",
                return_value=b"bad compose",
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                side_effect=ParseError(["violation 1", "violation 2"]),
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={"git_url": "https://github.com/org/repo.git", "name": "my-app"},
                headers=auth_headers,
            )

        assert resp.status_code == 422
        data = resp.json()
        assert "error" in data
        assert "violations" in data
        assert data["violations"] == ["violation 1", "violation 2"]

    async def test_name_already_in_store_returns_409(
        self, client: AsyncClient, auth_headers: dict,
    ):
        await _seed_store_record("my-app")

        resp = await client.post(
            "/onboard/preflight",
            json={"git_url": "https://github.com/org/repo.git", "name": "my-app"},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["error"]

    async def test_invalid_name_slug_returns_422(
        self, client: AsyncClient, auth_headers: dict,
    ):
        resp = await client.post(
            "/onboard/preflight",
            json={"git_url": "https://github.com/org/repo.git", "name": "Invalid_Name!"},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# POST /onboard/confirm
# ---------------------------------------------------------------------------


class TestOnboardConfirm:
    async def test_confirm_creates_component_and_returns_200(
        self, client: AsyncClient, auth_headers: dict,
    ):
        spec = _make_derived_spec("new-svc")
        store: InMemoryStore = server_mod.app.state.store

        resp = await client.post(
            "/onboard/confirm",
            json={"spec": spec.model_dump()},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "new-svc"
        assert data["image"] == "ghcr.io/org/test-svc:main"
        assert data["state"] == ServiceState.RUNNING.value

        # Verify config persisted in ComponentConfigStore
        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        all_configs = config_store.all()
        assert len(all_configs) == 1
        assert all_configs[0].id == "new-svc"
        assert all_configs[0].image == "ghcr.io/org/test-svc:main"

        # Verify ServiceRecord in store with RUNNING state
        record = await store.get("new-svc")
        assert record is not None
        assert record.state == ServiceState.RUNNING

    async def test_confirm_duplicate_name_returns_409(
        self, client: AsyncClient, auth_headers: dict,
    ):
        await _seed_store_record("existing-svc")
        spec = _make_derived_spec("existing-svc")

        resp = await client.post(
            "/onboard/confirm",
            json={"spec": spec.model_dump()},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["error"]

    async def test_confirm_deploy_failure_rolls_back_and_returns_500(
        self, client: AsyncClient, auth_headers: dict,
    ):
        spec = _make_derived_spec("fail-svc")
        store: InMemoryStore = server_mod.app.state.store

        with patch.object(
            server_mod.app.state.backend,
            "deploy",
            side_effect=RuntimeError("simulated deploy failure"),
        ):
            resp = await client.post(
                "/onboard/confirm",
                json={"spec": spec.model_dump()},
                headers=auth_headers,
            )

        assert resp.status_code == 500
        assert "simulated deploy failure" in resp.json()["error"]

        # Config should be removed from store
        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        assert len(config_store.all()) == 0

        # ServiceRecord should be removed
        record = await store.get("fail-svc")
        assert record is None

        # Registry should not contain the entry
        registry: ComponentRegistry = server_mod.app.state.registry
        assert registry.get("fail-svc") is None

    async def test_confirm_uses_container_name_override(
        self, client: AsyncClient, auth_headers: dict,
    ):
        """The created ServiceRecord should use container_name from DerivedSpec when set."""
        spec = _make_derived_spec("broker")
        spec.container_name = "agent-comm"

        resp = await client.post(
            "/onboard/confirm",
            json={"spec": spec.model_dump()},
            headers=auth_headers,
        )

        assert resp.status_code == 200

        # Verify ServiceRecord uses container_name override
        store: InMemoryStore = server_mod.app.state.store
        record = await store.get("broker")
        assert record is not None
        assert record.container_name == "agent-comm"

        # Verify ComponentConfig also uses the override
        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        all_configs = config_store.all()
        assert len(all_configs) == 1
        assert all_configs[0].id == "broker"
        assert all_configs[0].container_name == "agent-comm"
