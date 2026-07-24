"""Tests for the RegistryChecker."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from robotsix_http import RetryClient

from robotsix_central_deploy.registry_check.checker import RegistryChecker


class TestRegistryChecker:
    @pytest.fixture
    def mock_client(self):
        return AsyncMock(spec=RetryClient)

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

    async def test_returns_none_for_unsupported_registry(self, mock_client):
        checker = self._make_checker(mock_client)
        result = await checker.get_latest_digest("quay.io/org/image:latest")
        assert result is None

    async def test_dockerhub_implicit_ref(self, mock_client):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "dh-token"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:abc123"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = self._make_checker(mock_client)
        result = await checker.get_latest_digest("robotsix/mill:latest")
        assert result == "sha256:abc123"
        # check token call
        get_url = mock_client.get.call_args[0][0]
        assert get_url.startswith("https://auth.docker.io/token")
        assert "scope=repository:robotsix/mill:pull" in get_url
        # check manifest call
        head_url = mock_client.head.call_args[0][0]
        assert (
            head_url == "https://registry-1.docker.io/v2/robotsix/mill/manifests/latest"
        )
        # no ghcr.io references
        assert "ghcr.io" not in get_url
        assert "ghcr.io" not in head_url

    async def test_dockerhub_implicit_ref_no_tag(self, mock_client):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "dh-token"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:abc123"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = self._make_checker(mock_client)
        result = await checker.get_latest_digest("robotsix/mill")
        assert result == "sha256:abc123"
        head_url = mock_client.head.call_args[0][0]
        assert head_url.endswith("/manifests/latest")

    async def test_dockerhub_explicit_docker_io_ref(self, mock_client):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "dh-token"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:abc123"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = self._make_checker(mock_client)
        result = await checker.get_latest_digest("docker.io/robotsix/mill:latest")
        assert result == "sha256:abc123"
        get_url = mock_client.get.call_args[0][0]
        assert "scope=repository:robotsix/mill:pull" in get_url
        head_url = mock_client.head.call_args[0][0]
        assert (
            head_url == "https://registry-1.docker.io/v2/robotsix/mill/manifests/latest"
        )

    async def test_dockerhub_library_shorthand(self, mock_client):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "dh-token"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:abc123"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = self._make_checker(mock_client)
        result = await checker.get_latest_digest("nginx:latest")
        assert result == "sha256:abc123"
        get_url = mock_client.get.call_args[0][0]
        assert "scope=repository:library/nginx:pull" in get_url
        head_url = mock_client.head.call_args[0][0]
        assert (
            head_url == "https://registry-1.docker.io/v2/library/nginx/manifests/latest"
        )

    async def test_accept_header_contains_oci_manifest_type(self, mock_client):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "tok"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:abc"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = self._make_checker(mock_client)
        await checker.get_latest_digest("ghcr.io/owner/image:main")
        call_headers = mock_client.head.call_args[1].get("headers", {})
        accept = call_headers.get("Accept", "")
        assert "application/vnd.oci.image.manifest.v1+json" in accept
        assert "application/vnd.docker.distribution.manifest.v1+json" not in accept
