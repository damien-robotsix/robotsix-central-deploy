"""Tests for lifecycle/backends/docker_cli.py — DockerBackend via subprocess."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robotsix_central_deploy.lifecycle.backends.docker_cli import DockerBackend, _run
from robotsix_central_deploy.lifecycle.backends.base import ExecutionBackend
from robotsix_central_deploy.lifecycle.models import ServiceRecord, ServiceState


# ---------------------------------------------------------------------------
# _run helper — subprocess I/O, timeout, error handling
# ---------------------------------------------------------------------------


class TestRunHelper:
    """Unit tests for the module-level ``_run`` helper."""

    @pytest.fixture
    def mock_subprocess(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"stdout output\n", b""))
        proc.returncode = 0
        return proc

    async def test_success_returns_stdout_stderr_and_returncode(self, mock_subprocess):
        """_run returns (returncode, stdout, stderr) on success."""
        mock_subprocess.communicate = AsyncMock(return_value=(b"hello\n", b"warn\n"))
        mock_subprocess.returncode = 0
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli.asyncio.create_subprocess_exec",
            AsyncMock(return_value=mock_subprocess),
        ):
            rc, stdout, stderr = await _run("docker", "ps")
        assert rc == 0
        assert stdout == "hello\n"
        assert stderr == "warn\n"

    async def test_returncode_none_treated_as_zero(self, mock_subprocess):
        """_run treats None returncode as 0."""
        mock_subprocess.returncode = None
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli.asyncio.create_subprocess_exec",
            AsyncMock(return_value=mock_subprocess),
        ):
            rc, stdout, stderr = await _run("docker", "ps")
        assert rc == 0

    async def test_timeout_returns_minus_one(self):
        """_run returns (-1, '', timeout message) on TimeoutError."""
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli.asyncio.create_subprocess_exec"
        ) as mock_create:
            mock_create.side_effect = asyncio.TimeoutError()
            rc, stdout, stderr = await _run("docker", "ps", timeout=1.0)
        assert rc == -1
        assert stdout == ""
        assert "timed out" in stderr

    async def test_file_not_found_returns_minus_one(self):
        """_run returns (-1, '', 'executable not found') on FileNotFoundError."""
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError("no docker")),
        ):
            rc, stdout, stderr = await _run("nonexistent")
        assert rc == -1
        assert stdout == ""
        assert "executable not found" in stderr

    async def test_generic_exception_returns_minus_one(self):
        """_run returns (-1, '', str(exc)) on unexpected Exception."""
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            rc, stdout, stderr = await _run("docker", "ps")
        assert rc == -1
        assert stdout == ""
        assert "boom" in stderr

    async def test_stdout_stderr_decoded_with_replace(self):
        """Non-UTF-8 bytes are decoded with errors='replace'."""
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"out\xff", b"err\x80"))
        proc.returncode = 1
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            rc, stdout, stderr = await _run("docker", "ps")
        assert rc == 1
        assert "\ufffd" in stdout  # replacement character
        assert "\ufffd" in stderr


# ---------------------------------------------------------------------------
# _inspect_state — docker inspect status string → ServiceState mapping
# ---------------------------------------------------------------------------


class TestInspectState:
    """Unit tests for DockerBackend._inspect_state."""

    @pytest.fixture
    def backend(self) -> DockerBackend:
        return DockerBackend()

    @pytest.mark.parametrize(
        "status_string,expected",
        [
            ("running", ServiceState.RUNNING),
            ("paused", ServiceState.RUNNING),
            ("restarting", ServiceState.RESTARTING),
            ("created", ServiceState.STOPPED),
            ("exited", ServiceState.STOPPED),
            ("dead", ServiceState.FAILED),
            ("removing", ServiceState.STOPPING),
        ],
    )
    async def test_known_statuses_map_correctly(self, backend, status_string, expected):
        """Each known docker inspect status maps to the correct ServiceState."""
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(return_value=(0, status_string + "\n", "")),
        ):
            result = await backend._inspect_state("test-container")
        assert result == expected

    async def test_unknown_status_returns_unknown(self, backend):
        """An unrecognized status string returns ServiceState.UNKNOWN."""
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(return_value=(0, "bogus\n", "")),
        ):
            result = await backend._inspect_state("test-container")
        assert result == ServiceState.UNKNOWN

    async def test_container_not_found_returns_none(self, backend):
        """When docker inspect fails (rc != 0), return None."""
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(return_value=(1, "", "No such container")),
        ):
            result = await backend._inspect_state("missing-container")
        assert result is None

    async def test_whitespace_and_case_are_normalized(self, backend):
        """Status with leading/trailing whitespace and mixed case is mapped."""
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(return_value=(0, "  EXITED  \n", "")),
        ):
            result = await backend._inspect_state("test-container")
        assert result == ServiceState.STOPPED


# ---------------------------------------------------------------------------
# start — docker start, fallback to docker run
# ---------------------------------------------------------------------------


class TestStart:
    @pytest.fixture
    def backend(self) -> DockerBackend:
        return DockerBackend()

    @pytest.fixture
    def service(self) -> ServiceRecord:
        return ServiceRecord(name="test-svc", image="ghcr.io/o/img:main")

    async def test_docker_start_succeeds(self, backend, service):
        """When `docker start` succeeds, return RUNNING."""
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(return_value=(0, "", "")),
        ) as mock_run:
            result = await backend.start(service)
        assert result == ServiceState.RUNNING
        # First call is docker start
        assert mock_run.call_args_list[0][0][0] == "docker"
        assert mock_run.call_args_list[0][0][1] == "start"

    async def test_docker_start_fails_falls_back_to_run(self, backend, service):
        """When `docker start` fails, fall back to `docker run`."""
        side_effects = [
            (1, "", "no such container"),  # docker start fails
            (0, "", ""),  # docker run succeeds
        ]
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(side_effect=side_effects),
        ) as mock_run:
            result = await backend.start(service)
        assert result == ServiceState.RUNNING
        assert mock_run.call_count == 2
        # First call: docker start
        assert mock_run.call_args_list[0][0][1] == "start"
        # Second call: docker run
        assert mock_run.call_args_list[1][0][1] == "run"

    async def test_docker_run_fails_returns_failed(self, backend, service):
        """When both `docker start` and `docker run` fail, return FAILED."""
        side_effects = [
            (1, "", "no such container"),
            (1, "", "port already in use"),
        ]
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(side_effect=side_effects),
        ):
            result = await backend.start(service)
        assert result == ServiceState.FAILED

    async def test_no_image_returns_failed(self, backend):
        """When service has no image, start returns FAILED immediately."""
        service = ServiceRecord(name="no-img", image="")
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(),
        ) as mock_run:
            result = await backend.start(service)
        assert result == ServiceState.FAILED
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# stop — docker stop, idempotency
# ---------------------------------------------------------------------------


class TestStop:
    @pytest.fixture
    def backend(self) -> DockerBackend:
        return DockerBackend()

    @pytest.fixture
    def service(self) -> ServiceRecord:
        return ServiceRecord(name="test-svc", image="ghcr.io/o/img:main")

    async def test_docker_stop_succeeds(self, backend, service):
        """When `docker stop` succeeds, return STOPPED."""
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(return_value=(0, "", "")),
        ):
            result = await backend.stop(service)
        assert result == ServiceState.STOPPED

    async def test_docker_stop_fails_but_already_stopped(self, backend, service):
        """When `docker stop` fails but inspect reports STOPPED, return STOPPED."""
        side_effects = [
            (1, "", "container not running"),  # docker stop fails
            (0, "exited\n", ""),  # inspect says stopped
        ]
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(side_effect=side_effects),
        ):
            result = await backend.stop(service)
        assert result == ServiceState.STOPPED

    async def test_docker_stop_fails_and_container_gone(self, backend, service):
        """When `docker stop` fails and inspect returns None, return STOPPED."""
        side_effects = [
            (1, "", "no such container"),  # docker stop fails
            (1, "", "No such container"),  # inspect returns not found → None
        ]
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(side_effect=side_effects),
        ):
            result = await backend.stop(service)
        assert result == ServiceState.STOPPED

    async def test_docker_stop_fails_and_still_running(self, backend, service):
        """When `docker stop` fails and container is still running, return FAILED."""
        side_effects = [
            (1, "", "permission denied"),  # docker stop fails
            (0, "running\n", ""),  # inspect says running
        ]
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(side_effect=side_effects),
        ):
            result = await backend.stop(service)
        assert result == ServiceState.FAILED


# ---------------------------------------------------------------------------
# restart — docker restart, fallbacks
# ---------------------------------------------------------------------------


class TestRestart:
    @pytest.fixture
    def backend(self) -> DockerBackend:
        return DockerBackend()

    @pytest.fixture
    def service(self) -> ServiceRecord:
        return ServiceRecord(name="test-svc", image="ghcr.io/o/img:main")

    async def test_docker_restart_succeeds(self, backend, service):
        """When `docker restart` succeeds, return RUNNING."""
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(return_value=(0, "", "")),
        ):
            result = await backend.restart(service)
        assert result == ServiceState.RUNNING

    async def test_docker_restart_fails_falls_back_to_stop_start(
        self, backend, service
    ):
        """When `docker restart` fails, fall back to stop + start."""
        side_effects = [
            (1, "", "no such container"),  # restart fails
            (0, "", ""),  # stop success
            (0, "", ""),  # start success (docker start)
        ]
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(side_effect=side_effects),
        ) as mock_run:
            result = await backend.restart(service)
        assert result == ServiceState.RUNNING
        assert mock_run.call_count == 3

    async def test_no_image_falls_back_to_stop_start(self, backend):
        """When service has no image, restart falls back to stop + start."""
        service = ServiceRecord(name="no-img", image="")
        side_effects = [
            (0, "", ""),  # stop succeeds
            (-1, "", ""),  # start fails (no image)
        ]
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(side_effect=side_effects),
        ):
            result = await backend.restart(service)
        # start returns FAILED when there's no image
        assert result == ServiceState.FAILED

    async def test_no_image_stop_fails_returns_failed(self, backend):
        """When stop fails for no-image service, restart returns FAILED."""
        service = ServiceRecord(name="no-img", image="")
        side_effects = [
            (1, "", "permission denied"),  # stop fails
            (0, "running\n", ""),  # inspect says running
        ]
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(side_effect=side_effects),
        ):
            result = await backend.restart(service)
        assert result == ServiceState.FAILED


# ---------------------------------------------------------------------------
# status — inspect + ComponentInspect
# ---------------------------------------------------------------------------


class TestStatus:
    @pytest.fixture
    def backend(self) -> DockerBackend:
        return DockerBackend()

    async def test_status_with_container_name(self, backend):
        """status prefers container_name over name."""
        service = ServiceRecord(
            name="svc", container_name="custom-container", image="img:1"
        )
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(return_value=(0, "running\n", "")),
        ) as mock_run:
            result = await backend.status(service)
        assert result.state == ServiceState.RUNNING
        mock_run.assert_called_once()
        # inspects by container_name, not name (it's the 5th positional arg)
        assert mock_run.call_args[0][4] == "custom-container"

    async def test_status_falls_back_to_name(self, backend):
        """When container_name is empty, status falls back to name."""
        service = ServiceRecord(name="svc", image="img:1")
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(return_value=(0, "exited\n", "")),
        ) as mock_run:
            result = await backend.status(service)
        assert result.state == ServiceState.STOPPED
        assert mock_run.call_args[0][4] == "svc"

    async def test_status_inspect_returns_none_returns_unknown(self, backend):
        """When inspect returns None, status returns UNKNOWN."""
        service = ServiceRecord(name="missing", image="img:1")
        with patch(
            "robotsix_central_deploy.lifecycle.backends.docker_cli._run",
            AsyncMock(return_value=(1, "", "not found")),
        ):
            result = await backend.status(service)
        assert result.state == ServiceState.UNKNOWN


# ---------------------------------------------------------------------------
# Stream logs — yields informational message
# ---------------------------------------------------------------------------


class TestStreamLogs:
    @pytest.fixture
    def backend(self) -> DockerBackend:
        return DockerBackend()

    async def test_stream_logs_yields_info_message(self, backend):
        service = ServiceRecord(name="x", image="i:1")
        chunks = [chunk async for chunk in backend.stream_logs(service)]
        assert len(chunks) == 1
        assert b"docker-cli backend" in chunks[0]


# ---------------------------------------------------------------------------
# Stub methods — default/zero return values (no-op, not NotImplementedError)
# ---------------------------------------------------------------------------


class TestStubReturnValues:
    @pytest.fixture
    def backend(self) -> DockerBackend:
        return DockerBackend()

    async def test_disk_df_returns_zeroes(self, backend):
        result = await backend.disk_df()
        assert result is not None

    async def test_prune_builds_returns_zero(self, backend):
        result = await backend.prune_builds()
        assert result == 0

    async def test_prune_images_returns_zero(self, backend):
        result = await backend.prune_images(set())
        assert result == 0

    async def test_measure_volume_bytes_returns_zero(self, backend):
        result = await backend.measure_volume_bytes("vol")
        assert result == 0

    async def test_remove_container_is_noop(self, backend):
        """remove_container does nothing (pass)."""
        service = ServiceRecord(name="x", image="i:1")
        # Must not raise
        await backend.remove_container(service)


# ---------------------------------------------------------------------------
# Contract test — all ExecutionBackend abstract methods exist on DockerBackend
# ---------------------------------------------------------------------------


class TestContractConformance:
    """Ensure DockerBackend satisfies the ExecutionBackend interface."""

    def test_all_abstract_methods_implemented(self):
        """Every abstract method on ExecutionBackend is present on DockerBackend."""

        backend_methods = set(dir(DockerBackend))

        # Collect all abstract method names from the MRO.
        abstract_names: set[str] = set()
        for cls in ExecutionBackend.__mro__:
            for name, attr in cls.__dict__.items():
                if getattr(attr, "__isabstractmethod__", False):
                    abstract_names.add(name)

        missing = abstract_names - backend_methods
        assert not missing, f"DockerBackend is missing abstract methods: {missing}"

    @pytest.mark.parametrize(
        "method_name",
        [
            "deploy",
            "rollback",
            "write_config_to_volume",
            "write_llmio_tier_config_to_volume",
            "read_config_from_volume",
            "run_config_assist",
            "list_volume_dir",
            "read_volume_file",
            "remove_volume",
            "inspect_self",
            "trigger_self_update",
            "check_claude_auth",
            "write_claude_credentials",
            "read_claude_credentials",
        ],
    )
    async def test_not_implemented_methods_raise_not_implemented_error(
        self, method_name
    ):
        """Each NotImplementedError stub raises as expected."""
        backend = DockerBackend()
        method = getattr(backend, method_name)

        # Build reasonable dummy args for each method.
        # The exact signature varies; use try/except to call.
        with pytest.raises(NotImplementedError):
            if method_name == "deploy":
                await method(MagicMock(), MagicMock(), "img:ref")
            elif method_name == "rollback":
                await method(MagicMock(), MagicMock())
            elif method_name in (
                "write_config_to_volume",
                "write_llmio_tier_config_to_volume",
            ):
                await method("vol", {})
            elif method_name == "read_config_from_volume":
                await method("vol")
            elif method_name == "run_config_assist":
                await method("img", "cmd", "vol", "/mnt", {}, 60)
            elif method_name == "list_volume_dir":
                await method("vol", "")
            elif method_name == "read_volume_file":
                await method("vol", "f", 100)
            elif method_name == "remove_volume":
                await method("vol")
            elif method_name == "inspect_self":
                await method()
            elif method_name == "trigger_self_update":
                await method(MagicMock(), "img", "url", "1.43")
            elif method_name == "check_claude_auth":
                await method("vol")
            elif method_name == "write_claude_credentials":
                await method("vol", '{"key":"val"}')
            elif method_name == "read_claude_credentials":
                await method("vol")
