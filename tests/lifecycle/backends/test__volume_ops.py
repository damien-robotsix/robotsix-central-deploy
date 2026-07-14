"""Tests for VolumeOps — Docker named-volume operations."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from robotsix_central_deploy._yaml_utils import YamlParseError
from robotsix_central_deploy.lifecycle.backends._volume_ops import VolumeOps


# ---------------------------------------------------------------------------
# resolve_user_to_uid_gid (static)
# ---------------------------------------------------------------------------


class TestVolumeOpsResolveUserToUidGid:
    @pytest.mark.parametrize(
        "user_str,expected",
        [
            ("1000:1000", (1000, 1000)),
            ("1000", (1000, 1000)),
            ("0:0", (0, 0)),
            ("999:500", (999, 500)),
        ],
    )
    def test_numeric_strings(self, user_str, expected):
        assert VolumeOps.resolve_user_to_uid_gid(user_str) == expected

    def test_username_colon_groupname(self):
        pwd_mock = MagicMock()
        pwd_mock.getpwnam.return_value.pw_uid = 1000
        grp_mock = MagicMock()
        grp_mock.getgrnam.return_value.gr_gid = 2000
        with patch.dict(sys.modules, {"pwd": pwd_mock, "grp": grp_mock}):
            uid, gid = VolumeOps.resolve_user_to_uid_gid("alice:staff")
        assert uid == 1000
        assert gid == 2000

    def test_username_only(self):
        pwd_mock = MagicMock()
        pwd_mock.getpwnam.return_value.pw_uid = 500
        pwd_mock.getpwnam.return_value.pw_gid = 500
        grp_mock = MagicMock()
        # _resolve_gid tries grp.getgrnam first; make it fail so the
        # fallback to pwd.getpwnam().pw_gid is exercised.
        grp_mock.getgrnam.side_effect = KeyError("no such group")
        with patch.dict(sys.modules, {"pwd": pwd_mock, "grp": grp_mock}):
            uid, gid = VolumeOps.resolve_user_to_uid_gid("bob")
        assert uid == 500
        assert gid == 500

    def test_group_not_found_falls_back_to_user_gid(self):
        pwd_mock = MagicMock()
        pwd_mock.getpwnam.return_value.pw_uid = 1000
        pwd_mock.getpwnam.return_value.pw_gid = 1000
        grp_mock = MagicMock()
        grp_mock.getgrnam.side_effect = KeyError("no such group")
        with patch.dict(sys.modules, {"pwd": pwd_mock, "grp": grp_mock}):
            uid, gid = VolumeOps.resolve_user_to_uid_gid("alice:nonexistent_group")
        assert uid == 1000
        assert gid == 1000


# ---------------------------------------------------------------------------
# ensure_volume_ownership
# ---------------------------------------------------------------------------


class TestVolumeOpsEnsureVolumeOwnership:
    def test_runs_busybox_with_correct_chown_chmod(self):
        client = MagicMock()
        vo = VolumeOps(client)
        vo.ensure_volume_ownership("my-vol", 1000, 2000, 0o755)
        client.containers.run.assert_called_once()
        call_kwargs = client.containers.run.call_args[1]
        assert call_kwargs["command"][0] == "sh"
        assert call_kwargs["command"][1] == "-c"
        shell_cmd = call_kwargs["command"][2]
        assert "chown 1000:2000 /mnt" in shell_cmd
        assert "chmod 755 /mnt" in shell_cmd
        assert call_kwargs["volumes"]["my-vol"]["bind"] == "/mnt"
        assert call_kwargs["volumes"]["my-vol"]["mode"] == "rw"
        assert call_kwargs["remove"] is True


# ---------------------------------------------------------------------------
# write_config_to_volume / write_llmio_tier_config_to_volume
# ---------------------------------------------------------------------------


class TestVolumeOpsWriteConfig:
    @pytest.fixture
    def client(self) -> MagicMock:
        return MagicMock()

    def _make_docker_mock(self) -> MagicMock:
        docker_mock = MagicMock()
        docker_mock.errors.APIError = type("APIError", (Exception,), {})
        return docker_mock

    async def test_write_llmio_tier_config_writes_parseable_json(self, client):
        import base64

        vo = VolumeOps(client)
        client.containers.run.return_value = b""
        tier_config = {"tier": "premium", "limits": {"cpu": 4, "mem": "8G"}}

        docker_mock = self._make_docker_mock()
        with patch.dict(sys.modules, {"docker": docker_mock}):
            await vo.write_llmio_tier_config_to_volume("data-vol", tier_config)

        call_kwargs = client.containers.run.call_args[1]
        cmd = call_kwargs["command"][2]
        assert "/config/llmio_tier_config.json" in cmd
        encoded = cmd.split("echo ", 1)[1].split(" | base64 -d", 1)[0]
        written = base64.b64decode(encoded).decode()
        assert json.loads(written) == tier_config
        assert call_kwargs["volumes"]["data-vol"]["mode"] == "rw"
        assert call_kwargs["remove"] is True

    async def test_write_llmio_tier_config_raises_on_api_error(self, client):
        vo = VolumeOps(client)

        docker_mock = self._make_docker_mock()
        with patch.dict(sys.modules, {"docker": docker_mock}):
            import docker

            api_error = docker.errors.APIError("boom")
            client.containers.run.side_effect = api_error
            # Use the SAME APIError type so isinstance checks pass.
            docker_mock.errors.APIError = type(api_error)

            with pytest.raises(
                RuntimeError, match="llmio_tier_config\\.json write failed"
            ):
                await vo.write_llmio_tier_config_to_volume("data-vol", {"key": "val"})


# ---------------------------------------------------------------------------
# read_config_from_volume
# ---------------------------------------------------------------------------


class TestVolumeOpsReadConfig:
    @pytest.fixture
    def client(self) -> MagicMock:
        return MagicMock()

    def _make_docker_mock(self) -> MagicMock:
        docker_mock = MagicMock()
        docker_mock.errors.APIError = type("APIError", (Exception,), {})
        return docker_mock

    async def test_reads_valid_json_dict(self, client):
        vo = VolumeOps(client)
        client.containers.run.return_value = b'{"host": "localhost", "port": 8080}'

        docker_mock = self._make_docker_mock()
        with patch.dict(sys.modules, {"docker": docker_mock}):
            result = await vo.read_config_from_volume("config-vol")

        assert result == {"host": "localhost", "port": 8080}
        # Verify volumes mounted read-only
        call_kwargs = client.containers.run.call_args[1]
        assert call_kwargs["volumes"]["config-vol"]["mode"] == "ro"

    async def test_empty_output_returns_empty_dict(self, client):
        vo = VolumeOps(client)
        client.containers.run.return_value = b""

        docker_mock = self._make_docker_mock()
        with patch.dict(sys.modules, {"docker": docker_mock}):
            result = await vo.read_config_from_volume("config-vol")

        assert result == {}

    async def test_whitespace_only_output_returns_empty_dict(self, client):
        vo = VolumeOps(client)
        client.containers.run.return_value = b"   \n  \t  "

        docker_mock = self._make_docker_mock()
        with patch.dict(sys.modules, {"docker": docker_mock}):
            result = await vo.read_config_from_volume("config-vol")

        assert result == {}

    async def test_malformed_json_raises_yaml_parse_error(self, client):
        vo = VolumeOps(client)
        client.containers.run.return_value = b"{not valid json}"

        docker_mock = self._make_docker_mock()
        with patch.dict(sys.modules, {"docker": docker_mock}):
            with pytest.raises(YamlParseError, match="JSON parse error"):
                await vo.read_config_from_volume("config-vol")

    async def test_non_dict_json_raises_invalid_config_structure_error(self, client):
        vo = VolumeOps(client)
        client.containers.run.return_value = b'["list", "not", "dict"]'

        docker_mock = self._make_docker_mock()
        with patch.dict(sys.modules, {"docker": docker_mock}):
            # InvalidConfigStructureError extends ValueError, so it is
            # caught by the except (JSONDecodeError, ValueError) handler
            # and re-raised as a YamlParseError.
            with pytest.raises(YamlParseError, match="Expected a mapping"):
                await vo.read_config_from_volume("config-vol")

    async def test_api_error_raises_runtime_error(self, client):
        vo = VolumeOps(client)

        docker_mock = self._make_docker_mock()
        with patch.dict(sys.modules, {"docker": docker_mock}):
            import docker

            api_error = docker.errors.APIError("docker API failure")
            client.containers.run.side_effect = api_error
            docker_mock.errors.APIError = type(api_error)

            with pytest.raises(RuntimeError, match="read_config_from_volume failed"):
                await vo.read_config_from_volume("config-vol")


# ---------------------------------------------------------------------------
# measure_volume_bytes
# ---------------------------------------------------------------------------


class TestVolumeOpsMeasureVolumeBytes:
    @pytest.fixture
    def client(self) -> MagicMock:
        return MagicMock()

    async def test_normal_output_returns_parsed_int(self, client):
        vo = VolumeOps(client)
        client.containers.run.return_value = b"1048576\n"

        result = await vo.measure_volume_bytes("data-vol")

        assert result == 1048576
        call_kwargs = client.containers.run.call_args[1]
        assert call_kwargs["volumes"]["data-vol"]["mode"] == "ro"
        assert call_kwargs["remove"] is True

    async def test_zero_output_returns_zero(self, client):
        vo = VolumeOps(client)
        client.containers.run.return_value = b"0\n"

        result = await vo.measure_volume_bytes("data-vol")

        assert result == 0

    async def test_empty_output_returns_zero(self, client):
        vo = VolumeOps(client)
        client.containers.run.return_value = b""

        result = await vo.measure_volume_bytes("data-vol")

        assert result == 0

    async def test_error_returns_zero(self, client):
        vo = VolumeOps(client)
        client.containers.run.side_effect = RuntimeError("container failed")

        result = await vo.measure_volume_bytes("data-vol")

        assert result == 0
