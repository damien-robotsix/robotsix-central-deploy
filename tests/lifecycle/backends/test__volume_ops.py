"""Tests for VolumeOps — Docker named-volume operations."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from robotsix_central_deploy.lifecycle._yaml_utils import YamlParseError
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

    async def test_write_config_chowns_to_component_uid_before_tightening_perms(
        self, client
    ):
        """The busybox writer runs as root but components run as 1000:1000 —
        without the chown, the 700/600 tightening locks the component out of
        its own config.json (chat crash-looped on PermissionError)."""
        vo = VolumeOps(client)
        client.containers.run.return_value = b""

        docker_mock = self._make_docker_mock()
        with patch.dict(sys.modules, {"docker": docker_mock}):
            await vo.write_config_to_volume("cfg-vol", {"a": 1})

        cmd = client.containers.run.call_args[1]["command"][2]
        assert "chown 1000:1000 /config /config/config.json" in cmd
        assert "chmod 700 /config" in cmd
        assert "chmod 600 /config/config.json" in cmd
        # ownership must be fixed before permissions are tightened
        assert cmd.index("chown 1000:1000") < cmd.index("chmod 700")

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
        with patch.dict(sys.modules, {"docker": docker_mock}):  # noqa: SIM117
            with pytest.raises(YamlParseError, match="JSON parse error"):
                await vo.read_config_from_volume("config-vol")

    async def test_non_dict_json_raises_invalid_config_structure_error(self, client):
        vo = VolumeOps(client)
        client.containers.run.return_value = b'["list", "not", "dict"]'

        docker_mock = self._make_docker_mock()
        with patch.dict(sys.modules, {"docker": docker_mock}):  # noqa: SIM117
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


# ---------------------------------------------------------------------------
# relocate_volume
# ---------------------------------------------------------------------------


class TestVolumeOpsRelocate:
    """Unit tests for the data-loss-sensitive relocation path.

    The docker SDK client is fully faked so every failure branch can be
    exercised without a real daemon.  The target directory is a real
    ``tmp_path`` so the sentinel probe exercises genuine file I/O.
    """

    @pytest.fixture
    def docker_mock(self) -> MagicMock:
        m = MagicMock()

        class _APIError(Exception):
            def __init__(self, msg: str = "api error", status_code: int | None = None):
                super().__init__(msg)
                self.status_code = status_code
                self.explanation = msg

        class _NotFound(Exception):
            pass

        class _ContainerError(Exception):
            def __init__(self, msg: str = "container error", exit_status: int = 1):
                super().__init__(msg)
                self.exit_status = exit_status

        class _DockerException(Exception):
            pass

        m.errors.APIError = _APIError
        m.errors.NotFound = _NotFound
        m.errors.ContainerError = _ContainerError
        m.errors.DockerException = _DockerException
        return m

    @pytest.fixture
    def client(self, docker_mock: MagicMock) -> MagicMock:
        c = MagicMock()
        c.volumes.get.return_value.attrs = {
            "Driver": "local",
            "Mountpoint": "/var/lib/docker/volumes/my-vol/_data",
            "Options": {
                "device": "/old/disk/my-vol",
                "o": "bind",
                "type": "none",
            },
            "Labels": {"com.example": "kept"},
        }
        c.volumes.create.return_value = None
        return c

    @staticmethod
    def _target_dir(tmp_path) -> str:
        return str(tmp_path / "robotsix-volumes" / "my-vol")

    async def test_happy_path(self, client, docker_mock, tmp_path):
        vo = VolumeOps(client)
        target = self._target_dir(tmp_path)
        # probe, copy, verify all succeed.
        # The probe uses run() synchronously, so it returns bytes.
        # The copy and verify use detach=True, so they return container objects.
        probe_result = b"robotsix-probe"
        copy_container = MagicMock()
        copy_container.wait.return_value = {"StatusCode": 0}
        copy_container.logs.return_value = b""
        verify_container = MagicMock()
        verify_container.wait.return_value = {"StatusCode": 0}
        client.containers.run.side_effect = [
            probe_result,
            copy_container,
            verify_container,
        ]
        # First create raises 409 (volume name exists), second succeeds.
        conflict = docker_mock.errors.APIError("already exists", status_code=409)
        client.volumes.create.side_effect = [conflict, None]

        with (
            patch.object(vo, "ensure_volume_ownership") as mock_owner,
            patch.dict(sys.modules, {"docker": docker_mock}),
        ):
            result = await vo.relocate_volume("my-vol", str(tmp_path), "1000:1000")

        assert result["status"] == "ok"
        assert "relocated" in result["detail"]
        # The old volume was removed (force=True) after the 409 so the
        # retry create can succeed with the same name at the new location.
        client.volumes.get.return_value.remove.assert_called_once_with(force=True)
        assert client.volumes.create.call_count == 2
        # Second call is the recreation at the target path.
        second_create = client.volumes.create.call_args_list[1]
        assert second_create.args == ("my-vol",)
        assert second_create.kwargs["driver_opts"]["device"] == target
        mock_owner.assert_called_once_with("my-vol", 1000, 1000, 0o755)

    async def test_volume_not_found(self, client, docker_mock, tmp_path):
        vo = VolumeOps(client)
        client.volumes.get.side_effect = docker_mock.errors.NotFound("gone")

        with patch.dict(sys.modules, {"docker": docker_mock}):
            result = await vo.relocate_volume("my-vol", str(tmp_path), "1000:1000")

        assert result == {"status": "failed", "detail": "Volume 'my-vol' not found"}
        client.volumes.create.assert_not_called()
        client.containers.run.assert_not_called()

    async def test_inspect_api_error_is_graceful(self, client, docker_mock, tmp_path):
        vo = VolumeOps(client)
        client.volumes.get.side_effect = docker_mock.errors.APIError(
            "daemon unreachable", status_code=500
        )

        with patch.dict(sys.modules, {"docker": docker_mock}):
            result = await vo.relocate_volume("my-vol", str(tmp_path), "1000:1000")

        assert result["status"] == "failed"
        assert "inspect" in result["detail"]
        client.volumes.create.assert_not_called()
        client.containers.run.assert_not_called()

    async def test_copy_failure_leaves_source_intact(
        self, client, docker_mock, tmp_path
    ):
        vo = VolumeOps(client)
        client.containers.run.side_effect = [
            b"robotsix-probe",  # probe
            MagicMock(wait=lambda **kw: {"StatusCode": 1}),  # copy fails
        ]

        with patch.dict(sys.modules, {"docker": docker_mock}):
            result = await vo.relocate_volume("my-vol", str(tmp_path), "1000:1000")

        assert result["status"] == "failed"
        assert "copy failed" in result["detail"]
        client.volumes.create.assert_not_called()
        client.volumes.get.return_value.remove.assert_not_called()

    async def test_verification_failure_leaves_source_intact(
        self, client, docker_mock, tmp_path
    ):
        vo = VolumeOps(client)
        client.containers.run.side_effect = [
            b"robotsix-probe",  # probe
            MagicMock(
                wait=lambda **kw: {"StatusCode": 0}, logs=lambda **kw: b""
            ),  # copy ok
            MagicMock(wait=lambda **kw: {"StatusCode": 1}),  # verify fails
        ]

        with patch.dict(sys.modules, {"docker": docker_mock}):
            result = await vo.relocate_volume("my-vol", str(tmp_path), "1000:1000")

        assert result["status"] == "failed"
        assert "verification failed" in result["detail"]
        client.volumes.create.assert_not_called()
        client.volumes.get.return_value.remove.assert_not_called()

    async def test_recreate_failure_restores_old_volume(
        self, client, docker_mock, tmp_path
    ):
        vo = VolumeOps(client)
        client.containers.run.side_effect = [
            b"robotsix-probe",  # probe
            MagicMock(
                wait=lambda **kw: {"StatusCode": 0}, logs=lambda **kw: b""
            ),  # copy ok
            MagicMock(wait=lambda **kw: {"StatusCode": 0}),  # verify ok
        ]
        conflict = docker_mock.errors.APIError("already exists", status_code=409)
        recreate_err = docker_mock.errors.APIError("daemon exploded", status_code=500)
        # 1st create -> 409 (old volume present), retry create -> 500, restore -> ok.
        client.volumes.create.side_effect = [conflict, recreate_err, None]

        with patch.dict(sys.modules, {"docker": docker_mock}):
            result = await vo.relocate_volume("my-vol", str(tmp_path), "1000:1000")

        assert result["status"] == "failed"
        assert "recreate" in result["detail"]
        # The old volume was removed once (force=True) before the retry.
        client.volumes.get.return_value.remove.assert_called_once_with(force=True)
        assert client.volumes.create.call_count == 3
        restore_call = client.volumes.create.call_args_list[2]
        assert restore_call.args == ("my-vol",)
        assert restore_call.kwargs["driver"] == "local"
        assert restore_call.kwargs["driver_opts"] == {
            "device": "/old/disk/my-vol",
            "o": "bind",
            "type": "none",
        }
        assert restore_call.kwargs["labels"] == {"com.example": "kept"}
