"""Integration tests for the chat-agent Langfuse proxy endpoints."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod
from robotsix_central_deploy.lifecycle.config import LangfuseProjectCreds
from robotsix_central_deploy.lifecycle._langfuse_config import (
    _reconcile_auto_langfuse_projects,
)
from robotsix_central_deploy.registry.models import ComponentConfig


@asynccontextmanager
async def _mock_retry_client(mock_client):
    """Drop-in replacement for retry_client_context that yields *mock_client*."""
    yield mock_client


class _VolumeBackend:
    """Backend that serves component config from an in-memory volume.

    Langfuse discovery reads each component's own config file rather than a
    deploy-plane copy, so these tests seed the volume the component reads.
    """

    def __init__(self) -> None:
        self.volumes: dict[str, dict] = {}

    async def read_config_from_volume(self, volume_name: str) -> dict:
        return dict(self.volumes.get(volume_name, {}))


def _seed_component_config(component_id: str, config: dict) -> None:
    """Put *config* on the volume that *component_id* would read."""
    backend = getattr(server_mod.app.state, "backend", None)
    if not isinstance(backend, _VolumeBackend):
        backend = _VolumeBackend()
        server_mod.app.state.backend = backend
    backend.volumes[f"{component_id}-config"] = config


class TestLangfuseProxyAuth:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.get(
            "/chat/langfuse/robotsix-chat/traces",
        )
        assert resp.status_code == 401


class TestLangfuseProjectsEndpoint:
    async def test_lists_configured_projects(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """GET /chat/langfuse/projects returns only projects with both keys set."""
        cfg = server_mod.app.state.config
        cfg.langfuse_projects["robotsix-chat"] = LangfuseProjectCreds(
            public_key="pk-chat", secret_key="sk-chat"
        )
        cfg.langfuse_projects["robotsix-mill"] = LangfuseProjectCreds(
            public_key="pk-mill", secret_key="sk-mill"
        )
        # cognee keys are NOT set — should not appear.

        resp = await client.get(
            "/chat/langfuse/projects",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "robotsix-chat" in data
        assert "robotsix-mill" in data
        assert "cognee" not in data


class TestLangfuseUnknownProject:
    async def test_unknown_project_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """An unknown project alias in the path returns 404."""
        resp = await client.get(
            "/chat/langfuse/nonexistent/traces",
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert "Unknown Langfuse project alias" in resp.text


class TestLangfuseNotConfigured:
    async def test_503_when_no_credentials_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        """When keys for a known project are empty, the proxy returns 503."""
        cfg = server_mod.app.state.config
        cfg.langfuse_projects["robotsix-chat"] = LangfuseProjectCreds(
            public_key="", secret_key=""
        )

        resp = await client.get(
            "/chat/langfuse/robotsix-chat/traces",
            headers=auth_headers,
        )
        assert resp.status_code == 503
        assert "not configured" in resp.text


class TestLangfuseProxyTraces:
    async def test_proxies_traces_with_injected_basic_auth(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """GET /chat/langfuse/{project}/traces forwards to Langfuse with
        Basic Auth injected from the project's keys."""
        cfg = server_mod.app.state.config
        cfg.langfuse_projects["robotsix-chat"] = LangfuseProjectCreds(
            public_key="pk-chat", secret_key="sk-chat"
        )
        cfg.langfuse_base_url = "https://langfuse.example"

        captured_headers: dict[str, str] = {}
        captured_url: str = ""

        async def _fake_get(url, headers=None, **kwargs):
            nonlocal captured_url, captured_headers
            captured_url = str(url)
            captured_headers = dict(headers) if headers else {}
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.content = b'{"data":[]}'
            resp.headers = {"content-type": "application/json"}
            return resp

        fake_client = MagicMock()
        fake_client.get = _fake_get

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_langfuse.retry_client_context",
            lambda *a, **kw: _mock_retry_client(fake_client),
        )

        resp = await client.get(
            "/chat/langfuse/robotsix-chat/traces?limit=5&page=1",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == {"data": []}

        assert "limit=5" in captured_url
        assert "page=1" in captured_url

        import base64

        decoded = base64.b64decode(
            captured_headers["authorization"].split(" ", 1)[1]
        ).decode()
        assert decoded == "pk-chat:sk-chat"

    async def test_limit_capped_at_100(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """The limit query param is capped at 100 server-side."""
        cfg = server_mod.app.state.config
        cfg.langfuse_projects["robotsix-chat"] = LangfuseProjectCreds(
            public_key="pk", secret_key="sk"
        )
        cfg.langfuse_base_url = "https://langfuse.example"

        captured_url: str = ""

        async def _fake_get(url, headers=None, **kwargs):
            nonlocal captured_url
            captured_url = str(url)
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.content = b'{"data":[]}'
            resp.headers = {"content-type": "application/json"}
            return resp

        fake_client = MagicMock()
        fake_client.get = _fake_get

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_langfuse.retry_client_context",
            lambda *a, **kw: _mock_retry_client(fake_client),
        )

        resp = await client.get(
            "/chat/langfuse/robotsix-chat/traces?limit=500",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert "limit=100" in captured_url
        assert "limit=500" not in captured_url

    async def test_proxies_single_trace(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """GET /chat/langfuse/{project}/traces/{traceId} works."""
        cfg = server_mod.app.state.config
        cfg.langfuse_projects["robotsix-chat"] = LangfuseProjectCreds(
            public_key="pk-chat", secret_key="sk-chat"
        )
        cfg.langfuse_base_url = "https://langfuse.example"

        captured_url: str = ""

        async def _fake_get(url, headers=None, **kwargs):
            nonlocal captured_url
            captured_url = str(url)
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.content = b'{"id":"trace-1"}'
            resp.headers = {"content-type": "application/json"}
            return resp

        fake_client = MagicMock()
        fake_client.get = _fake_get

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_langfuse.retry_client_context",
            lambda *a, **kw: _mock_retry_client(fake_client),
        )

        resp = await client.get(
            "/chat/langfuse/robotsix-chat/traces/abc123",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert "https://langfuse.example/api/public/traces/abc123" in captured_url

    async def test_proxies_observations(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """GET /chat/langfuse/{project}/observations works."""
        cfg = server_mod.app.state.config
        cfg.langfuse_projects["robotsix-chat"] = LangfuseProjectCreds(
            public_key="pk-chat", secret_key="sk-chat"
        )
        cfg.langfuse_base_url = "https://langfuse.example"

        captured_url: str = ""

        async def _fake_get(url, headers=None, **kwargs):
            nonlocal captured_url
            captured_url = str(url)
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.content = b'{"data":[]}'
            resp.headers = {"content-type": "application/json"}
            return resp

        fake_client = MagicMock()
        fake_client.get = _fake_get

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_langfuse.retry_client_context",
            lambda *a, **kw: _mock_retry_client(fake_client),
        )

        resp = await client.get(
            "/chat/langfuse/robotsix-chat/observations",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert "https://langfuse.example/api/public/observations" in captured_url

    async def test_proxies_single_observation(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """GET /chat/langfuse/{project}/observations/{observationId} works."""
        cfg = server_mod.app.state.config
        cfg.langfuse_projects["robotsix-chat"] = LangfuseProjectCreds(
            public_key="pk-chat", secret_key="sk-chat"
        )
        cfg.langfuse_base_url = "https://langfuse.example"

        captured_url: str = ""

        async def _fake_get(url, headers=None, **kwargs):
            nonlocal captured_url
            captured_url = str(url)
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.content = b'{"id":"obs-1"}'
            resp.headers = {"content-type": "application/json"}
            return resp

        fake_client = MagicMock()
        fake_client.get = _fake_get

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_langfuse.retry_client_context",
            lambda *a, **kw: _mock_retry_client(fake_client),
        )

        resp = await client.get(
            "/chat/langfuse/robotsix-chat/observations/obs123",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert "https://langfuse.example/api/public/observations/obs123" in captured_url


class TestLangfuseProxyMillProject:
    async def test_proxies_with_mill_credentials(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """When the project path param is robotsix-mill, the proxy uses mill keys."""
        cfg = server_mod.app.state.config
        cfg.langfuse_projects["robotsix-mill"] = LangfuseProjectCreds(
            public_key="pk-mill", secret_key="sk-mill"
        )
        cfg.langfuse_base_url = "https://langfuse.example"

        captured_headers: dict[str, str] = {}

        async def _fake_get(url, headers=None, **kwargs):
            nonlocal captured_headers
            captured_headers = dict(headers) if headers else {}
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.content = b'{"data":[]}'
            resp.headers = {"content-type": "application/json"}
            return resp

        fake_client = MagicMock()
        fake_client.get = _fake_get

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_langfuse.retry_client_context",
            lambda *a, **kw: _mock_retry_client(fake_client),
        )

        resp = await client.get(
            "/chat/langfuse/robotsix-mill/traces",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        import base64

        decoded = base64.b64decode(
            captured_headers["authorization"].split(" ", 1)[1]
        ).decode()
        assert decoded == "pk-mill:sk-mill"

    async def test_mill_not_configured_returns_503(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """When mill keys are empty, robotsix-mill returns 503."""
        cfg = server_mod.app.state.config
        cfg.langfuse_projects["robotsix-mill"] = LangfuseProjectCreds(
            public_key="", secret_key=""
        )

        resp = await client.get(
            "/chat/langfuse/robotsix-mill/traces",
            headers=auth_headers,
        )
        assert resp.status_code == 503


class TestLangfuseProxyCogneeProject:
    async def test_proxies_with_cognee_credentials(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """When the project path param is cognee, the proxy uses cognee keys."""
        cfg = server_mod.app.state.config
        cfg.langfuse_projects["cognee"] = LangfuseProjectCreds(
            public_key="pk-cog", secret_key="sk-cog"
        )
        cfg.langfuse_base_url = "https://langfuse.example"

        captured_headers: dict[str, str] = {}

        async def _fake_get(url, headers=None, **kwargs):
            nonlocal captured_headers
            captured_headers = dict(headers) if headers else {}
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.content = b'{"data":[]}'
            resp.headers = {"content-type": "application/json"}
            return resp

        fake_client = MagicMock()
        fake_client.get = _fake_get

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_langfuse.retry_client_context",
            lambda *a, **kw: _mock_retry_client(fake_client),
        )

        resp = await client.get(
            "/chat/langfuse/cognee/traces",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        import base64

        decoded = base64.b64decode(
            captured_headers["authorization"].split(" ", 1)[1]
        ).decode()
        assert decoded == "pk-cog:sk-cog"

    async def test_cognee_not_configured_returns_503(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        """When cognee keys are empty, cognee returns 503."""
        cfg = server_mod.app.state.config
        cfg.langfuse_projects["cognee"] = LangfuseProjectCreds(
            public_key="", secret_key=""
        )

        resp = await client.get(
            "/chat/langfuse/cognee/traces",
            headers=auth_headers,
        )
        assert resp.status_code == 503


class TestLangfuseAutoDiscovery:
    """Auto-discovered projects from components with chat access enabled.

    When a component declares ``langfuse.projects`` in its standardized
    config AND has ``allow_chat_access`` or ``chat_agent_mutatable``
    enabled, reconciliation extracts the project aliases and makes them
    available through the chat proxy's project list.
    """

    async def test_auto_discovered_projects_appear_in_project_list(
        self, client, auth_headers
    ):
        """Project aliases from a chat-accessible component are listed."""
        _seed_component_config(
            "auto-comp",
            {
                "langfuse": {
                    "host": "https://langfuse.example.com",
                    "projects": {
                        "auto-proj-a": {
                            "public_key": "pk-auto-a",
                            "secret_key": "sk-auto-a",
                        },
                        "auto-proj-b": {
                            "public_key": "pk-auto-b",
                            "secret_key": "sk-auto-b",
                        },
                    },
                },
            },
        )
        from robotsix_central_deploy.registry.models import ComponentConfig

        cfg = ComponentConfig(
            id="auto-comp",
            image="auto:latest",
            container_name="auto-comp",
            allow_chat_access=True,
            config_volume="auto-comp-config",
        )
        server_mod.app.state.component_config_store.register(cfg)
        server_mod.app.state.registry.register(cfg)

        # Trigger reconciliation
        from robotsix_central_deploy.lifecycle._langfuse_config import (
            _reconcile_auto_langfuse_projects,
        )

        auto_projects = await _reconcile_auto_langfuse_projects(
            server_mod.app.state.component_config_store,
            server_mod.app.state.backend,
        )
        server_mod.app.state.auto_langfuse_projects = auto_projects

        resp = await client.get("/chat/langfuse/projects", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "auto-proj-a" in data
        assert "auto-proj-b" in data

    async def test_auto_discovered_and_operator_projects_are_merged(
        self, client, auth_headers
    ):
        """Operator-configured projects are merged with auto-discovered ones."""
        _seed_component_config(
            "merge-comp",
            {
                "langfuse": {
                    "host": "https://langfuse.example.com",
                    "projects": {
                        "from-component": {
                            "public_key": "pk-cmp",
                            "secret_key": "sk-cmp",
                        },
                    },
                },
            },
        )
        from robotsix_central_deploy.registry.models import ComponentConfig

        cfg = ComponentConfig(
            id="merge-comp",
            image="merge:latest",
            container_name="merge-comp",
            allow_chat_access=True,
            config_volume="merge-comp-config",
        )
        server_mod.app.state.component_config_store.register(cfg)
        server_mod.app.state.registry.register(cfg)

        # Operator project
        from robotsix_central_deploy.lifecycle.config import LangfuseProjectCreds

        server_mod.app.state.config.langfuse_projects["from-operator"] = (
            LangfuseProjectCreds(public_key="pk-op", secret_key="sk-op")
        )

        # Trigger reconciliation
        from robotsix_central_deploy.lifecycle._langfuse_config import (
            _reconcile_auto_langfuse_projects,
        )

        auto_projects = await _reconcile_auto_langfuse_projects(
            server_mod.app.state.component_config_store,
            server_mod.app.state.backend,
        )
        server_mod.app.state.auto_langfuse_projects = auto_projects

        resp = await client.get("/chat/langfuse/projects", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "from-component" in data
        assert "from-operator" in data


class TestLangfuseProxyErrorHandling:
    async def test_connect_error_returns_502(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """A ConnectError from httpx becomes 502 Bad Gateway."""
        cfg = server_mod.app.state.config
        cfg.langfuse_projects["robotsix-chat"] = LangfuseProjectCreds(
            public_key="pk", secret_key="sk"
        )
        cfg.langfuse_base_url = "https://langfuse.example"

        async def _fake_get(url, headers=None, **kwargs):
            raise httpx.ConnectError("connection refused")

        fake_client = MagicMock()
        fake_client.get = _fake_get

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_langfuse.retry_client_context",
            lambda *a, **kw: _mock_retry_client(fake_client),
        )

        resp = await client.get(
            "/chat/langfuse/robotsix-chat/traces",
            headers=auth_headers,
        )
        assert resp.status_code == 502

    async def test_timeout_returns_504(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """A TimeoutException from httpx becomes 504 Gateway Timeout."""
        cfg = server_mod.app.state.config
        cfg.langfuse_projects["robotsix-chat"] = LangfuseProjectCreds(
            public_key="pk", secret_key="sk"
        )
        cfg.langfuse_base_url = "https://langfuse.example"

        async def _fake_get(url, headers=None, **kwargs):
            raise httpx.TimeoutException("timed out")

        fake_client = MagicMock()
        fake_client.get = _fake_get

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_langfuse.retry_client_context",
            lambda *a, **kw: _mock_retry_client(fake_client),
        )

        resp = await client.get(
            "/chat/langfuse/robotsix-chat/traces",
            headers=auth_headers,
        )
        assert resp.status_code == 504


# ---------------------------------------------------------------------------
# _reconcile_auto_langfuse_projects unit tests
# ---------------------------------------------------------------------------


def _make_config(
    component_id: str,
    *,
    allow_chat_access: bool = False,
    chat_agent_mutatable: bool = False,
) -> ComponentConfig:
    """Build a minimal ComponentConfig for reconciliation tests."""
    return ComponentConfig(
        id=component_id,
        image=f"ghcr.io/example/{component_id}:main",
        container_name=component_id,
        config_volume=f"{component_id}-config",
        allow_chat_access=allow_chat_access,
        chat_agent_mutatable=chat_agent_mutatable,
    )


def _langfuse_config(
    *aliases: str,
    host: str = "https://langfuse.example",
) -> dict:
    """Build a component config dict with a ``langfuse`` block.

    Each alias gets ``pk-{alias}`` / ``sk-{alias}`` keys.
    """
    return {
        "langfuse": {
            "host": host,
            "projects": {
                alias: {"public_key": f"pk-{alias}", "secret_key": f"sk-{alias}"}
                for alias in aliases
            },
        }
    }


class TestReconcileAutoLangfuseProjects:
    """Unit tests for ``_reconcile_auto_langfuse_projects``."""

    @pytest.mark.asyncio
    async def test_toggle_on_allow_chat_access_lists_aliases(self):
        """A component with allow_chat_access=True has its projects listed."""
        comp_store = MagicMock()
        comp_store.all.return_value = [
            _make_config("chat", allow_chat_access=True),
        ]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(
            return_value=_langfuse_config("robotsix-chat")
        )

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert set(result.keys()) == {"robotsix-chat"}
        assert result["robotsix-chat"].public_key == "pk-robotsix-chat"

    @pytest.mark.asyncio
    async def test_toggle_on_chat_agent_mutatable_lists_aliases(self):
        """A component with chat_agent_mutatable=True has its projects listed."""
        comp_store = MagicMock()
        comp_store.all.return_value = [
            _make_config("mill", chat_agent_mutatable=True),
        ]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(
            return_value=_langfuse_config("robotsix-mill")
        )

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert set(result.keys()) == {"robotsix-mill"}

    @pytest.mark.asyncio
    async def test_toggle_off_component_is_skipped(self):
        """A component with neither toggle enabled is entirely skipped."""
        comp_store = MagicMock()
        comp_store.all.return_value = [
            _make_config("hidden"),
        ]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(
            return_value=_langfuse_config("hidden-project")
        )

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert result == {}

    @pytest.mark.asyncio
    async def test_no_config_returns_none_is_skipped(self):
        """A component whose get_current returns None is skipped gracefully."""
        comp_store = MagicMock()
        comp_store.all.return_value = [
            _make_config("unconfigured", allow_chat_access=True),
        ]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(return_value=None)

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert result == {}

    @pytest.mark.asyncio
    async def test_no_langfuse_block_yields_nothing(self):
        """A component with a config dict but no langfuse block adds no entries."""
        comp_store = MagicMock()
        comp_store.all.return_value = [
            _make_config("no-lf", allow_chat_access=True),
        ]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(return_value={"server_port": 8080})

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert result == {}

    @pytest.mark.asyncio
    async def test_multiple_projects_per_component(self):
        """A single component declaring several Langfuse projects exposes all of them."""
        comp_store = MagicMock()
        comp_store.all.return_value = [
            _make_config("chat", allow_chat_access=True),
        ]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(
            return_value=_langfuse_config("robotsix-chat", "robotsix-chat-cognee")
        )

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert set(result.keys()) == {"robotsix-chat", "robotsix-chat-cognee"}

    @pytest.mark.asyncio
    async def test_first_alias_wins_on_collision(self):
        """When two components declare the same alias, the first one wins."""
        comp_store = MagicMock()
        comp_store.all.return_value = [
            _make_config("chat", allow_chat_access=True),
            _make_config("mill", allow_chat_access=True),
        ]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(
            side_effect=[
                _langfuse_config("shared-alias"),  # chat declares "shared-alias"
                _langfuse_config("shared-alias"),  # mill also declares it
            ]
        )
        # Give chat and mill different keys for the same alias so we can
        # verify which one stuck.
        chat_config = _langfuse_config("shared-alias")
        chat_config["langfuse"]["projects"]["shared-alias"]["public_key"] = (
            "pk-from-chat"
        )
        mill_config = _langfuse_config("shared-alias")
        mill_config["langfuse"]["projects"]["shared-alias"]["public_key"] = (
            "pk-from-mill"
        )
        backend.read_config_from_volume = AsyncMock(
            side_effect=[chat_config, mill_config]
        )

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert set(result.keys()) == {"shared-alias"}
        assert result["shared-alias"].public_key == "pk-from-chat"

    @pytest.mark.asyncio
    async def test_multiple_components_unique_aliases(self):
        """Several components each with distinct aliases all contribute."""
        comp_store = MagicMock()
        comp_store.all.return_value = [
            _make_config("chat", allow_chat_access=True),
            _make_config("mill", chat_agent_mutatable=True),
            _make_config("cognee", allow_chat_access=True),
        ]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(
            side_effect=[
                _langfuse_config("robotsix-chat"),
                _langfuse_config("robotsix-mill"),
                _langfuse_config("robotsix-cognee"),
            ]
        )

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert set(result.keys()) == {
            "robotsix-chat",
            "robotsix-mill",
            "robotsix-cognee",
        }

    @pytest.mark.asyncio
    async def test_mixed_toggles_only_enabled_contribute(self):
        """Only components with at least one toggle enabled are scanned."""
        comp_store = MagicMock()
        comp_store.all.return_value = [
            _make_config("enabled", allow_chat_access=True),
            _make_config("disabled"),
            _make_config("also-disabled"),
        ]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(
            side_effect=[
                _langfuse_config("proj-enabled"),
                _langfuse_config("proj-disabled"),
                _langfuse_config("proj-also-disabled"),
            ]
        )

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert set(result.keys()) == {"proj-enabled"}

    @pytest.mark.asyncio
    async def test_idempotent_same_input_same_output(self):
        """Repeated calls with the same stores produce the same result."""
        comp_store = MagicMock()
        comp_store.all.return_value = [
            _make_config("chat", allow_chat_access=True),
        ]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(
            return_value=_langfuse_config("robotsix-chat")
        )

        result1 = await _reconcile_auto_langfuse_projects(comp_store, backend)
        result2 = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert result1 == result2
        assert set(result1.keys()) == {"robotsix-chat"}

    @pytest.mark.asyncio
    async def test_empty_component_list_returns_empty(self):
        """When no components are registered the result is empty."""
        comp_store = MagicMock()
        comp_store.all.return_value = []
        backend = MagicMock()

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert result == {}

    @pytest.mark.asyncio
    async def test_half_filled_projects_are_dropped(self):
        """Projects missing either public_key or secret_key are silently dropped."""
        comp_store = MagicMock()
        comp_store.all.return_value = [
            _make_config("chat", allow_chat_access=True),
        ]
        config = {
            "langfuse": {
                "host": "https://lf",
                "projects": {
                    "good": {"public_key": "pk", "secret_key": "sk"},
                    "no-secret": {"public_key": "pk", "secret_key": ""},
                },
            }
        }
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(return_value=config)

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert set(result.keys()) == {"good"}

    @pytest.mark.asyncio
    async def test_components_processed_in_registration_order(self):
        """Components are iterated in the order .all() returns them."""
        comp_store = MagicMock()
        comp_store.all.return_value = [
            _make_config("first", allow_chat_access=True),
            _make_config("second", allow_chat_access=True),
        ]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(
            side_effect=[
                _langfuse_config("alpha"),
                _langfuse_config("beta"),
            ]
        )

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        # Both are unique so both appear; ordering is dict-insertion order.
        assert list(result.keys()) == ["alpha", "beta"]


class TestDiscoveryReadsTheComponentNotAMirror:
    """Discovery must read the component's own config file.

    On 2026-08-07 chat's Langfuse credentials were repaired in its config
    volume and fleet-wide discovery still reported them missing, because it
    read central-deploy's stored copy — which held a stale pre-migration
    block. Nothing errored; the credentials simply appeared absent to
    everything downstream and cost-monitor stopped seeing chat.
    """

    @pytest.mark.asyncio
    async def test_reads_the_components_config_volume(self):
        comp_store = MagicMock()
        comp_store.all.return_value = [_make_config("chat", allow_chat_access=True)]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(
            return_value=_langfuse_config("robotsix-chat")
        )

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        backend.read_config_from_volume.assert_awaited_once_with("chat-config")
        assert set(result) == {"robotsix-chat"}

    @pytest.mark.asyncio
    async def test_component_without_a_volume_is_skipped(self):
        """Nothing to read means no projects, not an error."""
        cfg = _make_config("virtual", allow_chat_access=True)
        cfg.config_volume = None
        comp_store = MagicMock()
        comp_store.all.return_value = [cfg]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock()

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert result == {}
        backend.read_config_from_volume.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_unreadable_volume_does_not_hide_the_rest(self):
        comp_store = MagicMock()
        comp_store.all.return_value = [
            _make_config("broken", allow_chat_access=True),
            _make_config("mill", allow_chat_access=True),
        ]
        backend = MagicMock()
        backend.read_config_from_volume = AsyncMock(
            side_effect=[OSError("volume gone"), _langfuse_config("robotsix-mill")]
        )

        result = await _reconcile_auto_langfuse_projects(comp_store, backend)

        assert set(result) == {"robotsix-mill"}
