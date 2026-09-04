"""Tests for VolumeOps — Docker named-volume operations."""

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from robotsix_central_deploy.lifecycle._yaml_utils import YamlParseError
from robotsix_central_deploy.lifecycle.backends._volume_ops import (
    _DU_BYTES_FN,
    VolumeOps,
)

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
# write_volume_file
# ---------------------------------------------------------------------------


class TestVolumeOpsWriteVolumeFile:
    @staticmethod
    def _docker_mock() -> MagicMock:
        docker_mock = MagicMock()
        docker_mock.errors.APIError = type("APIError", (Exception,), {})
        docker_mock.errors.ContainerError = type("ContainerError", (Exception,), {})
        return docker_mock

    @staticmethod
    def _container(logs: bytes = b"") -> MagicMock:
        container = MagicMock()
        container.status = "exited"
        container.attrs = {"State": {"ExitCode": 0}}
        container.logs.return_value = logs
        return container

    @staticmethod
    def _extract_payload(container: MagicMock) -> bytes:
        """Return the raw bytes of /robotsix-staging/payload from the tar sent
        to put_archive."""
        import io
        import tarfile

        _path, blob = container.put_archive.call_args[0]
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r") as tar:
            member = tar.extractfile("robotsix-staging/payload")
            assert member is not None
            return member.read()

    async def test_large_content_streamed_not_inlined_in_argv(self):
        """A write near the 1 MiB cap must not inline content into the shell
        argv (which Linux caps at 128 KiB); it is streamed via put_archive."""
        client = MagicMock()
        container = self._container()
        client.containers.create.return_value = container
        vo = VolumeOps(client)

        big = "A" * 500_000  # ~500 KiB, well past MAX_ARG_STRLEN

        with patch.dict(sys.modules, {"docker": self._docker_mock()}):
            result = await vo.write_volume_file("data-vol", "blob.json", big, False)

        assert result == {"size_bytes": 500_000}
        # No inlined content anywhere in the argv (script + args).
        command = client.containers.create.call_args[1]["command"]
        assert all(big not in part for part in command)
        # command = ["sh", "-c", <script>, "sh", rel_path, overwrite_flag]
        assert command[4] == "blob.json"
        # Content reached the container as a streamed tar payload instead.
        assert self._extract_payload(container).decode() == big
        container.put_archive.assert_called_once()
        container.start.assert_called_once()
        container.remove.assert_called_once_with(force=True)

    async def test_returns_byte_count_for_utf8(self):
        client = MagicMock()
        client.containers.create.return_value = self._container()
        vo = VolumeOps(client)

        with patch.dict(sys.modules, {"docker": self._docker_mock()}):
            result = await vo.write_volume_file("data-vol", "note.txt", "héllo", False)

        # "héllo" is 6 UTF-8 bytes (é = 2 bytes).
        assert result == {"size_bytes": 6}

    async def test_file_exists_marker_raises(self):
        from robotsix_central_deploy.lifecycle.backends._volume_ops import _FILE_EXISTS

        client = MagicMock()
        client.containers.create.return_value = self._container(
            logs=(_FILE_EXISTS + "\n").encode()
        )
        vo = VolumeOps(client)

        with (
            patch.dict(sys.modules, {"docker": self._docker_mock()}),
            pytest.raises(FileExistsError),
        ):
            await vo.write_volume_file("data-vol", "note.txt", "x", False)

    async def test_is_a_dir_marker_raises(self):
        from robotsix_central_deploy.lifecycle.backends._volume_ops import _IS_A_DIR

        client = MagicMock()
        client.containers.create.return_value = self._container(
            logs=(_IS_A_DIR + "\n").encode()
        )
        vo = VolumeOps(client)

        with (
            patch.dict(sys.modules, {"docker": self._docker_mock()}),
            pytest.raises(IsADirectoryError),
        ):
            await vo.write_volume_file("data-vol", "adir", "x", True)

    async def test_container_removed_on_api_error(self):
        client = MagicMock()
        container = self._container()
        client.containers.create.return_value = container
        vo = VolumeOps(client)

        docker_mock = self._docker_mock()
        with patch.dict(sys.modules, {"docker": docker_mock}):
            import docker

            container.start.side_effect = docker.errors.APIError("boom")
            with pytest.raises(RuntimeError, match="write_volume_file failed"):
                await vo.write_volume_file("data-vol", "note.txt", "x", False)

        # The helper container is always cleaned up, even on failure.
        container.remove.assert_called_once_with(force=True)


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


