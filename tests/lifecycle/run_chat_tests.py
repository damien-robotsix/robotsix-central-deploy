"""Standalone test runner for chat-agent write-surface tests.

Runs the tests from test_chat_agent.py without pytest — needed when the
sandbox cannot reach the network and pytest is not installed.

Usage:
    python3 tests/lifecycle/run_chat_tests.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add src to path so we can import the package under test.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

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
# Helpers (mirror test_chat_agent.py)
# ---------------------------------------------------------------------------

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


def _make_config(
    component_id: str = "robotsix-chat",
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
# Test runner infrastructure
# ---------------------------------------------------------------------------

class TestContext:
    """Mutable bag that holds per-test fixtures and state."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.state_dir = tmp_path / "_chat_test"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Build fixtures
        self.cfg = LifecycleConfig(  # type: ignore[call-arg]
            store_backend="memory",
            execution_backend=ExecutionBackendType.NOOP,
            api_key="test-key",
        )
        self.store = InMemoryStore()
        self.backend = NoopBackend()
        self.config_yaml_store = ConfigYamlStore(self.state_dir / "config_yaml.json")
        self.audit_store = ChatAgentAuditStore(
            self.state_dir / "chat_agent_audit.json"
        )
        self.component_config_store = ComponentConfigStore(
            self.state_dir / "component_configs.json"
        )
        self.component_config_store.register(
            _make_config("robotsix-chat", "ghcr.io/test/robotsix-chat:main")
        )
        self.component_config_store.register(
            _make_config("cognee", "ghcr.io/test/cognee:main")
        )
        self.component_config_store.register(
            _make_config("other-svc", "ghcr.io/test/other:main")
        )
        self.registry = ComponentRegistry(list(self.component_config_store.all()))

        # Wire app.state
        import os

        os.environ["ROBOTSIX_LIFECYCLE_API_KEY"] = "test-key"

        mock_checker = MagicMock()
        mock_checker.get_latest_digest = AsyncMock(return_value=None)

        km = SecretKeyManager(self.state_dir / "secrets.key")
        env_store = EnvStore(self.state_dir / "env.json", km)
        deploy_history_store = DeployHistoryStore(
            self.state_dir / "deploy_history.json"
        )

        server_mod._config = self.cfg
        server_mod._store = self.store
        server_mod._backend = self.backend
        server_mod._registry_checker = mock_checker
        server_mod.app.state.config = self.cfg
        server_mod.app.state.store = self.store
        server_mod.app.state.backend = self.backend
        server_mod.app.state.registry_checker = mock_checker
        server_mod.app.state.key_manager = km
        server_mod.app.state.env_store = env_store
        server_mod.app.state.config_yaml_store = self.config_yaml_store
        server_mod.app.state.deploy_history_store = deploy_history_store
        server_mod.app.state.chat_agent_audit_store = self.audit_store
        server_mod.app.state.chat_agent_rate_limits = {}
        server_mod.app.state.component_config_store = self.component_config_store
        server_mod.app.state.registry = self.registry
        server_mod.app.state.job_registry = JobRegistry()

        self.auth_headers = {"X-API-Key": "test-key"}

    async def client(self) -> AsyncClient:
        transport = ASGITransport(app=server_mod.app)  # type: ignore[arg-type]
        return AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Test functions (adapted from test_chat_agent.py)
# ---------------------------------------------------------------------------


