"""Unit tests for lifecyle.backends._auth_ops AuthOps."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from robotsix_central_deploy.lifecycle.backends._auth_ops import AuthOps


class TestCheckClaudeAuth:
    """Tests for AuthOps.check_claude_auth()."""

    @pytest.fixture
    def docker_mock(self):
        dm = MagicMock()
        dm.errors.NotFound = type("NotFound", (Exception,), {})
        dm.errors.ContainerError = type("ContainerError", (Exception,), {})
        dm.errors.APIError = type("APIError", (Exception,), {})
        return dm

    @pytest.fixture
    def client_mock(self):
        return MagicMock()

    @pytest.fixture
    def auth_ops(self, client_mock: MagicMock, docker_mock: MagicMock):
        with patch.dict(sys.modules, {"docker": docker_mock}):
            ops = AuthOps(client_mock)
            yield ops, client_mock, docker_mock

    async def test_volume_not_found_returns_not_authenticated(self, auth_ops):
        ops, client, dm = auth_ops
        client.volumes.get.side_effect = dm.errors.NotFound("no such volume")
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "not-authenticated"
        assert "does not exist" in result["detail"]

    async def test_container_error_returns_not_authenticated(self, auth_ops):
        ops, client, dm = auth_ops
        client.containers.run.side_effect = dm.errors.ContainerError()
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "not-authenticated"
        assert "No credentials" in result["detail"]

    async def test_missing_content_returns_not_authenticated(self, auth_ops):
        ops, client, dm = auth_ops
        client.containers.run.return_value = b"MISSING"
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "not-authenticated"
        assert "No credentials" in result["detail"]

    async def test_non_json_content_returns_error(self, auth_ops):
        ops, client, dm = auth_ops
        client.containers.run.return_value = b"not valid json at all"
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "error"
        assert "not valid JSON" in result["detail"]

    async def test_valid_credentials_no_expiry_returns_authenticated(self, auth_ops):
        ops, client, dm = auth_ops
        creds = {"claudeAiOauth": {"accessToken": "tok"}}
        client.containers.run.return_value = json.dumps(creds).encode()
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "authenticated"

    async def test_ms_epoch_expiry_far_future_returns_authenticated(self, auth_ops):
        ops, client, dm = auth_ops
        import time

        future_ms = int((time.time() + 7 * 86400) * 1000)
        creds = {"claudeAiOauth": {"accessToken": "tok", "expiresAt": future_ms}}
        client.containers.run.return_value = json.dumps(creds).encode()
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "authenticated"

    async def test_ms_epoch_expiry_expired_no_refresh(self, auth_ops):
        ops, client, dm = auth_ops
        import time

        past_ms = int((time.time() - 3600) * 1000)
        creds = {"claudeAiOauth": {"accessToken": "tok", "expiresAt": past_ms}}
        client.containers.run.return_value = json.dumps(creds).encode()
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "not-authenticated"
        assert "expired" in result["detail"]

    async def test_ms_epoch_expiry_expired_with_refresh(self, auth_ops):
        ops, client, dm = auth_ops
        import time

        past_ms = int((time.time() - 3600) * 1000)
        creds = {
            "claudeAiOauth": {
                "accessToken": "tok",
                "expiresAt": past_ms,
                "refreshToken": "rt",
            }
        }
        client.containers.run.return_value = json.dumps(creds).encode()
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "authenticated"
        assert "refreshes" in result["detail"]

    async def test_ms_epoch_expiry_soon_without_refresh(self, auth_ops):
        ops, client, dm = auth_ops
        import time

        # 1 hour from now (< 24h → expiring)
        soon_ms = int((time.time() + 3600) * 1000)
        creds = {"claudeAiOauth": {"accessToken": "tok", "expiresAt": soon_ms}}
        client.containers.run.return_value = json.dumps(creds).encode()
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "expiring"
        assert "expire in" in result["detail"]

    async def test_ms_epoch_expiry_soon_with_refresh_stays_authenticated(
        self, auth_ops
    ):
        ops, client, dm = auth_ops
        import time

        soon_ms = int((time.time() + 3600) * 1000)
        creds = {
            "claudeAiOauth": {
                "accessToken": "tok",
                "expiresAt": soon_ms,
                "refreshToken": "rt",
            }
        }
        client.containers.run.return_value = json.dumps(creds).encode()
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "authenticated"

    async def test_iso_expiry_format_far_future(self, auth_ops):
        ops, client, dm = auth_ops
        creds = {"expires_at": "2099-01-01T00:00:00Z"}
        client.containers.run.return_value = json.dumps(creds).encode()
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "authenticated"

    async def test_top_level_expires_at_expired(self, auth_ops):
        ops, client, dm = auth_ops
        creds = {"expires_at": "2020-01-01T00:00:00Z"}
        client.containers.run.return_value = json.dumps(creds).encode()
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "not-authenticated"
        assert "expired" in result["detail"]

    async def test_top_level_expires_at_camel_case(self, auth_ops):
        ops, client, dm = auth_ops
        import time

        future_ms = int((time.time() + 7 * 86400) * 1000)
        creds = {"expiresAt": future_ms}
        client.containers.run.return_value = json.dumps(creds).encode()
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "authenticated"

    async def test_empty_content_returns_not_authenticated(self, auth_ops):
        ops, client, dm = auth_ops
        client.containers.run.return_value = b""
        result = await ops.check_claude_auth("test-vol")
        assert result["status"] == "not-authenticated"
        assert "No credentials" in result["detail"]


class TestWriteClaudeCredentials:
    """Tests for AuthOps.write_claude_credentials()."""

    @pytest.fixture
    def docker_mock(self):
        dm = MagicMock()
        dm.errors.NotFound = type("NotFound", (Exception,), {})
        dm.errors.ContainerError = type("ContainerError", (Exception,), {})
        dm.errors.APIError = type("APIError", (Exception,), {})
        return dm

    @pytest.fixture
    def client_mock(self):
        return MagicMock()

    @pytest.fixture
    def auth_ops(self, client_mock: MagicMock, docker_mock: MagicMock):
        with patch.dict(sys.modules, {"docker": docker_mock}):
            ops = AuthOps(client_mock)
            yield ops, client_mock, docker_mock

    async def test_invalid_json_returns_error(self, auth_ops):
        ops, client, dm = auth_ops
        result = await ops.write_claude_credentials("test-vol", "not json")
        assert result["status"] == "error"
        assert "Invalid JSON" in result["error"]

    async def test_volume_exists_writes_credentials_no_create(self, auth_ops):
        ops, client, dm = auth_ops
        result = await ops.write_claude_credentials("test-vol", '{"key":"val"}')
        assert result["status"] == "authenticated"
        client.volumes.create.assert_not_called()
        assert client.containers.run.called

    async def test_volume_not_found_creates_volume_and_writes(self, auth_ops):
        ops, client, dm = auth_ops
        client.volumes.get.side_effect = dm.errors.NotFound("no vol")
        result = await ops.write_claude_credentials("test-vol", '{"key":"val"}')
        assert result["status"] == "authenticated"
        client.volumes.create.assert_called_once_with("test-vol")
        # Two container runs: chown then write
        assert client.containers.run.call_count >= 1

    async def test_writes_base64_encoded_content(self, auth_ops):
        ops, client, dm = auth_ops
        creds = '{"claudeAiOauth":{"accessToken":"my-token"}}'
        result = await ops.write_claude_credentials("test-vol", creds)
        assert result["status"] == "authenticated"
        # Verify the B64 env var was passed to the write container run
        call_kwargs = client.containers.run.call_args_list[-1][1]
        assert "environment" in call_kwargs
        assert "B64" in call_kwargs["environment"]


class TestReadClaudeCredentials:
    """Tests for AuthOps.read_claude_credentials()."""

    @pytest.fixture
    def docker_mock(self):
        dm = MagicMock()
        dm.errors.NotFound = type("NotFound", (Exception,), {})
        dm.errors.ContainerError = type("ContainerError", (Exception,), {})
        dm.errors.APIError = type("APIError", (Exception,), {})
        return dm

    @pytest.fixture
    def client_mock(self):
        return MagicMock()

    @pytest.fixture
    def auth_ops(self, client_mock: MagicMock, docker_mock: MagicMock):
        with patch.dict(sys.modules, {"docker": docker_mock}):
            ops = AuthOps(client_mock)
            yield ops, client_mock, docker_mock

    async def test_volume_not_found_raises_value_error(self, auth_ops):
        ops, client, dm = auth_ops
        client.volumes.get.side_effect = dm.errors.NotFound("no vol")
        with pytest.raises(ValueError, match="does not exist"):
            await ops.read_claude_credentials("test-vol")

    async def test_container_error_raises_value_error(self, auth_ops):
        ops, client, dm = auth_ops
        client.containers.run.side_effect = dm.errors.ContainerError()
        with pytest.raises(ValueError, match="No credentials"):
            await ops.read_claude_credentials("test-vol")

    async def test_missing_content_raises_value_error(self, auth_ops):
        ops, client, dm = auth_ops
        client.containers.run.return_value = b"MISSING"
        with pytest.raises(ValueError, match="No credentials"):
            await ops.read_claude_credentials("test-vol")

    async def test_non_dict_json_raises_value_error(self, auth_ops):
        ops, client, dm = auth_ops
        client.containers.run.return_value = b'["list", "not", "dict"]'
        with pytest.raises(ValueError, match="not a JSON object"):
            await ops.read_claude_credentials("test-vol")

    async def test_invalid_json_raises_value_error(self, auth_ops):
        ops, client, dm = auth_ops
        client.containers.run.return_value = b"not json"
        with pytest.raises(ValueError, match="not valid JSON"):
            await ops.read_claude_credentials("test-vol")

    async def test_valid_credentials_returns_parsed_dict(self, auth_ops):
        ops, client, dm = auth_ops
        creds = {"claudeAiOauth": {"accessToken": "tok123"}}
        client.containers.run.return_value = json.dumps(creds).encode()
        result = await ops.read_claude_credentials("test-vol")
        assert result == creds
        assert isinstance(result, dict)

    async def test_empty_content_raises_value_error(self, auth_ops):
        ops, client, dm = auth_ops
        client.containers.run.return_value = b""
        with pytest.raises(ValueError, match="No credentials"):
            await ops.read_claude_credentials("test-vol")
