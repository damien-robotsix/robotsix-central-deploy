"""Pluggable execution backends for actually starting/stopping services.

Provides an abstract ``ExecutionBackend`` and a ``DockerBackend`` that
drives ``docker`` / ``docker-compose`` via subprocess.
"""

from __future__ import annotations

import shlex

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Optional

from ..gateway.proxy import PROXY_NETWORK
from robotsix_central_deploy._yaml_utils import (
    InvalidConfigStructureError,
    YamlParseError,
)
from .models import (
    ComponentInspect,
    DeployOutcome,
    DockerDfStats,
    HealthStatus,
    RollbackOutcome,
    SelfInspect,
    ServiceRecord,
    ServiceState,
    VolumeStat,
)

if TYPE_CHECKING:
    from ..registry.models import ComponentConfig

logger = logging.getLogger(__name__)


class ExecutionBackend(ABC):
    """Abstract interface for service lifecycle operations."""

    @abstractmethod
    async def start(self, service: ServiceRecord) -> ServiceState: ...

    @abstractmethod
    async def stop(self, service: ServiceRecord) -> ServiceState: ...

    @abstractmethod
    async def remove_container(self, service: ServiceRecord) -> None:
        """Remove the managed container for *service* (best-effort, already stopped)."""
        ...

    @abstractmethod
    async def restart(self, service: ServiceRecord) -> ServiceState: ...

    @abstractmethod
    async def status(self, service: ServiceRecord) -> ComponentInspect: ...

    @abstractmethod
    async def deploy(
        self,
        service: ServiceRecord,
        config: "ComponentConfig",
        image_ref: str,
    ) -> DeployOutcome: ...

    @abstractmethod
    async def rollback(
        self,
        service: ServiceRecord,
        config: "ComponentConfig",
    ) -> RollbackOutcome: ...

    @abstractmethod
    def stream_logs(
        self,
        service: ServiceRecord,
        tail: int = 100,
        since: str | None = None,
        follow: bool = False,
    ) -> AsyncIterator[bytes]: ...

    @abstractmethod
    async def disk_df(self) -> DockerDfStats:
        """Return Docker storage breakdown (images, build cache, reclaimable)."""
        ...

    @abstractmethod
    async def prune_builds(self) -> int:
        """Prune Docker build cache. Returns bytes reclaimed."""
        ...

    @abstractmethod
    async def prune_images(self, protected_refs: set[str]) -> int:
        """Remove dangling images, skipping any whose image id or repo digest
        is in *protected_refs* (rollback targets must stay pullable-free —
        rollback recreates containers from a local image id, which Docker
        cannot re-pull). Returns bytes reclaimed."""
        ...

    @abstractmethod
    async def write_config_to_volume(
        self, volume_name: str, config_dict: dict[str, Any]
    ) -> None:
        """Write *config_dict* as YAML into a Docker named volume."""
        ...

    @abstractmethod
    async def read_config_from_volume(self, volume_name: str) -> dict[str, Any]:
        """Read /config/config.yaml from a named volume; return parsed dict (empty if absent)."""
        ...

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
        ...

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
        ...

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
        ...

    @abstractmethod
    async def remove_volume(self, volume_name: str) -> None:
        """Remove the Docker named volume *volume_name*.

        Best-effort and IRREVERSIBLE: destroys all data stored in the
        volume.  Implementations must NOT raise on failure (a volume that
        is already gone, or a transient removal error, must not abort the
        caller) — they log and return instead.
        """
        ...

    @abstractmethod
    async def inspect_self(self) -> Optional[SelfInspect]:
        """Identify the container this server runs in.

        Returns ``None`` when the server is not containerised (or the
        backend cannot tell) — self-update is unsupported in that case.
        """
        ...

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
        ...


