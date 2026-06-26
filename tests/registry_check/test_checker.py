"""Tests for the RegistryChecker."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from robotsix_central_deploy.registry_check.checker import RegistryChecker


class TestRegistryChecker:
    @pytest.fixture
    def mock_client(self):
        return AsyncMock(spec=httpx.AsyncClient)

    def _make_checker(self, mock_client, **kw):
        return RegistryChecker(mock_client, **kw)

    async def test_returns_digest_from_header(self, mock_client):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "test-token"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:abc123"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = self._make_checker(mock_client)
        result = await checker.get_latest_digest("ghcr.io/owner/image:main")
        assert result == "sha256:abc123"

    async def test_returns_none_on_network_error(self, mock_client):
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))
        checker = self._make_checker(mock_client)
        result = await checker.get_latest_digest("ghcr.io/owner/image:main")
        assert result is None

    async def test_returns_none_on_non_2xx(self, mock_client):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "tok"}
        manifest_resp = MagicMock(status_code=503)
        manifest_resp.headers = {}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = self._make_checker(mock_client)
        result = await checker.get_latest_digest("ghcr.io/owner/image:main")
        assert result is None

    async def test_cache_hit_no_second_request(self, mock_client):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "tok"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:abc"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = self._make_checker(mock_client, ttl_seconds=300)
        await checker.get_latest_digest("ghcr.io/owner/image:main")
        await checker.get_latest_digest("ghcr.io/owner/image:main")
        assert mock_client.head.call_count == 1

    async def test_cache_miss_after_ttl(self, mock_client):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "tok"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:abc"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = self._make_checker(mock_client, ttl_seconds=0)
        await checker.get_latest_digest("ghcr.io/owner/image:main")
        await checker.get_latest_digest("ghcr.io/owner/image:main")
        assert mock_client.head.call_count == 2

    async def test_returns_none_for_non_ghcr_registry(self, mock_client):
        checker = self._make_checker(mock_client)
        result = await checker.get_latest_digest("docker.io/library/nginx:latest")
        assert result is None
        mock_client.head.assert_not_called()

    async def test_uses_configured_token_when_anonymous_fails(self, mock_client):
        token_resp = MagicMock(status_code=401)
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:xyz"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = self._make_checker(mock_client, ghcr_token="my-pat")
        result = await checker.get_latest_digest("ghcr.io/owner/image:main")
        assert result == "sha256:xyz"
        call_headers = mock_client.head.call_args[1].get("headers", {})
        assert call_headers.get("Authorization") == "Bearer my-pat"