def _one_shot_container(output: bytes) -> MagicMock:
    """Mock of the detached helper container the one-shot runner drives."""
    container = MagicMock()
    container.status = "exited"
    container.attrs = {"State": {"ExitCode": 0}}
    container.logs.return_value = output
    return container


def _exited_container(exit_code: int, logs: bytes = b"") -> MagicMock:
    """Mock of a detached container that has already exited with *exit_code*."""
    container = MagicMock()
    container.status = "exited"
    container.attrs = {"State": {"ExitCode": exit_code}}
    container.logs.return_value = logs
    return container


class TestVolumeOpsMeasureVolumeBytes:
    @pytest.fixture
    def client(self) -> MagicMock:
        return MagicMock()

    async def test_normal_output_returns_parsed_int(self, client):
        vo = VolumeOps(client)
        container = _one_shot_container(b"1048576\n")
        client.containers.run.return_value = container

        result = await vo.measure_volume_bytes("data-vol")

        assert result == 1048576
        call_kwargs = client.containers.run.call_args[1]
        assert call_kwargs["volumes"]["data-vol"]["mode"] == "ro"
        assert call_kwargs["detach"] is True
        container.remove.assert_called_once_with(force=True)

    async def test_zero_output_returns_zero(self, client):
        vo = VolumeOps(client)
        client.containers.run.return_value = _one_shot_container(b"0\n")

        result = await vo.measure_volume_bytes("data-vol")

        assert result == 0

    async def test_empty_output_returns_zero(self, client):
        vo = VolumeOps(client)
        client.containers.run.return_value = _one_shot_container(b"")

        result = await vo.measure_volume_bytes("data-vol")

        assert result == 0

    async def test_error_returns_none_after_retries(self, client, monkeypatch):
        """A persistent helper failure surfaces None (measurement-failed),
        not a bogus 0 — the scheduler turns that into a finding."""
        monkeypatch.setattr(VolumeOps, "_MEASURE_RETRY_DELAY_S", 0)
        vo = VolumeOps(client)
        client.containers.run.side_effect = RuntimeError("container failed")

        result = await vo.measure_volume_bytes("data-vol")

        assert result is None

    async def test_transient_failure_retries_then_succeeds(self, client, monkeypatch):
        """A one-off Docker stream cut must not fail the whole measurement:
        the helper is retried and the size is still returned."""
        monkeypatch.setattr(VolumeOps, "_MEASURE_RETRY_DELAY_S", 0)
        monkeypatch.setattr(VolumeOps, "_WAIT_POLL_MAX_CONSECUTIVE_ERRORS", 1)
        vo = VolumeOps(client)
        first = _one_shot_container(b"")
        first.reload.side_effect = RuntimeError("Response ended prematurely")
        second = _one_shot_container(b"1048576\n")
        client.containers.run.side_effect = [first, second]

        result = await vo.measure_volume_bytes("data-vol")

        assert result == 1048576
        first.remove.assert_called_once_with(force=True)
        second.remove.assert_called_once_with(force=True)

    async def test_broken_wait_stream_still_removes_every_attempt(
        self, client, monkeypatch
    ):
        """Regression: a broken attach/wait stream must not orphan the du.

        The old non-detached run(remove=True) leaked the helper container
        when the Docker API stream failed ("Response ended prematurely",
        2026-09-02) — the du kept grinding the volume for 40+ minutes.
        Every retry attempt removes its helper; after all retries the
        measurement returns None (a measurement-failed finding), never 0.
        """
        monkeypatch.setattr(VolumeOps, "_MEASURE_RETRY_DELAY_S", 0)
        monkeypatch.setattr(VolumeOps, "_WAIT_POLL_MAX_CONSECUTIVE_ERRORS", 1)
        vo = VolumeOps(client)
        container = _one_shot_container(b"")
        container.reload.side_effect = RuntimeError("Response ended prematurely")
        client.containers.run.return_value = container

        result = await vo.measure_volume_bytes("data-vol")

        assert result is None
        assert container.remove.call_count == VolumeOps._MEASURE_ATTEMPTS

    async def test_long_helper_survives_proxy_idle_timeout(self, client, monkeypatch):
        """Regression (2026-09-04): the blocking ``/containers/{id}/wait``
        call idles for the helper's whole runtime, and the socket-proxy's
        haproxy (``timeout client/server 10m``) cut it at ~600s on every
        mill-mill-data measure — "Response ended prematurely" three times
        per scan, despite the 1800s deadline.  The wait now polls short
        inspect calls, so a du outliving any idle window still completes
        and ``wait()`` is never used.
        """
        monkeypatch.setattr(VolumeOps, "_WAIT_POLL_START_S", 0)
        vo = VolumeOps(client)
        container = _one_shot_container(b"123\n")
        container.status = "running"
        polls = {"n": 0}

        def _reload():
            polls["n"] += 1
            if polls["n"] >= 150:  # helper runs far longer than one idle window
                container.status = "exited"

        container.reload.side_effect = _reload
        client.containers.run.return_value = container

        result = await vo.measure_volume_bytes("data-vol")

        assert result == 123
        container.wait.assert_not_called()
        container.remove.assert_called_once_with(force=True)

    async def test_cleanup_failure_does_not_mask_result(self, client):
        vo = VolumeOps(client)
        container = _one_shot_container(b"77\n")
        container.remove.side_effect = RuntimeError("daemon busy")
        client.containers.run.return_value = container

        result = await vo.measure_volume_bytes("data-vol")

        assert result == 77


