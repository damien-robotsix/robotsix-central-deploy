"""Tests for the GET /chat/components endpoint.

Covers Docker-component probing, external-component handling, credential
management, and the server-side proxy for external components.

The tests use pytest fixtures when available and fall back to a manual
setup path when pytest is not installed (e.g. in minimal containers).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient

from robotsix_central_deploy.lifecycle import server as server_mod
from robotsix_central_deploy.registry.env_store import EnvStore
from robotsix_central_deploy.registry.models import ComponentConfig

try:
    import pytest
except ImportError:  # pragma: no cover — pytest not available in all environments
    pytest = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helper: seed a component via the server module's store + registry.
# ---------------------------------------------------------------------------


async def _seed_component(cfg: ComponentConfig) -> None:
    """Put *cfg* into the running app's component config store and registry."""
    store = server_mod.app.state.component_config_store
    await store.put(cfg)
    server_mod.app.state.registry.register(cfg)


# ---------------------------------------------------------------------------
# Pytest helpers — no-ops when pytest is absent.
# ---------------------------------------------------------------------------


def _fixture_autouse(fn):
    """pytest.fixture(autouse=True) when pytest is available, else no-op."""
    if pytest is not None:
        return pytest.fixture(autouse=True)(fn)
    return fn


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestChatComponentsAuth:
    """Authentication tests for GET /chat/components."""

    async def test_endpoint_requires_auth(self, client: AsyncClient) -> None:
        """The endpoint returns 401 when no credentials are supplied."""
        resp = await client.get("/chat/components")
        assert resp.status_code == 401


