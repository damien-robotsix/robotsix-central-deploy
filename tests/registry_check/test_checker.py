"""Tests for the RegistryChecker."""

import base64
import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from robotsix_http import ExternalHTTPError, RetryClient

from robotsix_central_deploy._ghcr_auth import GhcrCredentials
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


class TestRegistryCheckerGhcrAuth:
    """The update check must present the same credential as the image pull.

    Anonymously, a private GHCR package 401s at the token exchange and its
    dashboard status stays "unknown" forever — while pulls of the very same
    image succeed, because only the pull path authenticated.
    """

    @pytest.fixture
    def mock_client(self):
        return AsyncMock(spec=RetryClient)

    @staticmethod
    def _resolver(*creds):
        """Resolver yielding *creds* in preference order (None → anonymous)."""
        candidates = [c for c in creds if c is not None]
        resolver = MagicMock()
        resolver.resolve_all = AsyncMock(return_value=candidates)
        return resolver

    @staticmethod
    def _auth_error(status):
        """What RetryClient actually raises on a 4xx — it never returns one."""
        return ExternalHTTPError(
            f"HTTP {status}", status_code=status, response=MagicMock(status_code=status)
        )

    @staticmethod
    def _auth_header(mock_client) -> str:
        return mock_client.get.call_args[1]["headers"]["Authorization"]

    async def test_private_package_with_credential_resolves_a_digest(self, mock_client):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "scoped-token"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:private"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = RegistryChecker(
            mock_client,
            ghcr_credentials=self._resolver(GhcrCredentials("robot", "ghp_secret")),
        )

        result = await checker.get_latest_digest("ghcr.io/owner/private:main")

        assert result == "sha256:private"
        scheme, _, value = self._auth_header(mock_client).partition(" ")
        assert scheme == "Basic"
        assert base64.b64decode(value).decode() == "robot:ghp_secret"

    async def test_no_credential_falls_back_to_an_anonymous_exchange(self, mock_client):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "anon-token"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:public"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = RegistryChecker(mock_client, ghcr_credentials=self._resolver(None))

        result = await checker.get_latest_digest("ghcr.io/owner/public:main")

        assert result == "sha256:public"
        assert "Authorization" not in mock_client.get.call_args[1]["headers"]

    async def test_unauthorized_token_exchange_logs_and_reports_unknown(
        self, mock_client, caplog
    ):
        token_resp = MagicMock(status_code=401)
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock()
        checker = RegistryChecker(mock_client)

        with caplog.at_level(logging.WARNING):
            result = await checker.get_latest_digest("ghcr.io/owner/private:main")

        assert result is None
        assert "registry auth failed" in caplog.text
        assert "read:packages" in caplog.text, "the log must say how to fix it"

    async def test_unauthorized_manifest_head_is_logged(self, mock_client, caplog):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "tok"}
        manifest_resp = MagicMock(status_code=403, headers={})
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = RegistryChecker(mock_client)

        with caplog.at_level(logging.WARNING):
            result = await checker.get_latest_digest("ghcr.io/owner/private:main")

        assert result is None
        assert "registry auth failed" in caplog.text

    async def test_credential_resolution_failure_degrades_to_anonymous(
        self, mock_client, caplog
    ):
        """A broken GitHub App must not take the whole update check down."""
        resolver = MagicMock()
        resolver.resolve_all = AsyncMock(side_effect=RuntimeError("mint failed"))
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "anon-token"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:pub"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = RegistryChecker(mock_client, ghcr_credentials=resolver)

        with caplog.at_level(logging.WARNING):
            result = await checker.get_latest_digest("ghcr.io/owner/image:main")

        assert result == "sha256:pub"
        assert "registry auth failed" in caplog.text

    async def test_dockerhub_is_never_sent_the_ghcr_credential(self, mock_client):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "dh-token"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:abc"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = RegistryChecker(
            mock_client,
            ghcr_credentials=self._resolver(GhcrCredentials("robot", "ghp_secret")),
        )

        await checker.get_latest_digest("docker.io/robotsix/mill:latest")

        assert "headers" not in mock_client.get.call_args[1]

    async def test_raised_auth_error_on_token_exchange_is_logged(
        self, mock_client, caplog
    ):
        """RetryClient *raises* on 4xx — the returned-response path is a mock
        artefact.  A revoked PAT went unnoticed for 15 days because the only
        coverage used the shape the real client never produces."""
        mock_client.get = AsyncMock(side_effect=self._auth_error(403))
        mock_client.head = AsyncMock()
        checker = RegistryChecker(
            mock_client,
            ghcr_credentials=self._resolver(GhcrCredentials("robot", "dead")),
        )

        with caplog.at_level(logging.WARNING):
            result = await checker.get_latest_digest("ghcr.io/owner/private:main")

        assert result is None
        assert "registry auth failed" in caplog.text
        assert "ghcr_pull_token" in caplog.text, "the log must say how to fix it"

    async def test_raised_auth_error_on_manifest_head_is_logged(
        self, mock_client, caplog
    ):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "tok"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(side_effect=self._auth_error(401))
        checker = RegistryChecker(mock_client)

        with caplog.at_level(logging.WARNING):
            result = await checker.get_latest_digest("ghcr.io/owner/private:main")

        assert result is None
        assert "registry auth failed" in caplog.text

    async def test_rejected_credential_falls_through_to_the_next_one(self, mock_client):
        """A stale PAT must not shadow a working GitHub App credential."""
        dead = GhcrCredentials("robot", "revoked-pat")
        good = GhcrCredentials("x-access-token", "app-token")
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "scoped"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:private"}
        mock_client.get = AsyncMock(side_effect=[self._auth_error(403), token_resp])
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = RegistryChecker(
            mock_client, ghcr_credentials=self._resolver(dead, good)
        )

        result = await checker.get_latest_digest("ghcr.io/owner/private:main")

        assert result == "sha256:private", "must retry with the App credential"
        second = mock_client.get.call_args_list[1][1]["headers"]["Authorization"]
        assert base64.b64decode(second.split()[1]).decode() == (
            "x-access-token:app-token"
        )

    async def test_all_credentials_rejected_falls_back_to_anonymous(self, mock_client):
        """A public package must still resolve when every credential is dead."""
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "anon"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": "sha256:public"}
        mock_client.get = AsyncMock(side_effect=[self._auth_error(403), token_resp])
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = RegistryChecker(
            mock_client,
            ghcr_credentials=self._resolver(GhcrCredentials("robot", "revoked")),
        )

        result = await checker.get_latest_digest("ghcr.io/owner/public:main")

        assert result == "sha256:public"
        assert "Authorization" not in mock_client.get.call_args_list[1][1]["headers"]