class TestDuBytesLargeTree:
    """Regression test for the recurring mill-mill-data measurement failure.

    The whole-volume du helper must correctly sum a deeply-nested tree of
    per-board workspaces and exclude SQLite transient sidecars — the
    measurement that was silently returning 0 when the helper died/timed out
    (2026-09-02).  The tree is kept modest on purpose: the summation and
    sidecar-exclusion logic is exercised by the nesting and the sidecar files,
    not by raw file count, and a multi-thousand-file tree needlessly spikes
    memory/IO in the constrained CI sandbox (it OOM-killed the suite).
    """

    def test_large_tree_sums_correctly_and_excludes_sidecars(self, tmp_path):
        root = tmp_path / "tree"
        expected = 0
        # Nested boards x workspaces x files — enough to exercise recursion
        # and the -exec du batching without a resource spike.
        for b in range(4):
            for w in range(4):
                d = root / f"board-{b}" / f"workspace-{w}"
                d.mkdir(parents=True, exist_ok=True)
                for f in range(8):
                    size = (b + w + f) % 64 + 1
                    (d / f"file-{f}.txt").write_bytes(b"x" * size)
                    expected += size
        # SQLite sidecars must be excluded from the sum.
        (root / "board-0" / "workspace-0" / "data.db-wal").write_bytes(b"y" * 10_000)
        (root / "board-0" / "workspace-0" / "data.db-shm").write_bytes(b"y" * 20_000)
        (root / "board-0" / "workspace-0" / "data.db-journal").write_bytes(
            b"y" * 30_000
        )

        script = _DU_BYTES_FN + "du_bytes " + str(root) + "\n"
        # The script is assembled from a trusted constant + an absolute tmp
        # path, not user input; sh -c is required to exercise the real helper.
        proc = subprocess.run(  # noqa: S603
            ["sh", "-c", script],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert int(proc.stdout.strip()) == expected


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
        copy_container.status = "exited"
        copy_container.attrs = {"State": {"ExitCode": 0}}
        copy_container.logs.return_value = b""
        verify_container = MagicMock()
        verify_container.status = "exited"
        verify_container.attrs = {"State": {"ExitCode": 0}}
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
        # Both create calls should pass the old volume's labels.
        for call in client.volumes.create.call_args_list:
            assert call.kwargs.get("labels") == {"com.example": "kept"}
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
            _exited_container(1),  # copy fails
        ]

        with patch.dict(sys.modules, {"docker": docker_mock}):
            result = await vo.relocate_volume("my-vol", str(tmp_path), "1000:1000")

        assert result["status"] == "failed"
        assert "copy failed" in result["detail"]
        client.volumes.create.assert_not_called()
        client.volumes.get.return_value.remove.assert_not_called()

    async def test_non_bind_mount_rejected_before_copy(
        self, client, docker_mock, tmp_path
    ):
        """A regular (non-bind-mount) Docker volume is rejected before any
        copy/verify work, avoiding a wasted full copy cycle (review #436)."""
        vo = VolumeOps(client)
        # Override the fixture: no bind-mount type.
        del client.volumes.get.return_value.attrs["Options"]["type"]
        client.volumes.get.return_value.attrs["Options"] = {"device": "/some/data"}

        with patch.dict(sys.modules, {"docker": docker_mock}):
            result = await vo.relocate_volume("my-vol", str(tmp_path), "1000:1000")

        assert result["status"] == "failed"
        assert "not a bind-mount volume" in result["detail"]
        # No Docker operations beyond inspection should occur.
        client.containers.run.assert_not_called()
        client.volumes.create.assert_not_called()
        client.volumes.get.return_value.remove.assert_not_called()

    async def test_verification_failure_leaves_source_intact(
        self, client, docker_mock, tmp_path
    ):
        vo = VolumeOps(client)
        client.containers.run.side_effect = [
            b"robotsix-probe",  # probe
            _exited_container(0),  # copy ok
            _exited_container(1),  # verify fails
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
            _exited_container(0),  # copy ok
            _exited_container(0),  # verify ok
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


# ---------------------------------------------------------------------------
# one-shot helper labelling + stale sweep
# ---------------------------------------------------------------------------


class TestVolumeOpsStaleHelperSweep:
    @pytest.fixture
    def client(self) -> MagicMock:
        return MagicMock()

    async def test_one_shot_helpers_carry_the_sweep_label(self, client):
        """Regression: a helper without the label is invisible to the startup
        sweep, so a self-update that kills the parent mid-wait leaks it
        forever (2026-09-02: the old server's hourly du of mill-mill-data
        outlived the process by 10+ minutes)."""
        vo = VolumeOps(client)
        client.containers.run.return_value = _one_shot_container(b"1\n")

        await vo.measure_volume_bytes("data-vol")

        labels = client.containers.run.call_args[1]["labels"]
        assert labels == {VolumeOps.HELPER_LABEL: "1"}

    async def test_remove_stale_helpers_force_removes_labelled(self, client):
        vo = VolumeOps(client)
        stale_a, stale_b = MagicMock(), MagicMock()
        client.containers.list.return_value = [stale_a, stale_b]

        removed = await vo.remove_stale_helpers()

        assert removed == 2
        client.containers.list.assert_called_once_with(
            all=True, filters={"label": VolumeOps.HELPER_LABEL}
        )
        stale_a.remove.assert_called_once_with(force=True)
        stale_b.remove.assert_called_once_with(force=True)

    async def test_remove_stale_helpers_survives_failures(self, client):
        vo = VolumeOps(client)
        bad, good = MagicMock(), MagicMock()
        bad.remove.side_effect = RuntimeError("daemon busy")
        client.containers.list.return_value = [bad, good]

        removed = await vo.remove_stale_helpers()

        assert removed == 1
        good.remove.assert_called_once_with(force=True)

    async def test_remove_stale_helpers_listing_failure_returns_zero(self, client):
        vo = VolumeOps(client)
        client.containers.list.side_effect = RuntimeError("daemon unreachable")

        assert await vo.remove_stale_helpers() == 0


# ---------------------------------------------------------------------------
# prune_volume_files
# ---------------------------------------------------------------------------


class TestVolumeOpsPruneVolumeFiles:
    @pytest.fixture
    def client(self) -> MagicMock:
        return MagicMock()

    async def test_parses_count_and_bytes(self, client):
        vo = VolumeOps(client)
        client.containers.run.return_value = _one_shot_container(b"12 34567\n")

        result = await vo.prune_volume_files("claude-auth", "projects", "*.jsonl", 30)

        assert result == {"removed": 12, "bytes": 34567}
        kwargs = client.containers.run.call_args[1]
        assert kwargs["volumes"]["claude-auth"]["mode"] == "rw"
        # positional args: command = ["sh", "-c", script, "sh", rel, glob, days]
        assert kwargs["command"][4:] == ["projects", "*.jsonl", "30"]
        assert kwargs["detach"] is True
        assert kwargs["labels"] == {VolumeOps.HELPER_LABEL: "1"}

    async def test_unparseable_output_returns_zeros(self, client):
        vo = VolumeOps(client)
        client.containers.run.return_value = _one_shot_container(b"garbage")

        result = await vo.prune_volume_files("v", "", "*", 7)

        assert result == {"removed": 0, "bytes": 0}

    async def test_script_never_deletes_the_rule_root(self, client):
        """The empty-dir cleanup must keep the rule's root directory —
        -mindepth 1 guards it."""
        vo = VolumeOps(client)
        client.containers.run.return_value = _one_shot_container(b"0 0\n")

        await vo.prune_volume_files("v", "projects", "*", 7)

        script = client.containers.run.call_args[1]["command"][2]
        assert "-mindepth 1" in script
        assert "-mtime" in script
