"""Integration tests for the chat-agent Langfuse proxy endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
from httpx import AsyncClient

from robotsix_central_deploy.lifecycle import server as server_mod


class TestLangfuseProxyAuth:
    async def test_unauthorized_returns_401(self, client: AsyncClient):
        resp = await client.get(
            "/chat/langfuse/api/public/traces",
        )
        assert resp.status_code == 401


class TestLangfuseProxyNotConfigured:
    async def test_503_when_no_credentials_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        """When no Langfuse keys are set, the proxy returns 503."""
        server_mod.app.state.config.langfuse_chat_public_key = ""
        server_mod.app.state.config.langfuse_chat_secret_key = ""

        resp = await client.get(
            "/chat/langfuse/api/public/traces",
            headers=auth_headers,
        )
        assert resp.status_code == 503
        assert "not configured" in resp.text


class TestLangfuseProxyChatProject:
    async def test_proxies_traces_with_injected_basic_auth(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """GET /chat/langfuse/api/public/traces forwards to Langfuse with
        Basic Auth injected from the robotsix-chat project keys."""
        server_mod.app.state.config.langfuse_chat_public_key = "pk-chat"
        server_mod.app.state.config.langfuse_chat_secret_key = "sk-chat"
        server_mod.app.state.config.langfuse_base_url = "https://langfuse.example"

        # Capture the request the proxy makes to Langfuse.
        captured_headers: dict[str, str] = {}
        captured_url: str = ""

        async def _fake_get(url, headers=None, **kwargs):
            nonlocal captured_url, captured_headers
            captured_url = url
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
            "/chat/langfuse/api/public/traces?limit=5&page=1",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == {"data": []}

        # Verify the upstream URL and auth header.
        assert captured_url == (
            "https://langfuse.example/api/public/traces?limit=5&page=1"
        )
        assert "authorization" in captured_headers
        assert captured_headers["authorization"].startswith("Basic ")

        # Verify it's the chat project credentials.
        import base64

        decoded = base64.b64decode(
            captured_headers["authorization"].split(" ", 1)[1]
        ).decode()
        assert decoded == "pk-chat:sk-chat"

    async def test_proxies_single_trace(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """GET /chat/langfuse/api/public/traces/{traceId} works."""
        server_mod.app.state.config.langfuse_chat_public_key = "pk-chat"
        server_mod.app.state.config.langfuse_chat_secret_key = "sk-chat"
        server_mod.app.state.config.langfuse_base_url = "https://langfuse.example"

        captured_url: str = ""

        async def _fake_get(url, headers=None, **kwargs):
            nonlocal captured_url
            captured_url = url
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
            "/chat/langfuse/api/public/traces/abc123",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert captured_url == ("https://langfuse.example/api/public/traces/abc123")

    async def test_proxies_observations(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """GET /chat/langfuse/api/public/observations works."""
        server_mod.app.state.config.langfuse_chat_public_key = "pk-chat"
        server_mod.app.state.config.langfuse_chat_secret_key = "sk-chat"
        server_mod.app.state.config.langfuse_base_url = "https://langfuse.example"

        captured_url: str = ""

        async def _fake_get(url, headers=None, **kwargs):
            nonlocal captured_url
            captured_url = url
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
            "/chat/langfuse/api/public/observations",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert captured_url == ("https://langfuse.example/api/public/observations")


class TestLangfuseProxyCogneeProject:
    async def test_proxies_with_cognee_credentials(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """When ?project=cognee, the proxy uses cognee keys."""
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
            "/chat/langfuse/api/public/traces?project=cognee",
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
        """When cognee keys are empty, ?project=cognee returns 503."""
        server_mod.app.state.config.langfuse_cognee_public_key = ""
        server_mod.app.state.config.langfuse_cognee_secret_key = ""

        resp = await client.get(
            "/chat/langfuse/api/public/traces?project=cognee",
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

        async def _fake_get(url, headers=None, **kwargs):
            raise httpx.ConnectError("connection refused")

        fake_client = MagicMock()
        fake_client.get = _fake_get
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: fake_client)

        resp = await client.get(
            "/chat/langfuse/api/public/traces",
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

        async def _fake_get(url, headers=None, **kwargs):
            raise httpx.TimeoutException("timed out")

        fake_client = MagicMock()
        fake_client.get = _fake_get
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: fake_client)

        resp = await client.get(
            "/chat/langfuse/api/public/traces",
            headers=auth_headers,
        )
        assert resp.status_code == 504


class TestLangfuseProxyQueryParamStripping:
    async def test_project_param_not_forwarded(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
    ):
        """The ?project= query parameter is consumed by the proxy and
        NOT forwarded to Langfuse."""
        server_mod.app.state.config.langfuse_chat_public_key = "pk"
        server_mod.app.state.config.langfuse_chat_secret_key = "sk"
        server_mod.app.state.config.langfuse_base_url = "https://langfuse.example"

        captured_url: str = ""

        async def _fake_get(url, headers=None, **kwargs):
            nonlocal captured_url
            captured_url = url
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
            "/chat/langfuse/api/public/traces?project=robotsix-chat&limit=10&tags=prod",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert "project=" not in captured_url
        assert "limit=10" in captured_url
        assert "tags=prod" in captured_url