class TestRegistryCheckerAuthErrorTracking:
    """Tests for the ``was_auth_error`` tracking method."""

    @pytest.fixture
    def mock_client(self):
        return AsyncMock(spec=RetryClient)

    def _setup_checker(self, mock_client, checker_fn=None):
        """Return a fresh RegistryChecker with an optional credentials resolver."""

    def _token_and_manifest(self, mock_client, digest="sha256:ok"):
        """Set up a normal token exchange + manifest response sequence."""
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "tok"}
        manifest_resp = MagicMock(status_code=200)
        manifest_resp.headers = {"Docker-Content-Digest": digest}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(return_value=manifest_resp)

    async def test_was_auth_error_false_after_successful_fetch(self, mock_client):
        """After a successful fetch, was_auth_error returns False."""
        self._token_and_manifest(mock_client)
        checker = RegistryChecker(mock_client)

        await checker.get_latest_digest("ghcr.io/owner/image:main")

        assert checker.was_auth_error("ghcr.io/owner/image:main") is False

    async def test_was_auth_error_true_after_manifest_401(self, mock_client):
        """When the manifest HEAD returns 401, was_auth_error is True."""
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "tok"}
        mock_client.get = AsyncMock(return_value=token_resp)
        manifest_resp = MagicMock(status_code=401)
        manifest_resp.headers = {}
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = RegistryChecker(mock_client)

        result = await checker.get_latest_digest("ghcr.io/owner/private:main")

        assert result is None
        assert checker.was_auth_error("ghcr.io/owner/private:main") is True

    async def test_was_auth_error_true_after_manifest_403(self, mock_client):
        """When the manifest HEAD raises 403, was_auth_error is True."""
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "tok"}
        mock_client.get = AsyncMock(return_value=token_resp)
        mock_client.head = AsyncMock(
            side_effect=ExternalHTTPError(
                "HTTP 403", status_code=403, response=MagicMock(status_code=403)
            )
        )
        checker = RegistryChecker(mock_client)

        result = await checker.get_latest_digest("ghcr.io/owner/private:main")

        assert result is None
        assert checker.was_auth_error("ghcr.io/owner/private:main") is True

    async def test_was_auth_error_false_on_network_error(self, mock_client):
        """When a network error occurs (not 401/403), was_auth_error is False."""
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))
        checker = RegistryChecker(mock_client)

        result = await checker.get_latest_digest("ghcr.io/owner/image:main")

        assert result is None
        assert checker.was_auth_error("ghcr.io/owner/image:main") is False

    async def test_was_auth_error_false_for_unsupported_registry(self, mock_client):
        """Unsupported registries do not set auth_error."""
        checker = RegistryChecker(mock_client)

        result = await checker.get_latest_digest("quay.io/org/image:latest")

        assert result is None
        assert checker.was_auth_error("quay.io/org/image:latest") is False

    async def test_was_auth_error_caches_properly(self, mock_client):
        """Auth error state is cached alongside the digest (same TTL)."""
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "tok"}
        mock_client.get = AsyncMock(return_value=token_resp)
        manifest_resp = MagicMock(status_code=401)
        manifest_resp.headers = {}
        mock_client.head = AsyncMock(return_value=manifest_resp)
        checker = RegistryChecker(mock_client, ttl_seconds=300)

        await checker.get_latest_digest("ghcr.io/owner/private:main")
        # Second call should hit cache — no extra HTTP request
        auth_err = checker.was_auth_error("ghcr.io/owner/private:main")

        assert auth_err is True
        assert mock_client.head.call_count == 1  # cached

    async def test_was_auth_error_returns_false_when_image_unknown(self, mock_client):
        """For an image that has never been fetched, was_auth_error is False."""
        checker = RegistryChecker(mock_client)
        assert checker.was_auth_error("ghcr.io/unknown/image:main") is False

    async def test_auth_error_cleared_on_success_after_retry(
        self, mock_client, monkeypatch
    ):
        """When a subsequent cache miss succeeds, auth_error is cleared."""
        _fixed_time = 1000

        class _FakeMonotonic:
            def __init__(self):
                self.val = _fixed_time

            def __call__(self):
                return self.val

        fake_mono = _FakeMonotonic()
        monkeypatch.setattr(
            "robotsix_central_deploy.registry_check.checker.time.monotonic", fake_mono
        )

        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {"token": "tok"}
        mock_client.get = AsyncMock(return_value=token_resp)

        manifest_resp_401 = MagicMock(status_code=401)
        manifest_resp_401.headers = {}
        manifest_resp_ok = MagicMock(status_code=200)
        manifest_resp_ok.headers = {"Docker-Content-Digest": "sha256:ok"}
        mock_client.head = AsyncMock(side_effect=[manifest_resp_401, manifest_resp_ok])

        checker = RegistryChecker(mock_client, ttl_seconds=300)

        await checker.get_latest_digest("ghcr.io/owner/img:main")
        assert checker.was_auth_error("ghcr.io/owner/img:main") is True

        # Advance time past TTL
        fake_mono.val = _fixed_time + 301

        result = await checker.get_latest_digest("ghcr.io/owner/img:main")
        assert result == "sha256:ok"
        assert checker.was_auth_error("ghcr.io/owner/img:main") is False