class TestChatComponentsDocker:
    """Docker-component roster behaviour."""

    @_fixture_autouse
    async def _seed_component_fxt(self, client: AsyncClient) -> None:
        """Register a Docker component with chat access enabled."""
        await _seed_component(
            ComponentConfig(
                id="test-svc",
                image="test:latest",
                container_name="test-svc",
                ports=[{"host": 8080, "container": 8080}],
                allow_chat_access=True,
            )
        )

    async def test_docker_component_without_ports_omitted(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """A Docker component with no ports is silently omitted."""
        await _seed_component(
            ComponentConfig(
                id="no-ports",
                image="test:latest",
                container_name="no-ports",
                ports=[],
                allow_chat_access=True,
            )
        )

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200

        ids = [c["id"] for c in resp.json()]
        assert "no-ports" not in ids


class TestChatComponentsExternal:
    """External (non-Docker) component roster behaviour."""

    @_fixture_autouse
    async def _seed_external(self, client: AsyncClient) -> None:
        """Register an external component with a skill body."""
        await _seed_component(
            ComponentConfig(
                id="langfuse",
                image="",
                container_name="",
                allow_chat_access=True,
                external_url="https://langfuse.robotsix.net",
                external_chat_skill="# Langfuse Skill\n\nRead-only traces API.",
            )
        )

    async def test_external_component_with_skill_appears(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """An external component with a non-empty skill body is included."""
        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200

        components = resp.json()
        assert isinstance(components, list)

        langfuse = next((c for c in components if c["id"] == "langfuse"), None)
        assert langfuse is not None, f"langfuse not in {components}"
        assert langfuse["base_url"] == "https://langfuse.robotsix.net"
        assert "# Langfuse Skill" in langfuse["skill"]
        assert "Read-only traces API" in langfuse["skill"]

    async def test_external_component_without_skill_omitted(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """An external component with an empty skill body is omitted."""
        await _seed_component(
            ComponentConfig(
                id="no-skill-ext",
                image="",
                container_name="",
                allow_chat_access=True,
                external_url="https://example.com",
                external_chat_skill="",
            )
        )

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200

        ids = [c["id"] for c in resp.json()]
        assert "no-skill-ext" not in ids

    async def test_component_without_allow_chat_access_omitted(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """A component with allow_chat_access=False is omitted regardless of type."""
        await _seed_component(
            ComponentConfig(
                id="no-chat",
                image="",
                container_name="",
                allow_chat_access=False,
                external_url="https://example.com",
                external_chat_skill="has skill but disallowed",
            )
        )

        resp = await client.get("/chat/components", headers=auth_headers)
        assert resp.status_code == 200

        ids = [c["id"] for c in resp.json()]
        assert "no-chat" not in ids


# ---------------------------------------------------------------------------
# Credential management tests
# ---------------------------------------------------------------------------


class TestChatCredentials:
    """Tests for PUT/GET /chat/credentials/{component_id}."""

    @_fixture_autouse
    async def _seed_external(self, client: AsyncClient) -> None:
        """Register an external langfuse component."""
        await _seed_component(
            ComponentConfig(
                id="langfuse",
                image="",
                container_name="",
                allow_chat_access=True,
                external_url="https://langfuse.robotsix.net",
                external_chat_skill="# Langfuse Skill",
            )
        )

    async def test_put_then_get_credentials(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Storing credentials and reading them back returns masked secrets."""
        # Store credentials for the "chat" project.
        put_resp = await client.put(
            "/chat/credentials/langfuse",
            json={
                "project": "chat",
                "public_key": "pk-chat-123",
                "secret_key": "sk-chat-secret",
            },
            headers=auth_headers,
        )
        assert put_resp.status_code == 200
        body = put_resp.json()
        assert body["component_id"] == "langfuse"
        assert "chat" in body["projects"]
        assert body["projects"]["chat"]["public_key"] == "pk-chat-123"
        assert body["projects"]["chat"]["secret_key"] == "***"

        # Store credentials for the "cognee" project too.
        put_resp2 = await client.put(
            "/chat/credentials/langfuse",
            json={
                "project": "cognee",
                "public_key": "pk-cognee-456",
                "secret_key": "sk-cognee-secret",
            },
            headers=auth_headers,
        )
        assert put_resp2.status_code == 200
        body2 = put_resp2.json()
        assert "chat" in body2["projects"]
        assert "cognee" in body2["projects"]
        assert body2["projects"]["cognee"]["public_key"] == "pk-cognee-456"
        assert body2["projects"]["cognee"]["secret_key"] == "***"

        # GET should return both projects with masked secrets.
        get_resp = await client.get("/chat/credentials/langfuse", headers=auth_headers)
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["component_id"] == "langfuse"
        assert set(get_body["projects"].keys()) == {"chat", "cognee"}
        assert get_body["projects"]["chat"]["secret_key"] == "***"
        assert get_body["projects"]["cognee"]["secret_key"] == "***"

    async def test_put_credentials_requires_external_component(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """PUT returns 404 for a non-existent or non-external component."""
        resp = await client.put(
            "/chat/credentials/nonexistent",
            json={
                "project": "chat",
                "public_key": "pk",
                "secret_key": "sk",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_put_credentials_requires_chat_access(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """PUT returns 403 when allow_chat_access is False."""
        await _seed_component(
            ComponentConfig(
                id="no-chat-ext",
                image="",
                container_name="",
                allow_chat_access=False,
                external_url="https://example.com",
            )
        )
        resp = await client.put(
            "/chat/credentials/no-chat-ext",
            json={
                "project": "chat",
                "public_key": "pk",
                "secret_key": "sk",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 403

    async def test_get_credentials_requires_auth(self, client: AsyncClient) -> None:
        """GET /chat/credentials requires authentication."""
        resp = await client.get("/chat/credentials/langfuse")
        assert resp.status_code == 401

    async def test_put_credentials_requires_auth(self, client: AsyncClient) -> None:
        """PUT /chat/credentials requires authentication."""
        resp = await client.put(
            "/chat/credentials/langfuse",
            json={
                "project": "chat",
                "public_key": "pk",
                "secret_key": "sk",
            },
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Proxy tests
# ---------------------------------------------------------------------------


class TestChatProxy:
    """Tests for GET /chat/proxy/{component_id}/{path}."""

    @_fixture_autouse
    async def _seed_external(self, client: AsyncClient) -> None:
        """Register an external langfuse component and store credentials."""
        await _seed_component(
            ComponentConfig(
                id="langfuse",
                image="",
                container_name="",
                allow_chat_access=True,
                external_url="https://langfuse.robotsix.net",
                external_chat_skill="# Langfuse Skill",
            )
        )
        # Pre-seed credentials so the proxy can find them.
        env_store: EnvStore = server_mod.app.state.env_store
        await env_store.upsert(
            "langfuse",
            env={"LANGFUSE_CHAT_PUBLIC_KEY": "pk-chat"},
            secrets={"LANGFUSE_CHAT_SECRET_KEY": "sk-chat-secret"},
        )

    async def test_proxy_requires_auth(self, client: AsyncClient) -> None:
        """The proxy endpoint returns 401 without auth."""
        resp = await client.get("/chat/proxy/langfuse/api/public/traces?project=chat")
        assert resp.status_code == 401

    async def test_proxy_requires_external_component(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """The proxy returns 404 for a non-existent component."""
        resp = await client.get(
            "/chat/proxy/nonexistent/api/public/traces",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_proxy_no_credentials_returns_502(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """The proxy returns 502 when no credentials exist for the project."""
        resp = await client.get(
            "/chat/proxy/langfuse/api/public/traces?project=cognee",
            headers=auth_headers,
        )
        assert resp.status_code == 502

    async def test_proxy_forwards_with_auth(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """The proxy forwards to upstream with Basic auth injected."""
        mock_upstream_resp = MagicMock()
        mock_upstream_resp.content = json.dumps({"data": []}).encode()
        mock_upstream_resp.status_code = 200
        mock_upstream_resp.headers = {"Content-Type": "application/json"}

        mock_http_client = MagicMock()
        mock_http_client.get = AsyncMock(return_value=mock_upstream_resp)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_http_client):
            resp = await client.get(
                "/chat/proxy/langfuse/api/public/traces?project=chat&page=1&limit=10",
                headers=auth_headers,
            )

        assert resp.status_code == 200
        assert resp.json() == {"data": []}

        # Verify the upstream call included Basic auth.
        mock_http_client.get.assert_awaited_once()
        call_kwargs = mock_http_client.get.call_args.kwargs
        assert call_kwargs["auth"] == ("pk-chat", "sk-chat-secret")
        assert "api/public/traces" in str(mock_http_client.get.call_args.args)

    async def test_proxy_strips_project_param(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """The 'project' query param is consumed by the proxy, not forwarded."""
        mock_upstream_resp = MagicMock()
        mock_upstream_resp.content = b"{}"
        mock_upstream_resp.status_code = 200
        mock_upstream_resp.headers = {"Content-Type": "application/json"}

        mock_http_client = MagicMock()
        mock_http_client.get = AsyncMock(return_value=mock_upstream_resp)
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_http_client):
            resp = await client.get(
                "/chat/proxy/langfuse/api/public/traces?project=chat&page=1&limit=5",
                headers=auth_headers,
            )

        assert resp.status_code == 200

        # 'project' must not reach the upstream — neither in the URL nor in
        # the forwarded query params (passed via httpx's params kwarg).
        call_args = mock_http_client.get.call_args
        url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
        assert "project" not in url
        params = call_args.kwargs.get("params") or {}
        assert "project" not in params
        assert params.get("page") == "1"
        assert params.get("limit") == "5"

    async def test_proxy_requires_chat_access(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """The proxy returns 403 when allow_chat_access is False."""
        await _seed_component(
            ComponentConfig(
                id="no-chat-proxy",
                image="",
                container_name="",
                allow_chat_access=False,
                external_url="https://example.com",
            )
        )
        resp = await client.get(
            "/chat/proxy/no-chat-proxy/api/test",
            headers=auth_headers,
        )
        assert resp.status_code == 403
