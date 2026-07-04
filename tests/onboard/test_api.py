"""Integration tests for the onboard-from-git API endpoints.

Uses ``httpx.AsyncClient`` against a FastAPI test transport with mocked
fetch/compose functions so no real git clone or Docker daemon is needed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.backends import NoopBackend
from robotsix_central_deploy.lifecycle.deps import JobRegistry
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.models import (
    ExecutionBackendType,
    ServiceRecord,
    ServiceState,
)
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.onboard.models import (
    DerivedSpec,
    ParseError,
    SiblingDerivedSpec,
)
from robotsix_central_deploy.onboard.fetcher import RepoFiles
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.config_yaml_store import ConfigYamlStore
from robotsix_central_deploy.registry.env_store import EnvStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.models import PortMapping, VolumeMount
from robotsix_central_deploy.registry.secret_key import SecretKeyManager
from robotsix_central_deploy.registry.settings_store import (
    SystemSettings,
    SystemSettingsStore,
)

# Import the server module itself so we can set its globals.
from robotsix_central_deploy.lifecycle import server as server_mod


SAMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "host": {"type": "string"},
        "port": {"type": "integer", "default": 5432},
        "api_key": {"type": "string", "format": "password", "writeOnly": True},
    },
    "required": ["host"],
}
"""Minimal valid JSON Schema used across onboard tests."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_derived_spec(
    name: str = "test-svc", image: str = "ghcr.io/org/test-svc:main"
) -> DerivedSpec:
    return DerivedSpec(
        name=name,
        git_url="https://github.com/org/test-svc.git",
        image=image,
        ports=[PortMapping(host=8080, container=8080)],
        volume_mounts=[VolumeMount(host="test_data", container="/data")],
        env={"KEY": "val"},
        claude_mount=False,
        host_docker_sock=False,
    )