async def test_chat_config_update_happy_path(ctx: TestContext) -> None:
    await ctx.config_yaml_store.save_template("robotsix-chat", _CONFIG_TEMPLATE)
    await ctx.store.put(
        ServiceRecord(name="robotsix-chat", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        resp = await client.put(
            "/chat/config/robotsix-chat",
            json={"values": {"debug": True, "log_level": "debug"}},
            headers=ctx.auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["component"] == "robotsix-chat"
        assert data["restored"]["debug"] is True
        assert data["restored"]["log_level"] == "debug"
        assert data["restored"]["api_token"] == ""


async def test_chat_config_update_rejects_secret_keys(ctx: TestContext) -> None:
    await ctx.config_yaml_store.save_template("robotsix-chat", _CONFIG_TEMPLATE)
    await ctx.store.put(
        ServiceRecord(name="robotsix-chat", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        resp = await client.put(
            "/chat/config/robotsix-chat",
            json={"values": {"debug": True, "api_token": "leaked-secret"}},
            headers=ctx.auth_headers,
        )
        assert resp.status_code == 403, resp.text
        assert "api_token" in resp.json()["error"]


async def test_chat_config_update_secret_in_nested_object(ctx: TestContext) -> None:
    await ctx.config_yaml_store.save_template("robotsix-chat", _CONFIG_TEMPLATE)
    await ctx.store.put(
        ServiceRecord(name="robotsix-chat", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        resp = await client.put(
            "/chat/config/robotsix-chat",
            json={"values": {"nested": {"host": "newhost", "secret_key": "bad"}}},
            headers=ctx.auth_headers,
        )
        assert resp.status_code == 403, resp.text
        assert "secret_key" in resp.json()["error"]


async def test_chat_config_update_not_allowlisted(ctx: TestContext) -> None:
    await ctx.config_yaml_store.save_template("other-svc", _CONFIG_TEMPLATE)
    await ctx.store.put(
        ServiceRecord(name="other-svc", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        resp = await client.put(
            "/chat/config/other-svc",
            json={"values": {"debug": True}},
            headers=ctx.auth_headers,
        )
        assert resp.status_code == 403


async def test_chat_config_update_no_schema_returns_404(ctx: TestContext) -> None:
    await ctx.store.put(
        ServiceRecord(name="robotsix-chat", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        resp = await client.put(
            "/chat/config/robotsix-chat",
            json={"values": {"debug": True}},
            headers=ctx.auth_headers,
        )
        assert resp.status_code == 404


async def test_chat_config_rollback_happy_path(ctx: TestContext) -> None:
    await ctx.config_yaml_store.save_template("robotsix-chat", _CONFIG_TEMPLATE)
    await ctx.store.put(
        ServiceRecord(name="robotsix-chat", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        await client.put(
            "/chat/config/robotsix-chat",
            json={"values": {"debug": True, "log_level": "debug"}},
            headers=ctx.auth_headers,
        )
        resp = await client.post(
            "/chat/config/robotsix-chat/rollback",
            headers=ctx.auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["component"] == "robotsix-chat"
        assert data["restored"]["debug"] is False
        assert data["restored"]["log_level"] == "info"


async def test_chat_config_rollback_no_previous(ctx: TestContext) -> None:
    await ctx.config_yaml_store.save_template("robotsix-chat", _CONFIG_TEMPLATE)
    await ctx.store.put(
        ServiceRecord(name="robotsix-chat", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        resp = await client.post(
            "/chat/config/robotsix-chat/rollback",
            headers=ctx.auth_headers,
        )
        assert resp.status_code == 404


async def test_chat_config_rollback_not_allowlisted(ctx: TestContext) -> None:
    async with await ctx.client() as client:
        resp = await client.post(
            "/chat/config/other-svc/rollback",
            headers=ctx.auth_headers,
        )
        assert resp.status_code == 403


async def test_chat_restart_happy_path(ctx: TestContext) -> None:
    await ctx.store.put(
        ServiceRecord(name="robotsix-chat", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        resp = await client.post(
            "/chat/services/robotsix-chat/restart",
            headers=ctx.auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["name"] == "robotsix-chat"
        assert data["action"] == "restart"
        assert data["previous_state"] == "running"
        assert data["current_state"] == "running"


async def test_chat_restart_not_allowlisted(ctx: TestContext) -> None:
    await ctx.store.put(
        ServiceRecord(name="other-svc", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        resp = await client.post(
            "/chat/services/other-svc/restart",
            headers=ctx.auth_headers,
        )
        assert resp.status_code == 403


async def test_chat_restart_rate_limited(ctx: TestContext) -> None:
    await ctx.store.put(
        ServiceRecord(name="robotsix-chat", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        resp1 = await client.post(
            "/chat/services/robotsix-chat/restart",
            headers=ctx.auth_headers,
        )
        assert resp1.status_code == 200
        resp2 = await client.post(
            "/chat/services/robotsix-chat/restart",
            headers=ctx.auth_headers,
        )
        assert resp2.status_code == 429
        assert "Rate limit" in resp2.json()["error"]


async def test_chat_update_happy_path(ctx: TestContext) -> None:
    await ctx.store.put(
        ServiceRecord(name="robotsix-chat", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        resp = await client.post(
            "/chat/services/robotsix-chat/update",
            headers=ctx.auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["name"] == "robotsix-chat"
        assert data["action"] == "update"
        assert data["deployed_digest"] == "sha256:noop"
        assert data["current_state"] == "running"


async def test_chat_update_not_allowlisted(ctx: TestContext) -> None:
    await ctx.store.put(
        ServiceRecord(name="other-svc", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        resp = await client.post(
            "/chat/services/other-svc/update",
            headers=ctx.auth_headers,
        )
        assert resp.status_code == 403


async def test_chat_update_rate_limited(ctx: TestContext) -> None:
    await ctx.store.put(
        ServiceRecord(name="robotsix-chat", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        resp1 = await client.post(
            "/chat/services/robotsix-chat/update",
            headers=ctx.auth_headers,
        )
        assert resp1.status_code == 200
        resp2 = await client.post(
            "/chat/services/robotsix-chat/update",
            headers=ctx.auth_headers,
        )
        assert resp2.status_code == 429
        assert "Rate limit" in resp2.json()["error"]


async def test_chat_audit_log(ctx: TestContext) -> None:
    await ctx.config_yaml_store.save_template("robotsix-chat", _CONFIG_TEMPLATE)
    await ctx.store.put(
        ServiceRecord(name="robotsix-chat", state=ServiceState.RUNNING)
    )
    async with await ctx.client() as client:
        await client.put(
            "/chat/config/robotsix-chat",
            json={"values": {"debug": True}},
            headers=ctx.auth_headers,
        )
        resp = await client.get("/chat/audit-log", headers=ctx.auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["entries"]) >= 1
        entry = data["entries"][0]
        assert entry["component"] == "robotsix-chat"
        assert entry["action"] == "config_update"
        assert entry["key"] == "debug"
        assert entry["new_value"] is True


async def test_chat_audit_log_filtered(ctx: TestContext) -> None:
    await ctx.config_yaml_store.save_template("cognee", _CONFIG_TEMPLATE)
    await ctx.config_yaml_store.save_template("robotsix-chat", _CONFIG_TEMPLATE)
    await ctx.store.put(
        ServiceRecord(name="robotsix-chat", state=ServiceState.RUNNING)
    )
    await ctx.store.put(ServiceRecord(name="cognee", state=ServiceState.RUNNING))
    async with await ctx.client() as client:
        await client.put(
            "/chat/config/cognee",
            json={"values": {"debug": True}},
            headers=ctx.auth_headers,
        )
        resp = await client.get(
            "/chat/audit-log?component=cognee", headers=ctx.auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        for entry in data["entries"]:
            assert entry["component"] == "cognee"


async def test_chat_endpoints_require_auth(ctx: TestContext) -> None:
    await ctx.config_yaml_store.save_template("robotsix-chat", _CONFIG_TEMPLATE)
    await ctx.store.put(
        ServiceRecord(name="robotsix-chat", state=ServiceState.RUNNING)
    )
    endpoints = [
        ("PUT", "/chat/config/robotsix-chat", {"values": {"debug": True}}),
        ("POST", "/chat/config/robotsix-chat/rollback", None),
        ("POST", "/chat/services/robotsix-chat/restart", None),
        ("POST", "/chat/services/robotsix-chat/update", None),
    ]
    async with await ctx.client() as client:
        for method, path, body in endpoints:
            if body is not None:
                resp = await client.request(method, path, json=body)
            else:
                resp = await client.request(method, path)
            assert resp.status_code == 401, f"{method} {path} should require auth"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_chat_config_update_happy_path,
    test_chat_config_update_rejects_secret_keys,
    test_chat_config_update_secret_in_nested_object,
    test_chat_config_update_not_allowlisted,
    test_chat_config_update_no_schema_returns_404,
    test_chat_config_rollback_happy_path,
    test_chat_config_rollback_no_previous,
    test_chat_config_rollback_not_allowlisted,
    test_chat_restart_happy_path,
    test_chat_restart_not_allowlisted,
    test_chat_restart_rate_limited,
    test_chat_update_happy_path,
    test_chat_update_not_allowlisted,
    test_chat_update_rate_limited,
    test_chat_audit_log,
    test_chat_audit_log_filtered,
    test_chat_endpoints_require_auth,
]


async def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        passed = 0
        failed = 0

        for test_fn in TESTS:
            ctx = TestContext(tmp_path / test_fn.__name__)
            try:
                await test_fn(ctx)
                print(f"  PASS  {test_fn.__name__}")
                passed += 1
            except Exception as exc:
                print(f"  FAIL  {test_fn.__name__}: {exc}")
                failed += 1
                import traceback

                traceback.print_exc()

        print(f"\n{'='*60}")
        print(f"Results: {passed} passed, {failed} failed, {len(TESTS)} total")
        if failed:
            print("SOME TESTS FAILED")
            sys.exit(1)
        else:
            print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
