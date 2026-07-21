"""Integration tests for the chat-agent Langfuse proxy endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
from httpx import AsyncClient

import robotsix_central_deploy.lifecycle.app as server_mod


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
        server_mod.app.state.config.langfuse_chat_public_key = "pk-chat"
        server_mod.app.state.config.langfuse_chat_secret_key = "sk-chat"
        server_mod.app.state.config.langfuse_mill_public_key = "pk-mill"
        server_mod.app.state.config.langfuse_mill_secret_key = "sk-mill"
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
        server_mod.app.state.config.langfuse_chat_public_key = ""
        server_mod.app.state.config.langfuse_chat_secret_key = ""

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
        server_mod.app.state.config.langfuse_chat_public_key = "pk-chat"
        server_mod.app.state.config.langfuse_chat_secret_key = "sk-chat"
        server_mod.app.state.config.langfuse_base_url = "https://langfuse.example"

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
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(
            "httpx.AsyncClient",
            lambda *a, **kw: fake_client,
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
        server_mod.app.state.config.langfuse_chat_public_key = "pk"
        server_mod.app.state.config.langfuse_chat_secret_key = "sk"
        server_mod.app.state.config.langfuse_base_url = "https://langfuse.example"

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
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: fake_client)

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
        server_mod.app.state.config.langfuse_chat_public_key = "pk-chat"
        server_mod.app.state.config.langfuse_chat_secret_key = "sk-chat"
        server_mod.app.state.config.langfuse_base_url = "https://langfuse.example"

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
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: fake_client)

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
        server_mod.app.state.config.langfuse_chat_public_key = "pk-chat"
        server_mod.app.state.config.langfuse_chat_secret_key = "sk-chat"
        server_mod.app.state.config.langfuse_base_url = "https://langfuse.example"

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
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: fake_client)

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
        server_mod.app.state.config.langfuse_chat_public_key = "pk-chat"
        server_mod.app.state.config.langfuse_chat_secret_key = "sk-chat"
        server_mod.app.state.config.langfuse_base_url = "https://langfuse.example"

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
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: fake_client)

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
        server_mod.app.state.config.langfuse_mill_public_key = "pk-mill"
        server_mod.app.state.config.langfuse_mill_secret_key = "sk-mill"
        server_mod.app.state.config.langfuse_base_url = "https://langfuse.example"

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
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: fake_client)

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
        server_mod.app.state.config.langfuse_mill_public_key = ""
        server_mod.app.state.config.langfuse_mill_secret_key = ""

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
        server_mod.app.state.config.langfuse_cognee_public_key = "pk-cog"
        server_mod.app.state.config.langfuse_cognee_secret_key = "sk-cog"
        server_mod.app.state.config.langfuse_base_url = "https://langfuse.example"

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
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: fake_client)

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
        server_mod.app.state.config.langfuse_cognee_public_key = ""
        server_mod.app.state.config.langfuse_cognee_secret_key = ""

        resp = await client.get(
            "/chat/langfuse/cognee/traces",
            headers=auth_headers,
        )
        assert resp.status_code == 503


class TestLangfuseProxyErrorHandling:
    async def test_connect_error_returns_502(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """A ConnectError from httpx becomes 502 Bad Gateway."""
        server_mod.app.state.config.langfuse_chat_public_key = "pk"
        server_mod.app.state.config.langfuse_chat_secret_key = "sk"
        server_mod.app.state.config.langfuse_base_url = "https://langfuse.example"

        async def _fake_get(url, headers=None, **kwargs):
            raise httpx.ConnectError("connection refused")

        fake_client = MagicMock()
        fake_client.get = _fake_get
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: fake_client)

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
        server_mod.app.state.config.langfuse_chat_public_key = "pk"
        server_mod.app.state.config.langfuse_chat_secret_key = "sk"
        server_mod.app.state.config.langfuse_base_url = "https://langfuse.example"

        async def _fake_get(url, headers=None, **kwargs):
            raise httpx.TimeoutException("timed out")

        fake_client = MagicMock()
        fake_client.get = _fake_get
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: fake_client)

        resp = await client.get(
            "/chat/langfuse/robotsix-chat/traces",
            headers=auth_headers,
        )
        assert resp.status_code == 504