async def _seed_store_record(name: str = "svc-a") -> None:
    s = server_mod.app.state.store
    await s.put(ServiceRecord(name=name, state=ServiceState.STOPPED, image="repo:v1"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch, tmp_path):
    """Wire a fresh store/backend/config/registry/config_store before each test.

    All file-backed stores use the per-test ``tmp_path`` so tests stay
    isolated under parallel (xdist) execution — a shared fixed path under
    /tmp leaks state across workers.
    """
    monkeypatch.setenv("ROBOTSIX_LIFECYCLE_API_KEY", "test-key")
    cfg = LifecycleConfig(  # type: ignore[call-arg]
        store_backend="memory",
        execution_backend=ExecutionBackendType.NOOP,
        api_key="test-key",
    )
    store = InMemoryStore()
    backend = NoopBackend()
    registry = ComponentRegistry([])
    config_store = ComponentConfigStore(tmp_path / "component_configs.json")

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
    server_mod.app.state.config_yaml_store = ConfigYamlStore(
        tmp_path / "config_json.json"
    )
    server_mod.app.state.env_store = EnvStore(
        tmp_path / "env_store.json", SecretKeyManager(tmp_path / "secret_key")
    )
    server_mod.app.state.job_registry = JobRegistry()

    # Settings store — needed by onboard_confirm for caretaker checks
    settings_path = tmp_path / "settings.json"
    server_mod.app.state.settings_store = SystemSettingsStore(settings_path)
    # Also set http_client on state for background job mill registration
    server_mod.app.state.http_client = MagicMock(spec=AsyncClient)


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
    async def test_returns_spec_on_success(
        self, client: AsyncClient, auth_headers: dict
    ):
        spec = _make_derived_spec("cool-app")

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=RepoFiles(
                    compose_bytes=b"fake compose bytes", config_json=None
                ),
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={
                    "git_url": "https://github.com/org/cool-app.git",
                    "name": "cool-app",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "spec" in data
        assert data["spec"]["name"] == "cool-app"
        assert data["spec"]["image"] == "ghcr.io/org/test-svc:main"
        assert data["spec"]["git_url"] == "https://github.com/org/test-svc.git"

    async def test_non_https_url_returns_422(
        self, client: AsyncClient, auth_headers: dict
    ):
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
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=RepoFiles(compose_bytes=b"bad compose", config_json=None),
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
        self,
        client: AsyncClient,
        auth_headers: dict,
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
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        resp = await client.post(
            "/onboard/preflight",
            json={
                "git_url": "https://github.com/org/repo.git",
                "name": "Invalid_Name!",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert "error" in resp.json()

    async def test_volume_collision_returns_409(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """Preflight detects that namespaced volumes would collide with an existing component."""
        # Seed an existing component with namespaced volumes.
        from robotsix_central_deploy.registry.models import (
            ComponentConfig,
        )

        existing = ComponentConfig(
            id="mail",
            image="ghcr.io/org/mail:main",
            container_name="mail",
            ports=[],
            mounts=[],
            env={},
            named_volumes=[
                "mail-auto-mail-config",
                "mail-auto-mail-data",
                "mail-auto-mail-logs",
            ],
        )
        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        await config_store.put(existing)

        # Build a DerivedSpec that (after namespacing) would produce the same volumes.
        spec = DerivedSpec(
            name="mail",  # same name as existing → "mail-auto-mail-config" etc.
            git_url="https://github.com/org/auto-mail.git",
            image="ghcr.io/org/auto-mail:main",
            ports=[],
            volume_mounts=[
                VolumeMount(host="auto-mail-config", container="/config"),
                VolumeMount(host="auto-mail-data", container="/data"),
                VolumeMount(host="auto-mail-logs", container="/logs"),
            ],
            env={},
            claude_mount=False,
            host_docker_sock=False,
        )

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=RepoFiles(
                    compose_bytes=b"fake compose bytes", config_json=None
                ),
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={
                    "git_url": "https://github.com/org/auto-mail.git",
                    "name": "mail",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 409
        data = resp.json()
        assert "error" in data
        assert "collisions" in data
        assert any("mail-auto-mail-config" in c for c in data["collisions"])
        assert any("mail-auto-mail-data" in c for c in data["collisions"])
        assert any("mail-auto-mail-logs" in c for c in data["collisions"])


# ---------------------------------------------------------------------------
# POST /onboard/confirm — helpers
# ---------------------------------------------------------------------------


async def _poll_job_until_done(
    client: AsyncClient, job_id: str, headers: dict, timeout: float = 5.0
) -> dict:
    """Poll GET /onboard/jobs/{job_id} until phase is 'done' or 'failed'.

    Returns the final job status dict. Raises AssertionError on timeout.
    """
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        resp = await client.get(f"/onboard/jobs/{job_id}", headers=headers)
        assert resp.status_code == 200, f"job poll returned {resp.status_code}"
        data = resp.json()
        if data["phase"] in ("done", "failed"):
            return data
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"job {job_id} did not reach terminal phase within {timeout}s"
            )
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# POST /onboard/confirm
# ---------------------------------------------------------------------------


class TestOnboardConfirm:
    async def test_confirm_creates_component_and_returns_202(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        spec = _make_derived_spec("new-svc")
        store: InMemoryStore = server_mod.app.state.store

        resp = await client.post(
            "/onboard/confirm",
            json={"spec": spec.model_dump()},
            headers=auth_headers,
        )

        assert resp.status_code == 202
        data = resp.json()
        assert data["name"] == "new-svc"
        assert "job_id" in data
        job_id = data["job_id"]

        # Poll until done
        job_status = await _poll_job_until_done(client, job_id, auth_headers)
        assert job_status["phase"] == "done"
        assert job_status["name"] == "new-svc"
        assert job_status["image"] == "ghcr.io/org/test-svc:main"
        assert job_status["state"] == ServiceState.RUNNING.value

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
        self,
        client: AsyncClient,
        auth_headers: dict,
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

    async def test_confirm_deploy_failure_rolls_back_and_returns_202(
        self,
        client: AsyncClient,
        auth_headers: dict,
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

            # Returns 202 immediately (deploy runs in background)
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            # Poll until failed (still inside the with-block so patch is active)
            job_status = await _poll_job_until_done(client, job_id, auth_headers)
            assert job_status["phase"] == "failed"
            assert "simulated deploy failure" in job_status["error"]

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
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """The created ServiceRecord should use container_name from DerivedSpec when set."""
        spec = _make_derived_spec("broker")
        spec.container_name = "agent-comm"

        resp = await client.post(
            "/onboard/confirm",
            json={"spec": spec.model_dump()},
            headers=auth_headers,
        )

        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Poll until done
        job_status = await _poll_job_until_done(client, job_id, auth_headers)
        assert job_status["phase"] == "done"

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

    # -- new rollback + env tests ------------------------------------------------

    async def test_deploy_failure_removes_container_and_clears_env(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """After a primary deploy failure, the container is removed and the
        seeded EnvStore entry is deleted."""
        spec = _make_derived_spec("fail-svc")
        store: InMemoryStore = server_mod.app.state.store
        env_store: EnvStore = server_mod.app.state.env_store

        class _SpyBackend(NoopBackend):
            def __init__(self):
                super().__init__()
                self.removed: list[str] = []

            async def remove_container(self, service: ServiceRecord) -> None:
                self.removed.append(service.name)

        spy = _SpyBackend()
        server_mod.app.state.backend = spy
        server_mod._backend = spy

        with patch.object(spy, "deploy", side_effect=RuntimeError("unhealthy")):
            resp = await client.post(
                "/onboard/confirm",
                json={"spec": spec.model_dump()},
                headers=auth_headers,
            )

            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            # Poll until failed (still inside the with-block so patch is active)
            job_status = await _poll_job_until_done(client, job_id, auth_headers)
            assert job_status["phase"] == "failed"
            assert "unhealthy" in job_status["error"]

        # Container removed
        assert spy.removed == [spec.name]

        # ServiceRecord removed
        record = await store.get("fail-svc")
        assert record is None

        # EnvStore entry deleted
        env_config = await env_store.get("fail-svc")
        assert env_config.env == {}
        assert env_config.secret_tokens == {}

    async def test_sibling_deploy_failure_removes_containers_and_clears_env(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """When a sibling deploy fails, both primary and sibling containers
        are removed and the seeded env entry is deleted."""
        spec = _make_multi_service_derived_spec("fail-multi")
        store: InMemoryStore = server_mod.app.state.store
        env_store: EnvStore = server_mod.app.state.env_store

        class _SpyBackend(NoopBackend):
            def __init__(self):
                super().__init__()
                self.removed: list[str] = []

            async def remove_container(self, service: ServiceRecord) -> None:
                self.removed.append(service.name)

        spy = _SpyBackend()
        server_mod.app.state.backend = spy
        server_mod._backend = spy

        call_count = [0]

        async def failing_deploy(service, config, image_ref):
            call_count[0] += 1
            if call_count[0] == 1:
                return await NoopBackend.deploy(spy, service, config, image_ref)
            raise RuntimeError("simulated sibling deploy failure")

        with patch.object(spy, "deploy", side_effect=failing_deploy):
            resp = await client.post(
                "/onboard/confirm",
                json={"spec": spec.model_dump()},
                headers=auth_headers,
            )

            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            # Poll until failed (still inside the with-block so patch is active)
            job_status = await _poll_job_until_done(client, job_id, auth_headers)
            assert job_status["phase"] == "failed"
            assert "simulated sibling deploy failure" in job_status["error"]

        # Both containers removed (primary first, then sibling)
        assert spy.removed == ["fail-multi", "fail-multi-worker"]

        # Both records removed
        assert await store.get("fail-multi") is None
        assert await store.get("fail-multi-worker") is None

        # EnvStore entry deleted
        env_config = await env_store.get("fail-multi")
        assert env_config.env == {}
        assert env_config.secret_tokens == {}

    async def test_deploy_failure_preserves_preexisting_env(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """A pre-existing EnvStore entry must survive rollback — only a
        freshly-seeded entry is deleted."""
        spec = _make_derived_spec("fail-svc")
        env_store: EnvStore = server_mod.app.state.env_store

        # Pre-seed the env entry
        await env_store.upsert(spec.name, {"MY_KEY": "existing"}, {})

        class _SpyBackend(NoopBackend):
            def __init__(self):
                super().__init__()
                self.removed: list[str] = []

            async def remove_container(self, service: ServiceRecord) -> None:
                self.removed.append(service.name)

        spy = _SpyBackend()
        server_mod.app.state.backend = spy
        server_mod._backend = spy

        with patch.object(spy, "deploy", side_effect=RuntimeError("unhealthy")):
            resp = await client.post(
                "/onboard/confirm",
                json={"spec": spec.model_dump()},
                headers=auth_headers,
            )

            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            # Poll until failed (still inside the with-block so patch is active)
            job_status = await _poll_job_until_done(client, job_id, auth_headers)
            assert job_status["phase"] == "failed"

        # Preexisting env entry preserved
        env_config = await env_store.get(spec.name)
        assert env_config.env == {"MY_KEY": "existing"}
        assert env_config.secret_tokens == {}

    async def test_deploy_failure_removes_named_volumes(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """After a deploy failure, rollback tears down the component's namespaced
        named volumes so a retry does not hit the volume-collision preflight."""
        spec = _make_derived_spec("fail-svc")

        class _SpyBackend(NoopBackend):
            def __init__(self):
                super().__init__()
                self.removed_volumes: list[str] = []

            async def remove_volume(self, volume_name: str) -> None:
                self.removed_volumes.append(volume_name)

        spy = _SpyBackend()
        server_mod.app.state.backend = spy
        server_mod._backend = spy

        with patch.object(spy, "deploy", side_effect=RuntimeError("unhealthy")):
            resp = await client.post(
                "/onboard/confirm",
                json={"spec": spec.model_dump()},
                headers=auth_headers,
            )
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]
            job_status = await _poll_job_until_done(client, job_id, auth_headers)
            assert job_status["phase"] == "failed"

        # The volume host "test_data" is namespaced as "<name>-test_data" on confirm.
        assert spy.removed_volumes == ["fail-svc-test_data"]

    async def test_deploy_failure_tolerates_remove_volume_raising(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """Rollback must not crash when remove_volume raises (e.g.
        NotImplementedError on DockerBackend or an already-missing volume) —
        the job still finishes as 'failed'."""
        spec = _make_derived_spec("fail-svc")

        class _SpyBackend(NoopBackend):
            def __init__(self):
                super().__init__()
                self.attempted_volumes: list[str] = []

            async def remove_volume(self, volume_name: str) -> None:
                self.attempted_volumes.append(volume_name)
                raise NotImplementedError("remove_volume not supported")

        spy = _SpyBackend()
        server_mod.app.state.backend = spy
        server_mod._backend = spy

        with patch.object(spy, "deploy", side_effect=RuntimeError("unhealthy")):
            resp = await client.post(
                "/onboard/confirm",
                json={"spec": spec.model_dump()},
                headers=auth_headers,
            )
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]
            job_status = await _poll_job_until_done(client, job_id, auth_headers)
            assert job_status["phase"] == "failed"

        # remove_volume was attempted despite raising; rollback swallowed the error.
        assert spy.attempted_volumes == ["fail-svc-test_data"]

    async def test_confirm_double_confirm_while_job_active_returns_409(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """A second confirm for the same component while a job is active returns 409."""
        spec = _make_derived_spec("blocked-svc")

        # Inject a deploy that hangs so the job stays active
        import asyncio

        async def slow_deploy(service, config, image_ref):
            await asyncio.sleep(0.5)
            return await NoopBackend.deploy(
                server_mod.app.state.backend, service, config, image_ref
            )

        with patch.object(
            server_mod.app.state.backend, "deploy", side_effect=slow_deploy
        ):
            # First confirm returns 202
            resp1 = await client.post(
                "/onboard/confirm",
                json={"spec": spec.model_dump()},
                headers=auth_headers,
            )
            assert resp1.status_code == 202

            # Second confirm while job is still active returns 409
            resp2 = await client.post(
                "/onboard/confirm",
                json={"spec": spec.model_dump()},
                headers=auth_headers,
            )
            assert resp2.status_code == 409
            assert "already in progress" in resp2.json()["error"]


# ---------------------------------------------------------------------------
# GET /onboard/jobs/{job_id}
# ---------------------------------------------------------------------------


class TestOnboardJobStatus:
    async def test_job_status_happy_path(self, client: AsyncClient, auth_headers: dict):
        """Poll the job status endpoint and see progress from writing_config to done."""
        spec = _make_derived_spec("job-svc")

        resp = await client.post(
            "/onboard/confirm",
            json={"spec": spec.model_dump()},
            headers=auth_headers,
        )

        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Immediate poll should show the job exists (phase may be done already
        # since NoopBackend is fast, but at least it shouldn't be unknown)
        status_resp = await client.get(f"/onboard/jobs/{job_id}", headers=auth_headers)
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["job_id"] == job_id
        assert data["component"] == "job-svc"
        assert data["phase"] in (
            "writing_config",
            "deploying_primary",
            "waiting_health",
            "deploying_siblings",
            "done",
        )

        # Poll until done
        job_status = await _poll_job_until_done(client, job_id, auth_headers)
        assert job_status["phase"] == "done"
        assert job_status["name"] == "job-svc"
        assert job_status["image"] == "ghcr.io/org/test-svc:main"
        assert job_status["state"] == ServiceState.RUNNING.value
        assert job_status["error"] is None

    async def test_job_status_unknown_job_returns_404(
        self, client: AsyncClient, auth_headers: dict
    ):
        """GET /onboard/jobs/bogus returns 404."""
        resp = await client.get("/onboard/jobs/nonexistent-1", headers=auth_headers)
        assert resp.status_code == 404
        assert "unknown job" in resp.json()["error"]

    async def test_job_status_failure_path(
        self, client: AsyncClient, auth_headers: dict
    ):
        """On deploy failure, the job reaches phase 'failed' with error detail."""
        spec = _make_derived_spec("fail-job")

        with patch.object(
            server_mod.app.state.backend,
            "deploy",
            side_effect=RuntimeError("boom"),
        ):
            resp = await client.post(
                "/onboard/confirm",
                json={"spec": spec.model_dump()},
                headers=auth_headers,
            )

            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            job_status = await _poll_job_until_done(client, job_id, auth_headers)
            assert job_status["phase"] == "failed"
            assert job_status["error"] == "boom"
            assert job_status["name"] is None
            assert job_status["image"] is None
            assert job_status["state"] is None


# ---------------------------------------------------------------------------
# Multi-service helpers
# ---------------------------------------------------------------------------


def _make_multi_service_derived_spec(name: str = "multi-svc") -> DerivedSpec:
    """Return a DerivedSpec with one sibling (worker)."""
    return DerivedSpec(
        name=name,
        git_url="https://github.com/org/multi-svc.git",
        image="ghcr.io/org/multi-svc:main",
        ports=[PortMapping(host=8080, container=8080)],
        volume_mounts=[],
        env={"PRIMARY_KEY": "val"},
        claude_mount=False,
        host_docker_sock=False,
        siblings=[
            SiblingDerivedSpec(
                service_key="worker",
                container_name=f"{name}-worker",
                image="ghcr.io/org/multi-svc-worker:v1",
                ports=[],
                volume_mounts=[],
                env={"WORKER_KEY": "worker-val"},
            )
        ],
    )


# ---------------------------------------------------------------------------
# Multi-service confirm tests
# ---------------------------------------------------------------------------


class TestMultiServiceOnboardConfirm:
    async def test_confirm_multi_service_creates_sibling_record(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """POST /onboard/confirm with multi-service spec creates sibling record."""
        spec = _make_multi_service_derived_spec("multi-svc")
        store: InMemoryStore = server_mod.app.state.store

        resp = await client.post(
            "/onboard/confirm",
            json={"spec": spec.model_dump()},
            headers=auth_headers,
        )

        assert resp.status_code == 202
        data = resp.json()
        assert data["name"] == "multi-svc"
        job_id = data["job_id"]

        # Poll until done
        job_status = await _poll_job_until_done(client, job_id, auth_headers)
        assert job_status["phase"] == "done"

        # Primary record
        primary = await store.get("multi-svc")
        assert primary is not None
        assert primary.state == ServiceState.RUNNING
        assert primary.component_id == ""  # primary has empty component_id

        # Sibling record
        sib = await store.get("multi-svc-worker")
        assert sib is not None
        assert sib.state == ServiceState.RUNNING
        assert sib.component_id == "multi-svc"
        assert sib.image == "ghcr.io/org/multi-svc-worker:v1"

        # ComponentConfig should have siblings populated
        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        all_configs = config_store.all()
        assert len(all_configs) == 1
        assert all_configs[0].id == "multi-svc"
        assert len(all_configs[0].siblings) == 1
        assert all_configs[0].siblings[0].service_key == "worker"

    async def test_confirm_multi_service_rollback_on_sibling_deploy_failure(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """If sibling deploy fails, primary AND sibling records are removed."""
        spec = _make_multi_service_derived_spec("fail-multi")
        store: InMemoryStore = server_mod.app.state.store
        config_store: ComponentConfigStore = server_mod.app.state.component_config_store
        registry: ComponentRegistry = server_mod.app.state.registry

        # Make the backend fail on the second deploy call (the sibling)
        original_deploy = server_mod.app.state.backend.deploy
        call_count = [0]

        async def failing_deploy(service, config, image_ref):
            call_count[0] += 1
            if call_count[0] == 1:
                return await original_deploy(service, config, image_ref)
            raise RuntimeError("simulated sibling deploy failure")

        with patch.object(
            server_mod.app.state.backend,
            "deploy",
            side_effect=failing_deploy,
        ):
            resp = await client.post(
                "/onboard/confirm",
                json={"spec": spec.model_dump()},
                headers=auth_headers,
            )

            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            # Poll until failed (still inside the with-block so patch is active)
            job_status = await _poll_job_until_done(client, job_id, auth_headers)
            assert job_status["phase"] == "failed"
            assert "simulated sibling deploy failure" in job_status["error"]

        # Both primary and sibling records should be removed
        assert await store.get("fail-multi") is None
        assert await store.get("fail-multi-worker") is None

        # Config should be removed
        assert len(config_store.all()) == 0

        # Registry should not contain the entry
        assert registry.get("fail-multi") is None

    async def test_confirm_single_service_siblings_empty_unchanged(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """Single-service confirm: no sibling records, existing behavior preserved."""
        spec = _make_derived_spec("plain-svc")
        store: InMemoryStore = server_mod.app.state.store

        resp = await client.post(
            "/onboard/confirm",
            json={"spec": spec.model_dump()},
            headers=auth_headers,
        )

        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Poll until done
        job_status = await _poll_job_until_done(client, job_id, auth_headers)
        assert job_status["phase"] == "done"

        # Only primary record exists
        primary = await store.get("plain-svc")
        assert primary is not None
        assert primary.state == ServiceState.RUNNING

        # No sibling records
        all_records = await store.list_all()
        assert len(all_records) == 1
        assert all_records[0].name == "plain-svc"

    async def test_preflight_multi_service_returns_siblings(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """POST /onboard/preflight mocked to return 2-service parse includes siblings."""
        spec = _make_multi_service_derived_spec("auto-mail")

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=RepoFiles(
                    compose_bytes=b"fake compose bytes", config_json=None
                ),
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={
                    "git_url": "https://github.com/org/auto-mail.git",
                    "name": "auto-mail",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "spec" in data
        assert data["spec"]["name"] == "auto-mail"
        assert len(data["spec"]["siblings"]) == 1
        assert data["spec"]["siblings"][0]["service_key"] == "worker"


# ---------------------------------------------------------------------------
# Preflight with config.json
# ---------------------------------------------------------------------------


class TestOnboardPreflightWithConfig:
    async def test_preflight_includes_config_schema_when_present(
        self, client: AsyncClient, auth_headers: dict
    ):
        spec = _make_derived_spec("cool-app")
        spec.config_volume = (
            "cool-app-config"  # required by preflight gate when config.json present
        )
        schema_json_bytes = json.dumps(SAMPLE_SCHEMA).encode()

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=RepoFiles(
                    compose_bytes=b"fake compose bytes",
                    config_json=None,
                    config_schema_json=schema_json_bytes,
                ),
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={
                    "git_url": "https://github.com/org/cool-app.git",
                    "name": "cool-app",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "spec" in data
        assert data["spec"]["config_schema"] == SAMPLE_SCHEMA

    async def test_preflight_config_schema_null_when_absent(
        self, client: AsyncClient, auth_headers: dict
    ):
        spec = _make_derived_spec("cool-app")

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=RepoFiles(
                    compose_bytes=b"fake compose bytes",
                    config_json=None,
                ),
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={
                    "git_url": "https://github.com/org/cool-app.git",
                    "name": "cool-app",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["spec"]["config_schema"] is None

    async def test_preflight_invalid_config_schema_json_returns_422(
        self, client: AsyncClient, auth_headers: dict
    ):
        spec = _make_derived_spec("cool-app")

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=RepoFiles(
                    compose_bytes=b"fake compose bytes",
                    config_json=None,
                    config_schema_json=b"{invalid",
                ),
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={
                    "git_url": "https://github.com/org/cool-app.git",
                    "name": "cool-app",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 422
        data = resp.json()
        assert "not valid JSON" in data["error"]

    async def test_preflight_gate_missing_config_target_label(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Preflight returns 422 when config schema is present but no robotsix.deploy.config-target label."""
        spec = _make_derived_spec("cool-app")
        # config_volume NOT set — simulates missing config-target label
        schema_json_bytes = json.dumps(SAMPLE_SCHEMA).encode()

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=RepoFiles(
                    compose_bytes=b"fake compose bytes",
                    config_json=None,
                    config_schema_json=schema_json_bytes,
                ),
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={
                    "git_url": "https://github.com/org/cool-app.git",
                    "name": "cool-app",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 422
        data = resp.json()
        assert "robotsix.deploy.config-target" in data["error"]

    async def test_preflight_gate_config_target_without_schema(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Preflight returns 422 when config-target label is set but no config schema is found."""
        spec = _make_derived_spec("cool-app")
        spec.config_volume = "cool-app-config"
        # config_schema will be None because config_schema_json is None

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=RepoFiles(
                    compose_bytes=b"fake compose bytes",
                    config_json=None,
                    config_json_template=None,
                    config_schema_json=None,
                ),
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={
                    "git_url": "https://github.com/org/cool-app.git",
                    "name": "cool-app",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 422
        data = resp.json()
        assert "no config file or template was found" in data["error"]

    async def test_preflight_yaml_only_no_schema_gives_no_config_schema(
        self, client: AsyncClient, auth_headers: dict
    ):
        """When only config.json exists (no schema JSON), config_schema is None."""
        spec = _make_derived_spec("cool-app")
        config_json_bytes = b'{"host": "localhost"}'

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=RepoFiles(
                    compose_bytes=b"fake compose bytes",
                    config_json=config_json_bytes,
                    config_json_template=None,
                    config_schema_json=None,
                ),
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={
                    "git_url": "https://github.com/org/cool-app.git",
                    "name": "cool-app",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["spec"]["config_schema"] is None


# ---------------------------------------------------------------------------
# Confirm with config.json
# ---------------------------------------------------------------------------


class TestOnboardConfirmWithConfig:
    async def test_confirm_with_config_schema_saves_template_and_writes_volume(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        spec = _make_derived_spec("cfg-svc")
        spec.config_schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "password": {"type": "string", "format": "password", "writeOnly": True},
            },
        }
        # Simulate a real compose: config-target label resolves to a volume
        # that's also declared as a named-volume mount.
        spec.config_volume = "cfg-svc-data"
        spec.volume_mounts.append(VolumeMount(host="cfg-svc-data", container="/cfg"))

        # Track write_config_to_volume calls
        captured: list[tuple] = []
        original_write = server_mod.app.state.backend.write_config_to_volume

        async def _fake_write(volume_name: str, config_dict: dict) -> None:
            captured.append((volume_name, config_dict))
            return await original_write(volume_name, config_dict)

        monkeypatch.setattr(
            server_mod.app.state.backend,
            "write_config_to_volume",
            _fake_write,
        )

        resp = await client.post(
            "/onboard/confirm",
            json={"spec": spec.model_dump()},
            headers=auth_headers,
        )

        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Poll until done
        job_status = await _poll_job_until_done(client, job_id, auth_headers)
        assert job_status["phase"] == "done"

        # Template saved in ConfigYamlStore
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        template = await store.get_template("cfg-svc")
        assert template == spec.config_schema

        # Volume written via backend — uses the real config volume (now namespaced), not synthetic
        assert len(captured) == 1
        assert captured[0][0] == "cfg-svc-cfg-svc-data"
        assert captured[0][1] == {"host": "", "password": ""}

        # named_volumes includes the config volume (from spec.volume_mounts)
        registry_obj: ComponentRegistry = server_mod.app.state.registry
        in_memory_config = registry_obj.get("cfg-svc")
        assert in_memory_config is not None
        assert "cfg-svc-cfg-svc-data" in in_memory_config.named_volumes

    async def test_confirm_deploy_failure_cleans_up_config_yaml_store(
        self, client: AsyncClient, auth_headers: dict
    ):
        spec = _make_derived_spec("fail-cfg")
        spec.config_schema = {
            "type": "object",
            "properties": {"host": {"type": "string"}},
        }

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

            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            # Poll until failed (still inside the with-block so patch is active)
            job_status = await _poll_job_until_done(client, job_id, auth_headers)
            assert job_status["phase"] == "failed"

        # config_yaml_store should be cleaned up
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        assert await store.get_template("fail-cfg") is None

    async def test_confirm_without_config_schema_no_template_saved(
        self, client: AsyncClient, auth_headers: dict
    ):
        spec = _make_derived_spec("no-cfg-svc")
        spec.config_schema = None

        resp = await client.post(
            "/onboard/confirm",
            json={"spec": spec.model_dump()},
            headers=auth_headers,
        )

        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Poll until done
        job_status = await _poll_job_until_done(client, job_id, auth_headers)
        assert job_status["phase"] == "done"

        # No template saved
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        assert await store.get_template("no-cfg-svc") is None

    async def test_confirm_with_config_values_merges_and_writes_volume(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """config_values from the UI are merged with template and written to volume."""
        spec = _make_derived_spec("cfg-svc")
        spec.config_schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "password": {"type": "string", "format": "password", "writeOnly": True},
                "port": {"type": "integer", "default": 8080},
            },
        }
        spec.config_volume = "cfg-svc-data"
        spec.volume_mounts.append(VolumeMount(host="cfg-svc-data", container="/cfg"))

        # Track write_config_to_volume calls
        captured: list[tuple] = []
        original_write = server_mod.app.state.backend.write_config_to_volume

        async def _fake_write(volume_name: str, config_dict: dict) -> None:
            captured.append((volume_name, config_dict))
            return await original_write(volume_name, config_dict)

        monkeypatch.setattr(
            server_mod.app.state.backend,
            "write_config_to_volume",
            _fake_write,
        )

        resp = await client.post(
            "/onboard/confirm",
            json={
                "spec": spec.model_dump(),
                "config_values": {"host": "10.0.0.1", "password": "s3cret"},
            },
            headers=auth_headers,
        )

        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Poll until done
        job_status = await _poll_job_until_done(client, job_id, auth_headers)
        assert job_status["phase"] == "done"

        # Merged config written to volume — user values overlay template defaults
        assert len(captured) == 1
        assert captured[0][0] == "cfg-svc-cfg-svc-data"
        assert captured[0][1] == {
            "host": "10.0.0.1",
            "password": "s3cret",
            "port": 8080,
        }

        # current stored in ConfigYamlStore reflects entered values
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        current = await store.get_current("cfg-svc")
        assert current == {"host": "10.0.0.1", "password": "s3cret", "port": 8080}

    async def test_confirm_without_config_values_writes_template_only(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """Without config_values, the template is written as-is (back-compat)."""
        spec = _make_derived_spec("cfg-svc2")
        spec.config_schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "password": {"type": "string", "format": "password", "writeOnly": True},
            },
        }
        spec.config_volume = "cfg-svc2-data"
        spec.volume_mounts.append(VolumeMount(host="cfg-svc2-data", container="/cfg"))

        captured: list[tuple] = []
        original_write = server_mod.app.state.backend.write_config_to_volume

        async def _fake_write(volume_name: str, config_dict: dict) -> None:
            captured.append((volume_name, config_dict))
            return await original_write(volume_name, config_dict)

        monkeypatch.setattr(
            server_mod.app.state.backend,
            "write_config_to_volume",
            _fake_write,
        )

        # No config_values field at all
        resp = await client.post(
            "/onboard/confirm",
            json={"spec": spec.model_dump()},
            headers=auth_headers,
        )

        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Poll until done
        job_status = await _poll_job_until_done(client, job_id, auth_headers)
        assert job_status["phase"] == "done"

        # Template written as-is (empty defaults)
        assert len(captured) == 1
        assert captured[0][1] == {"host": "", "password": ""}

        # current still stored (equals template defaults since no user values)
        store: ConfigYamlStore = server_mod.app.state.config_yaml_store
        current = await store.get_current("cfg-svc2")
        assert current == {"host": "", "password": ""}


# ---------------------------------------------------------------------------
# New JSON Schema-driven config tests
# ---------------------------------------------------------------------------


class TestOnboardConfigSchemaValidation:
    """Tests for JSON Schema-driven config validation during preflight and confirm."""

    async def test_preflight_returns_json_schema_in_spec(
        self, client: AsyncClient, auth_headers: dict
    ):
        """config_schema_json present → spec.config_schema equals the parsed schema dict."""
        spec = _make_derived_spec("cool-app")
        spec.config_volume = "cool-app-config"
        schema_json_bytes = json.dumps(SAMPLE_SCHEMA).encode()

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=RepoFiles(
                    compose_bytes=b"fake compose bytes",
                    config_json=None,
                    config_schema_json=schema_json_bytes,
                ),
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={
                    "git_url": "https://github.com/org/cool-app.git",
                    "name": "cool-app",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["spec"]["config_schema"] == SAMPLE_SCHEMA

    async def test_preflight_invalid_json_returns_422(
        self, client: AsyncClient, auth_headers: dict
    ):
        """config_schema_json with invalid JSON → 422."""
        spec = _make_derived_spec("cool-app")

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=RepoFiles(
                    compose_bytes=b"fake compose bytes",
                    config_json=None,
                    config_schema_json=b"{invalid",
                ),
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={
                    "git_url": "https://github.com/org/cool-app.git",
                    "name": "cool-app",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 422
        assert "not valid JSON" in resp.json()["error"]

    async def test_preflight_yaml_only_no_schema_gives_no_config_schema(
        self, client: AsyncClient, auth_headers: dict
    ):
        """config_json present but config_schema_json=None → spec.config_schema is None."""
        spec = _make_derived_spec("cool-app")
        config_json_bytes = b'{"host": "localhost"}'

        with (
            patch(
                "robotsix_central_deploy.onboard.fetcher.fetch_repo_files",
                return_value=RepoFiles(
                    compose_bytes=b"fake compose bytes",
                    config_json=config_json_bytes,
                    config_schema_json=None,
                ),
            ),
            patch(
                "robotsix_central_deploy.onboard.parser.parse_compose",
                return_value=spec,
            ),
        ):
            resp = await client.post(
                "/onboard/preflight",
                json={
                    "git_url": "https://github.com/org/cool-app.git",
                    "name": "cool-app",
                },
                headers=auth_headers,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["spec"]["config_schema"] is None

    async def test_confirm_missing_required_field_returns_422(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Submit invalid type for a field → 422."""
        spec = _make_derived_spec("cfg-req")
        spec.config_schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "port": {"type": "integer", "default": 5432},
            },
            "required": ["host"],
        }
        spec.config_volume = "cfg-req-data"

        resp = await client.post(
            "/onboard/confirm",
            json={
                "spec": spec.model_dump(),
                "config_values": {"host": "", "port": "not-a-number"},
            },
            headers=auth_headers,
        )

        assert resp.status_code == 422
        error_msg = resp.json()["error"]
        assert "integer" in error_msg.lower() or "port" in error_msg.lower()

    async def test_confirm_wrong_type_returns_422(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Submit {"port": "not-a-number"} for integer field → 422."""
        spec = _make_derived_spec("cfg-type")
        spec.config_schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "port": {"type": "integer", "default": 5432},
            },
        }
        spec.config_volume = "cfg-type-data"

        resp = await client.post(
            "/onboard/confirm",
            json={
                "spec": spec.model_dump(),
                "config_values": {"host": "example.com", "port": "not-a-number"},
            },
            headers=auth_headers,
        )

        assert resp.status_code == 422
        error_msg = resp.json()["error"]
        assert "integer" in error_msg.lower()

    async def test_confirm_invalid_enum_returns_422(
        self, client: AsyncClient, auth_headers: dict
    ):
        """Submit {"mode": "invalid"} for enum field → 422."""
        spec = _make_derived_spec("cfg-enum")
        spec.config_schema = {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["auto", "manual"]},
            },
        }
        spec.config_volume = "cfg-enum-data"

        resp = await client.post(
            "/onboard/confirm",
            json={
                "spec": spec.model_dump(),
                "config_values": {"mode": "invalid"},
            },
            headers=auth_headers,
        )

        assert resp.status_code == 422
        error_msg = resp.json()["error"]
        assert "enum" in error_msg.lower() or "invalid" in error_msg.lower()

    async def test_confirm_omitted_nullable_object_field_validates(
        self, client: AsyncClient, auth_headers: dict
    ):
        """A nullable-object field (anyOf[object, null]) that is omitted from
        config_values validates and onboards (202). Mirrors the mill's
        ``repos.meta`` schema and the JS form fix that omits empty optionals
        rather than emitting an invalid "".
        """
        spec = _make_derived_spec("cfg-nullable")
        spec.config_schema = {
            "type": "object",
            "$defs": {
                "RepoConfig": {
                    "type": "object",
                    "properties": {"track": {"type": "string"}},
                }
            },
            "properties": {
                "host": {"type": "string"},
                "meta": {
                    "anyOf": [
                        {"$ref": "#/$defs/RepoConfig"},
                        {"type": "null"},
                    ],
                    "default": None,
                },
            },
        }
        spec.config_volume = "cfg-nullable-data"

        resp = await client.post(
            "/onboard/confirm",
            json={
                "spec": spec.model_dump(),
                "config_values": {"host": "example.com"},  # meta omitted
            },
            headers=auth_headers,
        )

        assert resp.status_code == 202

    async def test_confirm_empty_string_for_nullable_object_field_returns_422(
        self, client: AsyncClient, auth_headers: dict
    ):
        """The pre-fix failure mode: submitting "" for a nullable-object field
        fails validation (anyOf[object, null]) → 422. This is exactly what the
        JS form used to emit; the client-side fix omits the field instead.
        """
        spec = _make_derived_spec("cfg-nullable-bad")
        spec.config_schema = {
            "type": "object",
            "$defs": {
                "RepoConfig": {
                    "type": "object",
                    "properties": {"track": {"type": "string"}},
                }
            },
            "properties": {
                "meta": {
                    "anyOf": [
                        {"$ref": "#/$defs/RepoConfig"},
                        {"type": "null"},
                    ],
                    "default": None,
                },
            },
        }
        spec.config_volume = "cfg-nullable-bad-data"

        resp = await client.post(
            "/onboard/confirm",
            json={
                "spec": spec.model_dump(),
                "config_values": {"meta": ""},
            },
            headers=auth_headers,
        )

        assert resp.status_code == 422

    async def test_confirm_secret_preserved_when_sentinel_submitted(
        self, client: AsyncClient, auth_headers: dict, monkeypatch
    ):
        """During confirm with no existing config, "***" for a secret field
        defaults to empty string since there's no existing value to preserve."""
        spec = _make_derived_spec("cfg-secret")
        spec.config_schema = {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "api_key": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                },
            },
        }
        spec.config_volume = "cfg-secret-data"

        captured: list[dict] = []
        original_write = server_mod.app.state.backend.write_config_to_volume

        async def _fake_write(volume_name: str, config_dict: dict) -> None:
            captured.append(config_dict)
            return await original_write(volume_name, config_dict)

        monkeypatch.setattr(
            server_mod.app.state.backend,
            "write_config_to_volume",
            _fake_write,
        )

        resp = await client.post(
            "/onboard/confirm",
            json={
                "spec": spec.model_dump(),
                "config_values": {"host": "10.0.0.1", "api_key": "***"},
            },
            headers=auth_headers,
        )

        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        job_status = await _poll_job_until_done(client, job_id, auth_headers)
        assert job_status["phase"] == "done"

        # "***" with no existing → empty string
        assert len(captured) == 1
        assert captured[0]["api_key"] == ""
        assert captured[0]["host"] == "10.0.0.1"

    async def test_confirm_secret_detected_by_format_writeonly_not_sentinel(
        self, client: AsyncClient, auth_headers: dict
    ):
        """A field with format:password+writeOnly:true is treated as secret;
        a field with value "SECRET" (old sentinel) is NOT treated as secret."""
        spec = _make_derived_spec("cfg-fmt")
        spec.config_schema = {
            "type": "object",
            "properties": {
                "api_key": {
                    "type": "string",
                    "format": "password",
                    "writeOnly": True,
                },
                "legacy_field": {"type": "string"},
            },
        }
        spec.config_volume = "cfg-fmt-data"

        resp = await client.post(
            "/onboard/confirm",
            json={
                "spec": spec.model_dump(),
                "config_values": {
                    "api_key": "***",
                    "legacy_field": "SECRET",
                },
            },
            headers=auth_headers,
        )

        # Should NOT treat "SECRET" as a sentinel — should be accepted as literal string
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Onboard → Mill integration tests
# ---------------------------------------------------------------------------


class TestOnboardMillIntegration:
    """Tests for mill repo registration during onboard."""

    @pytest.mark.asyncio
    async def test_confirm_sets_repo_id_when_register_true(
        self, client: AsyncClient, auth_headers: dict, tmp_path
    ):
        """When caretaker is enabled and register_with_mill=True, repo_id is set from git_url."""
        ss = SystemSettingsStore(tmp_path / "settings2.json")
        await ss.put(SystemSettings(caretaker_enabled=True))
        server_mod.app.state.settings_store = ss

        spec = _make_derived_spec(name="zz-test-repo")
        spec = spec.model_copy(
            update={"git_url": "https://github.com/org/my-cool-repo.git"}
        )
        req = {"spec": spec.model_dump(), "register_with_mill": True}

        with patch(
            "robotsix_central_deploy.lifecycle.routers.onboard._run_onboard_deploy_job",
            new=AsyncMock(),
        ):
            resp = await client.post("/onboard/confirm", json=req, headers=auth_headers)
            assert resp.status_code == 202

        cfg = server_mod.app.state.component_config_store.get("zz-test-repo")
        assert cfg is not None
        assert cfg.repo_id == "my-cool-repo"

    @pytest.mark.asyncio
    async def test_confirm_clears_repo_id_when_register_false(
        self, client: AsyncClient, auth_headers: dict, tmp_path
    ):
        """When register_with_mill=False, repo_id is empty."""
        ss = SystemSettingsStore(tmp_path / "settings3.json")
        await ss.put(SystemSettings(caretaker_enabled=True))
        server_mod.app.state.settings_store = ss

        spec = _make_derived_spec(name="zz-no-register")
        req = {"spec": spec.model_dump(), "register_with_mill": False}

        with patch(
            "robotsix_central_deploy.lifecycle.routers.onboard._run_onboard_deploy_job",
            new=AsyncMock(),
        ):
            resp = await client.post("/onboard/confirm", json=req, headers=auth_headers)
            assert resp.status_code == 202

        cfg = server_mod.app.state.component_config_store.get("zz-no-register")
        assert cfg is not None
        assert cfg.repo_id == ""

    @pytest.mark.asyncio
    async def test_mill_component_forced_no_auto_update(
        self, client: AsyncClient, auth_headers: dict, tmp_path
    ):
        """Component with id=='mill' always gets caretaker_auto_update=False."""
        ss = SystemSettingsStore(tmp_path / "settings4.json")
        await ss.put(SystemSettings(caretaker_enabled=True))
        server_mod.app.state.settings_store = ss

        spec = _make_derived_spec(name="mill")
        req = {"spec": spec.model_dump(), "register_with_mill": True}

        with patch(
            "robotsix_central_deploy.lifecycle.routers.onboard._run_onboard_deploy_job",
            new=AsyncMock(),
        ):
            resp = await client.post("/onboard/confirm", json=req, headers=auth_headers)
            assert resp.status_code == 202

        cfg = server_mod.app.state.component_config_store.get("mill")
        assert cfg is not None
        assert cfg.caretaker_auto_update is False

    @pytest.mark.asyncio
    async def test_background_job_skips_when_untracked(
        self, client: AsyncClient, auth_headers: dict, tmp_path
    ):
        """When repo_id is empty, mill is never called in the deploy job."""
        ss = SystemSettingsStore(tmp_path / "settings5.json")
        await ss.put(SystemSettings(caretaker_enabled=True))
        server_mod.app.state.settings_store = ss

        mock_http = MagicMock()
        mock_http.post = AsyncMock()
        server_mod.app.state.http_client = mock_http

        spec = _make_derived_spec(name="zz-no-track")
        req = {"spec": spec.model_dump(), "register_with_mill": False}

        with patch(
            "robotsix_central_deploy.lifecycle.routers.onboard._run_onboard_deploy_job",
            new=AsyncMock(),
        ):
            resp = await client.post("/onboard/confirm", json=req, headers=auth_headers)
            assert resp.status_code == 202

        cfg = server_mod.app.state.component_config_store.get("zz-no-track")
        assert cfg is not None
        assert cfg.repo_id == ""