# ---------------------------------------------------------------------------
# Noop backend (for testing / dry runs)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Docker backend
# ---------------------------------------------------------------------------


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
            "docker",
            "start",
            service.name,
        )
        if rc == 0:
            return ServiceState.RUNNING

        # Container may not exist — create and start it.
        rc, _, stderr = await _run(
            "docker",
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
        rc, _, stderr = await _run("docker", "stop", service.name)
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

        rc, _, stderr = await _run("docker", "restart", service.name)
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
            "docker",
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


# ---------------------------------------------------------------------------
# subprocess helper
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Docker SDK backend
# ---------------------------------------------------------------------------


class DockerSdkBackend(ExecutionBackend):
    """Executes lifecycle actions via the Docker Python SDK against the local socket."""

    def __init__(
        self,
        socket_url: str = "unix:///var/run/docker.sock",
        claude_host_mount_path: str = "",
        timeout: int = 120,
    ) -> None:
        import docker

        self._client = docker.DockerClient(base_url=socket_url, timeout=timeout)
        self._claude_host_mount_path = claude_host_mount_path

    # -- helpers ------------------------------------------------------------

    def _container_name(self, service: ServiceRecord) -> str:
        return service.container_name if service.container_name else service.name

    @staticmethod
    def _state_from_docker(status: str) -> ServiceState:
        status = status.lower()
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

    async def _get_container(self, name: str) -> Any:
        """Run ``containers.get`` in the default executor and map known errors."""
        import docker

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None,
                self._client.containers.get,
                name,
            )
        except docker.errors.NotFound:
            return None
        except docker.errors.APIError:
            raise

    # -- ExecutionBackend ---------------------------------------------------

    async def status(self, service: ServiceRecord) -> ComponentInspect:
        name = self._container_name(service)
        import docker

        try:
            container = await self._get_container(name)
        except docker.errors.APIError as exc:
            logger.error("Docker daemon unreachable during status(%s): %s", name, exc)
            return ComponentInspect(state=ServiceState.UNKNOWN)

        if container is None:
            logger.warning("Container %s not found during status", name)
            return ComponentInspect(state=ServiceState.UNKNOWN)

        loop = asyncio.get_running_loop()

        def _inspect() -> ComponentInspect:
            state_str = container.attrs["State"]["Status"]
            state = self._state_from_docker(state_str)

            # image revision label from the container's image
            revision = ""
            try:
                revision = container.image.labels.get(
                    "org.opencontainers.image.revision",
                    "",
                )
            except Exception:
                pass

            # health check result
            health = ""
            try:
                health_obj = container.attrs["State"].get("Health")
                if health_obj:
                    health = health_obj.get("Status", "")
            except Exception:
                pass

            # running_digest from the image's RepoDigests
            running_digest = ""
            try:
                image_id = container.attrs.get("Image", "")
                if image_id:
                    img = self._client.images.get(image_id)
                    repo_digests = img.attrs.get("RepoDigests", [])
                    # Prefer an entry matching the service image (strips tag)
                    prefix = service.image.rsplit(":", 1)[0] + "@"
                    for rd in repo_digests:
                        if rd.startswith(prefix):
                            running_digest = rd.split("@", 1)[1]
                            break
                    if not running_digest:
                        # Fallback: any RepoDigest entry with sha256
                        for rd in repo_digests:
                            if "@sha256:" in rd:
                                running_digest = rd.split("@", 1)[1]
                                break
            except Exception:
                pass  # Gracefully degrade; digest stays ""

            return ComponentInspect(
                state=state,
                image_revision=revision,
                health=health,
                running_digest=running_digest,
            )

        return await loop.run_in_executor(None, _inspect)

    async def start(self, service: ServiceRecord) -> ServiceState:
        name = self._container_name(service)
        import docker

        try:
            container = await self._get_container(name)
        except docker.errors.APIError as exc:
            logger.error("Docker daemon unreachable during start(%s): %s", name, exc)
            return ServiceState.FAILED

        if container is None:
            logger.warning("Container %s not found — deploy first", name)
            return ServiceState.FAILED

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, container.start)
        except docker.errors.APIError as exc:
            logger.error("Docker API error starting %s: %s", name, exc)
            return ServiceState.FAILED

        return ServiceState.RUNNING

    async def stop(self, service: ServiceRecord) -> ServiceState:
        name = self._container_name(service)
        import docker

        try:
            container = await self._get_container(name)
        except docker.errors.APIError as exc:
            logger.error("Docker daemon unreachable during stop(%s): %s", name, exc)
            return ServiceState.FAILED

        if container is None:
            logger.debug("Container %s not found — already stopped", name)
            return ServiceState.STOPPED

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, container.stop)
        except docker.errors.APIError as exc:
            logger.error("Docker API error stopping %s: %s", name, exc)
            return ServiceState.FAILED

        return ServiceState.STOPPED

    async def remove_container(self, service: ServiceRecord) -> None:
        import docker

        loop = asyncio.get_running_loop()
        name = self._container_name(service)
        container = await self._get_container(name)
        if container is None:
            return
        try:
            await loop.run_in_executor(None, lambda: container.remove(force=True))
        except docker.errors.NotFound:
            pass
        except Exception as exc:
            logger.warning("remove_container %s: %s", name, exc)

    async def restart(self, service: ServiceRecord) -> ServiceState:
        name = self._container_name(service)
        import docker

        try:
            container = await self._get_container(name)
        except docker.errors.APIError as exc:
            logger.error("Docker daemon unreachable during restart(%s): %s", name, exc)
            return ServiceState.FAILED

        if container is None:
            logger.warning("Container %s not found — deploy first", name)
            return ServiceState.FAILED

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, container.restart)
        except docker.errors.APIError as exc:
            logger.error("Docker API error restarting %s: %s", name, exc)
            return ServiceState.FAILED

        return ServiceState.RUNNING

    # -- deploy / rollback --------------------------------------------------

    def _create_container(self, config: "ComponentConfig", image_ref: str) -> Any:
        """Create a Docker container from a ComponentConfig spec (synchronous)."""

        # Host ports are intentionally NOT published: the gateway reaches
        # managed containers over the central-deploy-proxy network by
        # container_name:container_port (gateway/router.py). Publishing host
        # ports caused "port is already allocated" conflicts with existing
        # host-bound services.
        ports: dict[str, Any] = {}
        volumes = {
            m.host: {"bind": m.container, "mode": "ro" if m.read_only else "rw"}
            for m in config.mounts
        }
        if config.claude_mount:
            import os

            claude_host = self._claude_host_mount_path or os.path.expanduser(
                "~/.claude"
            )
            volumes[claude_host] = {"bind": "/home/app/.claude", "mode": "rw"}
        if config.host_docker_sock:
            volumes["/var/run/docker.sock"] = {
                "bind": "/var/run/docker.sock",
                "mode": "ro",
            }
        healthcheck = None
        if config.health_check:
            hc = config.health_check
            healthcheck = {
                "Test": hc.test,
                "Interval": hc.interval_seconds * int(1e9),
                "Timeout": hc.timeout_seconds * int(1e9),
                "Retries": hc.retries,
                "StartPeriod": hc.start_period_seconds * int(1e9),
            }
        return self._client.containers.create(
            image=image_ref,
            name=config.container_name,
            command=config.command,
            entrypoint=config.entrypoint,
            environment=config.env,
            volumes=volumes,
            healthcheck=healthcheck,
            ports=ports,
            detach=True,
            restart_policy={"Name": "unless-stopped"},  # type: ignore[arg-type]  # types-docker stubs are incomplete for restart policy names
            network=PROXY_NETWORK,
        )

    async def _wait_healthy(self, name: str, timeout: float = 60.0) -> None:
        """Poll container health status until healthy, or raise on unhealthy/timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            container = await self._get_container(name)
            if container is None:
                raise RuntimeError(f"Container {name} disappeared during health wait")

            def _poll() -> str:
                container.reload()
                h = container.attrs["State"].get("Health")
                return (
                    h["Status"] if h else HealthStatus.HEALTHY
                )  # no healthcheck → treat as healthy

            status = await loop.run_in_executor(None, _poll)
            if status == HealthStatus.HEALTHY:
                return
            if status == HealthStatus.UNHEALTHY:
                raise RuntimeError(f"Container {name} is unhealthy after deploy")
            await asyncio.sleep(2)
        logger.warning(
            "Health wait timed out for %s after %.0fs — proceeding", name, timeout
        )

    def _stop_and_remove(self, container: Any) -> None:
        """Stop and force-remove a container (synchronous, best-effort stop)."""
        try:
            container.stop(timeout=30)
        except Exception:
            pass
        container.remove(force=True)

    async def deploy(
        self, service: ServiceRecord, config: "ComponentConfig", image_ref: str
    ) -> DeployOutcome:
        """Pull *image_ref*, recreate the container from *config*, return outcome."""
        import docker

        name = self._container_name(service)
        loop = asyncio.get_running_loop()

        # Step 1 — pull target image; obtain its digest
        try:
            image = await loop.run_in_executor(
                None, lambda: self._client.images.pull(image_ref)
            )
        except docker.errors.APIError as exc:
            raise RuntimeError(f"Image pull failed for {image_ref!r}: {exc}") from exc
        # Derive manifest digest from RepoDigests (comparable to registry
        # Docker-Content-Digest header), falling back to config digest.
        repo_without_tag = image_ref.rsplit(":", 1)[0]
        repo_digests = image.attrs.get("RepoDigests", [])
        new_digest: str = next(
            (
                rd.split("@")[1]
                for rd in repo_digests
                if rd.startswith(repo_without_tag + "@")
            ),
            image.id or "",
        )

        # Step 2 — snapshot current container's image digest (for rollback)
        prior_digest = ""
        existing = await self._get_container(name)
        if existing is not None:
            try:
                prior_digest = await loop.run_in_executor(
                    None, lambda: existing.image.id
                )
            except Exception:
                pass

        # Step 3 — stop + remove old container (if present)
        if existing is not None:
            try:
                await loop.run_in_executor(
                    None, lambda: self._stop_and_remove(existing)
                )
            except docker.errors.APIError as exc:
                raise RuntimeError(
                    f"Failed to remove existing container {name!r}: {exc}"
                ) from exc

        # Step 4 — create + start new container
        try:
            # Pre-create named volumes
            for vol_name in config.named_volumes:
                try:
                    await loop.run_in_executor(
                        None, self._client.volumes.create, vol_name
                    )
                except docker.errors.APIError as exc:
                    if exc.status_code == 409:
                        logger.info(
                            "Volume %s already exists, skipping creation", vol_name
                        )
                    else:
                        raise RuntimeError(
                            f"Failed to create volume {vol_name!r}: {exc.explanation or exc}"
                        ) from exc
                except docker.errors.DockerException as exc:
                    raise RuntimeError(
                        f"Docker daemon unreachable while creating volume {vol_name!r}: {exc}"
                    ) from exc

            new_container = await loop.run_in_executor(
                None, lambda: self._create_container(config, image_ref)
            )
            await loop.run_in_executor(None, new_container.start)
        except Exception as exc:
            # Best-effort restore: if we have a prior digest, try to recreate it
            if prior_digest:
                logger.error(
                    "deploy %s failed after container removal — attempting restore from %s",
                    name,
                    prior_digest,
                )
                try:
                    restore = await loop.run_in_executor(
                        None, lambda: self._create_container(config, prior_digest)
                    )
                    await loop.run_in_executor(None, restore.start)
                    logger.info("Restored %s from prior digest %s", name, prior_digest)
                except Exception as restore_exc:
                    logger.error("Restore of %s also failed: %s", name, restore_exc)
            raise RuntimeError(
                f"Container create/start failed for {name!r}: {exc}"
            ) from exc

        # Step 5 — health wait (if configured)
        if config.health_check:
            await self._wait_healthy(name, timeout=60.0)

        return DeployOutcome(
            deployed_digest=new_digest,
            previous_digest=prior_digest,
            state=ServiceState.RUNNING,
        )

    async def rollback(
        self, service: ServiceRecord, config: "ComponentConfig"
    ) -> RollbackOutcome:
        """Recreate container from ``service.previous_image_digest``."""
        import docker

        name = self._container_name(service)
        target_digest = (
            service.previous_image_digest
        )  # guaranteed non-empty by server layer
        loop = asyncio.get_running_loop()

        # Stop + remove current container
        existing = await self._get_container(name)
        if existing is not None:
            try:
                await loop.run_in_executor(
                    None, lambda: self._stop_and_remove(existing)
                )
            except docker.errors.APIError as exc:
                raise RuntimeError(
                    f"Failed to remove container {name!r} for rollback: {exc}"
                ) from exc

        # Create + start from prior digest
        try:
            rollback_container = await loop.run_in_executor(
                None, lambda: self._create_container(config, target_digest)
            )
            await loop.run_in_executor(None, rollback_container.start)
        except Exception as exc:
            raise RuntimeError(
                f"Rollback container create/start failed for {name!r}: {exc}"
            ) from exc

        if config.health_check:
            await self._wait_healthy(name, timeout=60.0)

        return RollbackOutcome(
            deployed_digest=target_digest, state=ServiceState.RUNNING
        )

    async def write_config_to_volume(
        self, volume_name: str, config_dict: dict[str, Any]
    ) -> None:
        """Write *config_dict* as YAML into a Docker named volume via a
        temporary busybox container.

        The volume **must** already exist; this method only writes to it.
        """
        import base64

        import docker
        import yaml

        yaml_content = yaml.dump(
            config_dict, default_flow_style=False, allow_unicode=True
        )
        encoded = base64.b64encode(yaml_content.encode()).decode()
        # base64 output contains only [A-Za-z0-9+/=] — safe to interpolate in sh without quoting
        cmd = f"mkdir -p /config && echo {encoded} | base64 -d > /config/config.yaml && chmod 777 /config && chmod 666 /config/config.yaml"
        loop = asyncio.get_running_loop()

        def _run() -> None:
            try:
                self._client.containers.run(
                    "busybox",
                    command=["sh", "-c", cmd],
                    volumes={volume_name: {"bind": "/config", "mode": "rw"}},
                    remove=True,
                )
            except docker.errors.APIError as exc:
                raise RuntimeError(
                    f"write_config_to_volume failed for {volume_name}: {exc}"
                ) from exc

        await loop.run_in_executor(None, _run)

    async def read_config_from_volume(self, volume_name: str) -> dict[str, Any]:
        """Read /config/config.yaml from a named volume via a temporary busybox container."""
        import yaml

        loop = asyncio.get_running_loop()

        def _run() -> dict[str, Any]:
            import docker

            try:
                raw = self._client.containers.run(
                    "busybox",
                    command=["sh", "-c", "cat /config/config.yaml 2>/dev/null || true"],
                    volumes={volume_name: {"bind": "/config", "mode": "ro"}},
                    remove=True,
                )
                text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw

                data = yaml.safe_load(text)
                if data is None:
                    return {}
                if not isinstance(data, dict):
                    raise InvalidConfigStructureError(
                        f"Expected a mapping in Docker volume {volume_name}, "
                        f"got {type(data).__name__}"
                    )
                return data
            except yaml.YAMLError as exc:
                raise YamlParseError(
                    f"YAML parse error in Docker volume {volume_name}: {exc}"
                ) from exc
            except docker.errors.APIError as exc:
                raise RuntimeError(
                    f"read_config_from_volume failed for {volume_name}: {exc}"
                ) from exc

        return await loop.run_in_executor(None, _run)

    async def measure_volume_bytes(self, volume_name: str) -> int:
        loop = asyncio.get_running_loop()
        cmd = (
            "find /vol -type f "
            "! -name '*.db-wal' ! -name '*.db-shm' ! -name '*.db-journal' "
            "-exec du -b {} + 2>/dev/null "
            "| awk '{s+=$1}END{print s+0}'"
        )
        try:
            raw: bytes = await loop.run_in_executor(
                None,
                lambda: self._client.containers.run(
                    "busybox",
                    command=["sh", "-c", cmd],
                    volumes={volume_name: {"bind": "/vol", "mode": "ro"}},
                    remove=True,
                ),
            )
            return int(raw.strip() or b"0")
        except Exception as exc:
            logger.warning("measure_volume_bytes(%r) failed: %s", volume_name, exc)
            return 0

    async def list_volume_dir(
        self, volume_name: str, rel_path: str
    ) -> list[dict[str, Any]]:
        """List immediate children of ``/vol/<rel_path>`` via a one-shot busybox container."""
        loop = asyncio.get_running_loop()
        # Shell script: iterate /vol/$1, emit tab-delimited  type\tsize\tname
        # per child.  $1 is the normalised relative path (positional arg, not
        # interpolated).  Directories report size 0.
        script = (
            'target="/vol/$1"\n'
            'for f in "$target"/* "$target"/.*; do\n'
            '  [ ! -e "$f" ] && continue\n'
            '  bn=$(basename "$f")\n'
            '  [ "$bn" = "." ] && continue\n'
            '  [ "$bn" = ".." ] && continue\n'
            '  if [ -d "$f" ]; then\n'
            '    printf "dir\\t0\\t%s\\n" "$bn"\n'
            '  elif [ -f "$f" ]; then\n'
            '    sz=$(stat -c "%s" "$f" 2>/dev/null || echo 0)\n'
            '    printf "file\\t%s\\t%s\\n" "$sz" "$bn"\n'
            "  fi\n"
            "done\n"
        )
        raw: bytes = await loop.run_in_executor(
            None,
            lambda: self._client.containers.run(
                "busybox",
                command=["sh", "-c", script, "sh", rel_path],
                volumes={volume_name: {"bind": "/vol", "mode": "ro"}},
                remove=True,
            ),
        )
        entries: list[dict[str, Any]] = []
        for line in raw.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            typ, size_str, name = parts
            try:
                size_bytes = int(size_str)
            except ValueError:
                size_bytes = 0
            entries.append({"name": name, "type": typ, "size_bytes": size_bytes})
        return entries

    async def read_volume_file(
        self, volume_name: str, rel_path: str, max_bytes: int
    ) -> dict[str, Any]:
        """Read ``/vol/<rel_path>`` via a one-shot busybox container.

        Returns size, content (or None for binary), binary flag, truncated flag.
        """
        loop = asyncio.get_running_loop()
        # $1 = rel_path, $2 = max_bytes+1 (head limit)
        script = (
            'target="/vol/$1"\n'
            "maxp1=$2\n"
            'stat -c "%s" "$target" 2>/dev/null || echo 0\n'
            'head -c "$maxp1" "$target" 2>/dev/null || true\n'
        )
        raw: bytes = await loop.run_in_executor(
            None,
            lambda: self._client.containers.run(
                "busybox",
                command=["sh", "-c", script, "sh", rel_path, str(max_bytes + 1)],
                volumes={volume_name: {"bind": "/vol", "mode": "ro"}},
                remove=True,
            ),
        )
        # First line is the file size; the rest is the file content.
        lines = raw.split(b"\n", 1)
        try:
            size_bytes = int(lines[0].strip())
        except ValueError, IndexError:
            size_bytes = 0
        body = lines[1] if len(lines) > 1 else b""

        truncated = len(body) > max_bytes
        if truncated:
            body = body[:max_bytes]

        binary = b"\x00" in body
        content: str | None = None
        if not binary:
            try:
                content = body.decode("utf-8")
            except UnicodeDecodeError:
                binary = True

        return {
            "size_bytes": size_bytes,
            "content": content,
            "binary": binary,
            "truncated": truncated,
        }

    async def remove_volume(self, volume_name: str) -> None:
        """Remove the Docker named volume *volume_name* (best-effort).

        Swallows ``docker.errors.NotFound`` (already gone) and logs a
        warning on any other error — never raises, so a failed volume
        removal cannot abort a component delete.
        """
        import docker

        loop = asyncio.get_running_loop()

        def _remove() -> None:
            self._client.volumes.get(volume_name).remove(force=True)

        try:
            await loop.run_in_executor(None, _remove)
        except docker.errors.NotFound:
            pass
        except Exception as exc:
            logger.warning("remove_volume %s: %s", volume_name, exc)

    async def run_config_assist(
        self,
        image: str,
        command_str: str,
        volume_name: str,
        volume_mount_path: str,
        env_dict: dict[str, str],
        timeout_seconds: int = 60,
    ) -> str:
        """Run a one-shot container from *image*, mount config volume at *volume_mount_path*."""
        import requests.exceptions

        loop = asyncio.get_running_loop()

        def _run() -> str:
            container = self._client.containers.create(
                image,
                command=shlex.split(command_str),
                volumes={volume_name: {"bind": volume_mount_path, "mode": "rw"}},
                environment=env_dict,
            )
            try:
                container.start()
                result = container.wait(timeout=timeout_seconds)
                logs: str = container.logs(stdout=True, stderr=True).decode(
                    errors="replace"
                )
                exit_code = result.get("StatusCode", 0)
                if exit_code != 0:
                    raise RuntimeError(
                        f"config-assist exited with code {exit_code}:\n{logs}"
                    )
                return logs
            except requests.exceptions.ReadTimeout:
                try:
                    container.kill()
                except Exception:
                    pass
                raise TimeoutError(f"config-assist timed out after {timeout_seconds}s")
            finally:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

        return await loop.run_in_executor(None, _run)

    async def stream_logs(
        self,
        service: ServiceRecord,
        tail: int = 100,
        since: str | None = None,
        follow: bool = False,
    ) -> AsyncIterator[bytes]:
        import docker

        loop = asyncio.get_running_loop()
        name = self._container_name(service)

        try:
            container = await self._get_container(name)
        except docker.errors.APIError as exc:
            yield f"[docker error: {exc}]\n".encode()
            return

        if container is None:
            yield b"[container not found]\n"
            return

        kwargs: dict[str, object] = {"stream": True, "follow": follow, "tail": tail}
        if since is not None:
            kwargs["since"] = since

        log_iter = None
        try:
            log_iter = await loop.run_in_executor(
                None, lambda: container.logs(**kwargs)
            )
            while True:

                def _next_chunk() -> tuple[bytes | None, bool]:
                    try:
                        return next(log_iter), False
                    except StopIteration:
                        return None, True

                chunk, exhausted = await loop.run_in_executor(None, _next_chunk)
                if exhausted:
                    break
                yield (
                    chunk
                    if isinstance(chunk, bytes)
                    else (chunk.encode() if chunk is not None else b"")
                )
        except asyncio.CancelledError:
            raise
        except docker.errors.APIError as exc:
            yield f"[docker error: {exc}]\n".encode()
        finally:
            if log_iter is not None:
                try:
                    log_iter.close()
                except Exception:
                    pass

    async def disk_df(self) -> DockerDfStats:
        import docker  # noqa: PLC0415

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, self._client.api.df)
        except docker.errors.APIError as exc:
            logger.warning("docker df failed: %s", exc)
            return DockerDfStats()
        images = result.get("Images") or []
        build_cache = result.get("BuildCache") or []
        # ``LayersSize`` is the de-duplicated total image disk (matches
        # ``docker system df``); summing each image's ``Size`` double-counts
        # shared layers and over-reports.
        images_size = result.get("LayersSize", 0) or sum(
            img.get("Size", 0) for img in images
        )
        # ``BuilderSize`` is the legacy pre-BuildKit builder cache (0 on modern
        # Docker); the real build cache is the sum of the ``BuildCache`` records.
        build_cache_size = sum(item.get("Size", 0) for item in build_cache)
        reclaimable = sum(
            item.get("Size", 0) for item in build_cache if not item.get("InUse", True)
        )
        # Per-volume sizes from ``docker system df`` (``Volumes`` carries
        # ``UsageData.Size`` and ``RefCount``); skip the -1 "unknown" sentinel.
        volumes = [
            VolumeStat(
                name=v.get("Name", ""),
                size_bytes=(v.get("UsageData") or {}).get("Size", 0),
                in_use=(v.get("UsageData") or {}).get("RefCount", 0) > 0,
            )
            for v in (result.get("Volumes") or [])
            if (v.get("UsageData") or {}).get("Size", 0) >= 0
        ]
        return DockerDfStats(
            images_size_bytes=images_size,
            build_cache_size_bytes=build_cache_size,
            build_cache_reclaimable_bytes=reclaimable,
            volumes=volumes,
        )

    async def prune_builds(self) -> int:
        """Call Docker builder prune API and return bytes reclaimed."""
        loop = asyncio.get_running_loop()
        # all=True prunes the full build cache (not just dangling) so the button
        # frees the reclaimable space the disk panel reports, not just a few KB.
        result = await loop.run_in_executor(
            None, lambda: self._client.api.prune_builds(all=True)
        )
        return int(result.get("SpaceReclaimed", 0))

    async def prune_images(self, protected_refs: set[str]) -> int:
        """Remove dangling (untagged) images one by one, skipping protected refs.

        Docker's bulk prune API has no exclusion list, so images are removed
        individually. An image is protected when its id or any of its repo
        digests appears in *protected_refs*; images still used by a container
        fail removal with a 409, which is swallowed.
        """
        import docker  # noqa: PLC0415

        loop = asyncio.get_running_loop()

        def _prune() -> int:
            reclaimed = 0
            try:
                dangling = self._client.images.list(filters={"dangling": True})
            except docker.errors.APIError as exc:
                logger.warning("image prune: list failed: %s", exc)
                return 0
            for img in dangling:
                digests = {
                    rd.split("@")[1]
                    for rd in img.attrs.get("RepoDigests", [])
                    if "@" in rd
                }
                if img.id in protected_refs or digests & protected_refs:
                    continue
                size = int(img.attrs.get("Size", 0))
                try:
                    self._client.images.remove(img.id)
                    reclaimed += size
                except docker.errors.APIError as exc:
                    logger.debug("image prune: skipped %s: %s", img.id, exc)
            return reclaimed

        return await loop.run_in_executor(None, _prune)

    async def _find_by_config_hostname(self, hostname: str) -> Any:
        """Find the running container whose ``Config.Hostname`` is *hostname*.

        After a watchtower self-update the recreated container keeps the
        previous container's hostname (its config is copied verbatim), so the
        hostname no longer matches the container id and the direct id lookup
        misses. Returns ``None`` when no running container matches.
        """
        import docker

        loop = asyncio.get_running_loop()

        def _scan() -> Any:
            for summary in self._client.containers.list():
                if not summary.id:
                    continue
                try:
                    full = self._client.containers.get(summary.id)
                except docker.errors.NotFound:
                    continue
                if (full.attrs.get("Config") or {}).get("Hostname") == hostname:
                    return full
            return None

        return await loop.run_in_executor(None, _scan)

    async def inspect_self(self) -> Optional[SelfInspect]:
        """Resolve the server's own container via the container-id hostname.

        Inside a container the default hostname is the short container id;
        after a watchtower self-update the hostname is the *previous*
        container's id, so a fallback scans for the container whose
        ``Config.Hostname`` matches. When both fail (custom hostname, not
        containerised, daemon unreachable) self-update is reported
        unsupported rather than raising.
        """
        import socket

        import docker

        hostname = socket.gethostname()
        try:
            container = await self._get_container(hostname)
            if container is None:
                container = await self._find_by_config_hostname(hostname)
        except docker.errors.APIError as exc:
            logger.warning("inspect_self: docker daemon unreachable: %s", exc)
            return None
        if container is None:
            return None

        attrs = container.attrs
        image_ref = (attrs.get("Config") or {}).get("Image", "")
        digest = ""
        try:
            repo_digests = (
                (container.image.attrs.get("RepoDigests") or [])
                if container.image
                else []
            )
            if repo_digests:
                digest = repo_digests[0].rsplit("@", 1)[-1]
        except docker.errors.APIError:
            pass
        networks = list(
            ((attrs.get("NetworkSettings") or {}).get("Networks") or {}).keys()
        )
        return SelfInspect(
            container_id=attrs.get("Id", ""),
            container_name=(attrs.get("Name") or "").lstrip("/"),
            image_ref=image_ref,
            running_digest=digest,
            networks=networks,
        )

    async def trigger_self_update(
        self,
        target: SelfInspect,
        watchtower_image: str,
        docker_host_url: str,
        docker_api_version: str,
    ) -> str:
        """Launch a one-shot watchtower container that updates *target*.

        Watchtower pulls the new image, then stops/removes the old container
        and recreates it with identical config — from outside this process,
        which is the only safe way for the server to replace itself. The
        watchtower container joins all of *target*'s networks so it reaches
        the socket proxy at *docker_host_url*, and auto-removes when done.

        ``DOCKER_API_VERSION`` must be exported: watchtower 1.7.1's client
        defaults to API 1.25, below modern daemons' minimum (1.44), and
        panics on the first API call without it. Recreating *target* also
        requires the socket proxy to allow the networks API (NETWORKS=1) —
        watchtower re-attaches the container's networks via
        ``/networks/{id}/connect``.
        """
        import docker

        loop = asyncio.get_running_loop()

        def _run() -> str:
            api = self._client.api
            self._client.images.pull(watchtower_image)
            networking = None
            if target.networks:
                # Multi-endpoint create keeps the watchtower container itself
                # off the /networks/*/connect API path.
                networking = api.create_networking_config(
                    {net: api.create_endpoint_config() for net in target.networks}
                )
            created = api.create_container(
                image=watchtower_image,
                command=["--run-once", "--cleanup", target.container_name],
                environment={
                    "DOCKER_HOST": docker_host_url,
                    "DOCKER_API_VERSION": docker_api_version,
                },
                host_config=api.create_host_config(auto_remove=True),
                networking_config=networking,
            )
            container_id: str = created["Id"]
            api.start(container_id)
            return container_id

        try:
            return await loop.run_in_executor(None, _run)
        except docker.errors.APIError as exc:
            raise RuntimeError(
                f"failed to launch self-update container: {exc}"
            ) from exc


async def collect_protected_image_refs(store: Any) -> set[str]:
    """Image ids/digests that must survive an image prune.

    Every record's deployed and previous digests are rollback targets;
    ``rollback`` recreates containers from a local image id, which Docker
    cannot re-pull, so pruning them would break rollback.
    """
    protected: set[str] = set()
    for record in await store.list_all():
        for ref in (
            record.deployed_image_digest,
            record.previous_image_digest,
            record.image_revision,
        ):
            if ref:
                protected.add(ref)
    return protected
