"""Noop backend — always reports success, for testing / dry runs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Optional

from .base import ExecutionBackend
from ..models import (
    ComponentInspect,
    DeployOutcome,
    DockerDfStats,
    RollbackOutcome,
    SelfInspect,
    ServiceRecord,
    ServiceState,
)

if TYPE_CHECKING:
    from ...registry.models import ComponentConfig


class NoopBackend(ExecutionBackend):
    """Backend that does nothing — always reports success."""

    async def start(self, service: ServiceRecord) -> ServiceState:
        return ServiceState.RUNNING

    async def stop(self, service: ServiceRecord) -> ServiceState:
        return ServiceState.STOPPED

    async def remove_container(self, service: ServiceRecord) -> None:
        pass

    async def restart(self, service: ServiceRecord) -> ServiceState:
        return ServiceState.RUNNING

    async def status(self, service: ServiceRecord) -> ComponentInspect:
        return ComponentInspect(state=service.state)

    async def deploy(
        self, service: ServiceRecord, config: "ComponentConfig", image_ref: str
    ) -> DeployOutcome:
        return DeployOutcome(
            deployed_digest="sha256:noop",
            previous_digest="",
            state=ServiceState.RUNNING,
        )

    async def rollback(
        self, service: ServiceRecord, config: "ComponentConfig"
    ) -> RollbackOutcome:
        return RollbackOutcome(
            deployed_digest=service.previous_image_digest or "sha256:noop",
            state=ServiceState.RUNNING,
        )

    async def stream_logs(
        self,
        service: ServiceRecord,
        tail: int = 100,
        since: str | None = None,
        follow: bool = False,
    ) -> AsyncIterator[bytes]:
        yield b"[noop backend]\n"

    async def disk_df(self) -> DockerDfStats:
        return DockerDfStats()

    async def prune_builds(self) -> int:
        return 0

    async def prune_images(self, protected_refs: set[str]) -> int:
        return 0

    async def write_config_to_volume(
        self, volume_name: str, config_dict: dict[str, Any]
    ) -> None:
        pass

    async def read_config_from_volume(self, volume_name: str) -> dict[str, Any]:
        return {}

    async def run_config_assist(
        self,
        image: str,
        command_str: str,
        volume_name: str,
        volume_mount_path: str,
        env_dict: dict[str, str],
        timeout_seconds: int = 60,
    ) -> str:
        return "[noop backend]"

    async def measure_volume_bytes(self, volume_name: str) -> int:
        return 0

    async def list_volume_dir(
        self, volume_name: str, rel_path: str
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("list_volume_dir not supported for NoopBackend")

    async def read_volume_file(
        self, volume_name: str, rel_path: str, max_bytes: int
    ) -> dict[str, Any]:
        raise NotImplementedError("read_volume_file not supported for NoopBackend")

    async def remove_volume(self, volume_name: str) -> None:
        pass

    async def inspect_self(self) -> Optional[SelfInspect]:
        return None

    async def trigger_self_update(
        self,
        target: SelfInspect,
        watchtower_image: str,
        docker_host_url: str,
        docker_api_version: str,
    ) -> str:
        return "noop-self-update"
