"""Abstract execution backend interface."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Optional

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

logger = logging.getLogger(__name__)


class ExecutionBackend(ABC):
    """Abstract interface for service lifecycle operations."""

    @abstractmethod
    async def start(self, service: ServiceRecord) -> ServiceState:
        pass

    @abstractmethod
    async def stop(self, service: ServiceRecord) -> ServiceState:
        pass

    @abstractmethod
    async def remove_container(self, service: ServiceRecord) -> None:
        """Remove the managed container for *service* (best-effort, already stopped)."""
        pass

    @abstractmethod
    async def restart(self, service: ServiceRecord) -> ServiceState:
        pass

    @abstractmethod
    async def status(self, service: ServiceRecord) -> ComponentInspect:
        pass

    @abstractmethod
    async def deploy(
        self,
        service: ServiceRecord,
        config: "ComponentConfig",
        image_ref: str,
    ) -> DeployOutcome:
        pass

    @abstractmethod
    async def rollback(
        self,
        service: ServiceRecord,
        config: "ComponentConfig",
    ) -> RollbackOutcome:
        pass

    @abstractmethod
    def stream_logs(
        self,
        service: ServiceRecord,
        tail: int = 100,
        since: str | None = None,
        follow: bool = False,
    ) -> AsyncIterator[bytes]:
        pass

    @abstractmethod
    async def disk_df(self) -> DockerDfStats:
        """Return Docker storage breakdown (images, build cache, reclaimable)."""
        pass

    @abstractmethod
    async def prune_builds(self) -> int:
        """Prune Docker build cache. Returns bytes reclaimed."""
        pass

    @abstractmethod
    async def prune_images(self, protected_refs: set[str]) -> int:
        """Remove dangling images, skipping any whose image id or repo digest
        is in *protected_refs* (rollback targets must stay pullable-free —
        rollback recreates containers from a local image id, which Docker
        cannot re-pull). Returns bytes reclaimed."""
        pass

    @abstractmethod
    async def write_config_to_volume(
        self, volume_name: str, config_dict: dict[str, Any]
    ) -> None:
        """Write *config_dict* as YAML into a Docker named volume."""
        pass

    @abstractmethod
    async def write_llmio_tier_config_to_volume(
        self, volume_name: str, tier_config: dict[str, Any]
    ) -> None:
        """Write *tier_config* as ``llmio_tier_config.json`` into a Docker named volume."""
        pass

    @abstractmethod
    async def read_config_from_volume(self, volume_name: str) -> dict[str, Any]:
        """Read /config/config.json from a named volume; return parsed dict (empty if absent)."""
        pass

    @abstractmethod
    async def run_config_assist(
        self,
        image: str,
        command_str: str,
        volume_name: str,
        volume_mount_path: str,
        env_dict: dict[str, str],
        timeout_seconds: int = 60,
    ) -> str:
        """Run a one-shot container from *image* executing *command_str* with the config
        volume mounted at *volume_mount_path*. Returns captured stdout+stderr.
        Raises TimeoutError if the container does not exit within *timeout_seconds*.
        Always removes the container on exit or timeout."""
        pass

    @abstractmethod
    async def measure_volume_bytes(self, volume_name: str) -> int:
        """Return effective total bytes for *volume_name*, excluding SQLite
        transient sidecars (*.db-wal, *.db-shm, *.db-journal).
        Returns 0 on error or when the volume is inaccessible.
        """

    @abstractmethod
    async def list_volume_dir(
        self, volume_name: str, rel_path: str
    ) -> list[dict[str, Any]]:
        """Return one entry per immediate child of ``/vol/<rel_path>``.

        Each entry is ``{"name": str, "type": 'file'|'dir', "size_bytes": int}``
        (dirs report size_bytes 0).
        """
        pass

    @abstractmethod
    async def read_volume_file(
        self, volume_name: str, rel_path: str, max_bytes: int
    ) -> dict[str, Any]:
        """Return ``{"size_bytes": int, "content": str|None, "binary": bool,
        "truncated": bool}`` for the file at ``/vol/<rel_path>``.

        *content* is None when the file is binary (NUL byte or UTF-8
        decode failure).  *truncated* is True when the file exceeded
        *max_bytes*.
        """
        pass

    @abstractmethod
    async def remove_volume(self, volume_name: str) -> None:
        """Remove the Docker named volume *volume_name*.

        Best-effort and IRREVERSIBLE: destroys all data stored in the
        volume.  Implementations must NOT raise on failure (a volume that
        is already gone, or a transient removal error, must not abort the
        caller) — they log and return instead.
        """
        pass

    @abstractmethod
    async def inspect_self(self) -> Optional[SelfInspect]:
        """Identify the container this server runs in.

        Returns ``None`` when the server is not containerised (or the
        backend cannot tell) — self-update is unsupported in that case.
        """
        pass

    @abstractmethod
    async def trigger_self_update(
        self,
        target: SelfInspect,
        watchtower_image: str,
        docker_host_url: str,
        docker_api_version: str,
    ) -> str:
        """Launch a one-shot watchtower container that updates *target*.

        The watchtower container is attached to the same networks as
        *target* so it reaches the Docker API endpoint at
        *docker_host_url* (the socket proxy). *docker_api_version* is
        exported as ``DOCKER_API_VERSION`` — watchtower 1.7.1's client
        defaults to API 1.25, below modern daemons' minimum, and crashes
        without it. Returns the watchtower container id. Raises
        ``RuntimeError`` on failure to launch.
        """
        pass

    @abstractmethod
    async def trigger_self_restart(self, target: SelfInspect) -> str:
        """Restart the container identified by *target* via the Docker API.

        The Docker daemon accepts the restart command and returns
        immediately, then sends SIGTERM to the container asynchronously.
        This allows the HTTP response to flush before the process is
        killed. Returns the container id. Raises ``RuntimeError`` on
        failure.
        """
        pass

    # -- claude-auth --------------------------------------------------------

    @abstractmethod
    async def check_claude_auth(self, volume_name: str) -> dict[str, Any]:
        """Check whether *volume_name* holds valid Claude credentials.

        Returns a dict with at least ``status``: one of ``"authenticated"``,
        ``"not-authenticated"``, ``"expiring"``, or ``"error"``, plus an
        optional ``detail`` string.
        """
        pass

    @abstractmethod
    async def write_claude_credentials(
        self, volume_name: str, credentials_json: str
    ) -> dict[str, Any]:
        """Write *credentials_json* directly into *volume_name* as
        ``.credentials.json`` with ownership ``1000:1000`` and mode ``0600``.
        Returns a dict with ``status``.
        """
        pass

    @abstractmethod
    async def read_claude_credentials(self, volume_name: str) -> dict[str, Any]:
        """Read and return the parsed ``.credentials.json`` from *volume_name*.

        Returns the parsed JSON dict.  Raises ``ValueError`` when the file
        is missing or unparsable.
        """
        pass
