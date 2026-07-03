"""Docker CLI backend — executes lifecycle actions via the ``docker`` CLI."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Optional

from .base import ExecutionBackend
from ..models import (
    ComponentInspect,
    DeployOutcome,
    DockerDfStats,
    ExecutionBackendType,
    RollbackOutcome,
    SelfInspect,
    ServiceRecord,
    ServiceState,
)

if TYPE_CHECKING:
    from ...registry.models import ComponentConfig

logger = logging.getLogger(__name__)


async def _run(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
        return (
            proc.returncode or 0,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )
    except asyncio.TimeoutError:
        logger.warning("Command timed out after %.1fs: %s", timeout, args)
        return (-1, "", f"timed out after {timeout}s")
    except FileNotFoundError:
        logger.error("Command not found: %s", args[0])
        return (-1, "", f"executable not found: {args[0]}")
    except Exception as exc:
        logger.exception("Unexpected error running %s", args)
        return (-1, "", str(exc))


class DockerBackend(ExecutionBackend):
    """Executes lifecycle actions via the local Docker daemon (``docker`` CLI)."""

    async def start(self, service: ServiceRecord) -> ServiceState:
        if not service.image:
            logger.warning(
                "Service %r has no image — cannot start via Docker", service.name
            )
            return ServiceState.FAILED

        # Try `docker start` (container may already exist), fall back to `docker run`.
        rc, _, stderr = await _run(
            ExecutionBackendType.DOCKER.value,
            "start",
            service.name,
        )
        if rc == 0:
            return ServiceState.RUNNING

        # Container may not exist — create and start it.
        rc, _, stderr = await _run(
            ExecutionBackendType.DOCKER.value,
            "run",
            "-d",
            "--name",
            service.name,
            service.image,
        )
        if rc != 0:
            logger.error("docker run %s failed: %s", service.name, stderr)
            return ServiceState.FAILED
        return ServiceState.RUNNING

    async def stop(self, service: ServiceRecord) -> ServiceState:
        rc, _, stderr = await _run(
            ExecutionBackendType.DOCKER.value, "stop", service.name
        )
        if rc != 0:
            # If it's already stopped, treat as success.
            state = await self._inspect_state(service.name)
            if state in (ServiceState.STOPPED, None):
                return ServiceState.STOPPED
            logger.error("docker stop %s failed: %s", service.name, stderr)
            return ServiceState.FAILED
        return ServiceState.STOPPED

    async def remove_container(self, service: ServiceRecord) -> None:
        pass

    async def restart(self, service: ServiceRecord) -> ServiceState:
        if not service.image:
            # Restart requires we can recreate; treat like stop+start.
            stop_state = await self.stop(service)
            if stop_state == ServiceState.FAILED:
                return ServiceState.FAILED
            return await self.start(service)

        rc, _, stderr = await _run(
            ExecutionBackendType.DOCKER.value, "restart", service.name
        )
        if rc != 0:
            # Container may not exist — fall back to stop + start.
            await self.stop(service)
            return await self.start(service)
        return ServiceState.RUNNING

    async def status(self, service: ServiceRecord) -> ComponentInspect:
        state = (
            await self._inspect_state(service.container_name or service.name)
            or ServiceState.UNKNOWN
        )
        return ComponentInspect(state=state)

    async def deploy(
        self, service: ServiceRecord, config: "ComponentConfig", image_ref: str
    ) -> DeployOutcome:
        raise NotImplementedError(
            "deploy not supported for DockerBackend — use DockerSdkBackend"
        )

    async def rollback(
        self, service: ServiceRecord, config: "ComponentConfig"
    ) -> RollbackOutcome:
        raise NotImplementedError(
            "rollback not supported for DockerBackend — use DockerSdkBackend"
        )

    async def stream_logs(
        self,
        service: ServiceRecord,
        tail: int = 100,
        since: str | None = None,
        follow: bool = False,
    ) -> AsyncIterator[bytes]:
        yield b"[docker-cli backend: use docker_sdk for log streaming]\n"

    async def disk_df(self) -> DockerDfStats:
        # CLI backend does not support df — returns zeroes.
        return DockerDfStats()

    async def prune_builds(self) -> int:
        # CLI backend does not support build prune.
        return 0

    async def prune_images(self, protected_refs: set[str]) -> int:
        # CLI backend does not support image prune.
        return 0

    async def write_config_to_volume(
        self, volume_name: str, config_dict: dict[str, Any]
    ) -> None:
        raise NotImplementedError(
            "write_config_to_volume not supported for DockerBackend — use DockerSdkBackend"
        )

    async def read_config_from_volume(self, volume_name: str) -> dict[str, Any]:
        raise NotImplementedError(
            "read_config_from_volume not supported for DockerBackend — use DockerSdkBackend"
        )

    async def run_config_assist(
        self,
        image: str,
        command_str: str,
        volume_name: str,
        volume_mount_path: str,
        env_dict: dict[str, str],
        timeout_seconds: int = 60,
    ) -> str:
        raise NotImplementedError(
            "run_config_assist not supported for DockerBackend — use DockerSdkBackend"
        )

    async def measure_volume_bytes(self, volume_name: str) -> int:
        return 0  # CLI backend lacks volume-inspection support; placeholder.

    async def list_volume_dir(
        self, volume_name: str, rel_path: str
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "list_volume_dir not supported for DockerBackend — use DockerSdkBackend"
        )

    async def read_volume_file(
        self, volume_name: str, rel_path: str, max_bytes: int
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "read_volume_file not supported for DockerBackend — use DockerSdkBackend"
        )

    async def remove_volume(self, volume_name: str) -> None:
        raise NotImplementedError(
            "remove_volume not supported for DockerBackend — use DockerSdkBackend"
        )

    async def inspect_self(self) -> Optional[SelfInspect]:
        raise NotImplementedError(
            "inspect_self not supported for DockerBackend — use DockerSdkBackend"
        )

    async def trigger_self_update(
        self,
        target: SelfInspect,
        watchtower_image: str,
        docker_host_url: str,
        docker_api_version: str,
    ) -> str:
        raise NotImplementedError(
            "trigger_self_update not supported for DockerBackend — use DockerSdkBackend"
        )

    async def _inspect_state(self, container_name: str) -> Optional[ServiceState]:
        """Map ``docker inspect`` output to a ``ServiceState``."""
        rc, stdout, _stderr = await _run(
            ExecutionBackendType.DOCKER.value,
            "inspect",
            "-f",
            "{{.State.Status}}",
            container_name,
        )
        if rc != 0:
            return None  # Container not found → treat as unknown.

        status = stdout.strip().lower()
        mapping: dict[str, ServiceState] = {
            "running": ServiceState.RUNNING,
            "paused": ServiceState.RUNNING,
            "restarting": ServiceState.RESTARTING,
            "created": ServiceState.STOPPED,
            "exited": ServiceState.STOPPED,
            "dead": ServiceState.FAILED,
            "removing": ServiceState.STOPPING,
        }
        return mapping.get(status, ServiceState.UNKNOWN)
