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
from .models import ComponentInspect, DeployOutcome, DockerDfStats, RollbackOutcome, ServiceRecord, ServiceState

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
    async def write_config_to_volume(self, volume_name: str, config_dict: dict[str, Any]) -> None:
        """Write *config_dict* as YAML into a Docker named volume."""
        ...

    @abstractmethod
    async def read_config_from_volume(self, volume_name: str) -> dict:
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

    async def deploy(self, service: ServiceRecord, config: "ComponentConfig", image_ref: str) -> DeployOutcome:
        return DeployOutcome(deployed_digest="sha256:noop", previous_digest="", state=ServiceState.RUNNING)

    async def rollback(self, service: ServiceRecord, config: "ComponentConfig") -> RollbackOutcome:
        return RollbackOutcome(deployed_digest=service.previous_image_digest or "sha256:noop", state=ServiceState.RUNNING)

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

    async def write_config_to_volume(self, volume_name: str, config_dict: dict[str, Any]) -> None:
        pass

    async def read_config_from_volume(self, volume_name: str) -> dict:
        return {}

    async def run_config_assist(
        self, image, command_str, volume_name, volume_mount_path, env_dict, timeout_seconds=60
    ) -> str:
        return "[noop backend]"


# ---------------------------------------------------------------------------
# Docker backend
# ---------------------------------------------------------------------------


class DockerBackend(ExecutionBackend):
    """Executes lifecycle actions via the local Docker daemon (``docker`` CLI)."""

    async def start(self, service: ServiceRecord) -> ServiceState:
        if not service.image:
            logger.warning("Service %r has no image — cannot start via Docker", service.name)
            return ServiceState.FAILED

        # Try `docker start` (container may already exist), fall back to `docker run`.
        rc, _, stderr = await _run(
            "docker", "start", service.name,
        )
        if rc == 0:
            return ServiceState.RUNNING

        # Container may not exist — create and start it.
        rc, _, stderr = await _run(
            "docker", "run", "-d", "--name", service.name, service.image,
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
        state = await self._inspect_state(service.container_name or service.name) or ServiceState.UNKNOWN
        return ComponentInspect(state=state)

    async def deploy(self, service: ServiceRecord, config: "ComponentConfig", image_ref: str) -> DeployOutcome:
        raise NotImplementedError("deploy not supported for DockerBackend — use DockerSdkBackend")

    async def rollback(self, service: ServiceRecord, config: "ComponentConfig") -> RollbackOutcome:
        raise NotImplementedError("rollback not supported for DockerBackend — use DockerSdkBackend")

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

    async def write_config_to_volume(self, volume_name: str, config_dict: dict[str, Any]) -> None:
        raise NotImplementedError(
            "write_config_to_volume not supported for DockerBackend — use DockerSdkBackend"
        )

    async def read_config_from_volume(self, volume_name: str) -> dict:
        raise NotImplementedError(
            "read_config_from_volume not supported for DockerBackend — use DockerSdkBackend"
        )

    async def run_config_assist(
        self, image, command_str, volume_name, volume_mount_path, env_dict, timeout_seconds=60
    ) -> str:
        raise NotImplementedError(
            "run_config_assist not supported for DockerBackend — use DockerSdkBackend"
        )

    async def _inspect_state(self, container_name: str) -> Optional[ServiceState]:
        """Map ``docker inspect`` output to a ``ServiceState``."""
        rc, stdout, _stderr = await _run(
            "docker", "inspect", "-f", "{{.State.Status}}", container_name,
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
            proc.communicate(), timeout=timeout,
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
    ) -> None:
        import docker

        self._client = docker.DockerClient(base_url=socket_url)
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
                None, self._client.containers.get, name,
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
                    "org.opencontainers.image.revision", "",
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
                state=state, image_revision=revision, health=health,
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
        ports: dict = {}
        volumes = {
            m.host: {"bind": m.container, "mode": "ro" if m.read_only else "rw"}
            for m in config.mounts
        }
        if config.claude_mount:
            import os
            claude_host = self._claude_host_mount_path or os.path.expanduser("~/.claude")
            volumes[claude_host] = {"bind": "/root/.claude", "mode": "rw"}
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
            restart_policy={"Name": "unless-stopped"},  # type: ignore[arg-type]
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
                return h["Status"] if h else "healthy"  # no healthcheck → treat as healthy

            status = await loop.run_in_executor(None, _poll)
            if status == "healthy":
                return
            if status == "unhealthy":
                raise RuntimeError(f"Container {name} is unhealthy after deploy")
            await asyncio.sleep(2)
        logger.warning("Health wait timed out for %s after %.0fs — proceeding", name, timeout)

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
        repo_without_tag = image_ref.rsplit(':', 1)[0]
        repo_digests = image.attrs.get('RepoDigests', [])
        new_digest: str = next(
            (rd.split('@')[1] for rd in repo_digests if rd.startswith(repo_without_tag + '@')),
            image.id or "",
        )

        # Step 2 — snapshot current container's image digest (for rollback)
        prior_digest = ""
        existing = await self._get_container(name)
        if existing is not None:
            try:
                prior_digest = await loop.run_in_executor(None, lambda: existing.image.id)
            except Exception:
                pass

        # Step 3 — stop + remove old container (if present)
        if existing is not None:
            try:
                await loop.run_in_executor(None, lambda: self._stop_and_remove(existing))
            except docker.errors.APIError as exc:
                raise RuntimeError(f"Failed to remove existing container {name!r}: {exc}") from exc

        # Step 4 — create + start new container
        try:
            # Pre-create named volumes
            for vol_name in config.named_volumes:
                try:
                    await loop.run_in_executor(None, self._client.volumes.create, vol_name)
                except docker.errors.APIError as exc:
                    if exc.status_code == 409:
                        logger.info("Volume %s already exists, skipping creation", vol_name)
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
                    name, prior_digest,
                )
                try:
                    restore = await loop.run_in_executor(
                        None, lambda: self._create_container(config, prior_digest)
                    )
                    await loop.run_in_executor(None, restore.start)
                    logger.info("Restored %s from prior digest %s", name, prior_digest)
                except Exception as restore_exc:
                    logger.error("Restore of %s also failed: %s", name, restore_exc)
            raise RuntimeError(f"Container create/start failed for {name!r}: {exc}") from exc

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
        target_digest = service.previous_image_digest  # guaranteed non-empty by server layer
        loop = asyncio.get_running_loop()

        # Stop + remove current container
        existing = await self._get_container(name)
        if existing is not None:
            try:
                await loop.run_in_executor(None, lambda: self._stop_and_remove(existing))
            except docker.errors.APIError as exc:
                raise RuntimeError(f"Failed to remove container {name!r} for rollback: {exc}") from exc

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

        return RollbackOutcome(deployed_digest=target_digest, state=ServiceState.RUNNING)

    async def write_config_to_volume(self, volume_name: str, config_dict: dict[str, Any]) -> None:
        """Write *config_dict* as YAML into a Docker named volume via a
        temporary busybox container.

        The volume **must** already exist; this method only writes to it.
        """
        import base64

        import docker
        import yaml

        yaml_content = yaml.dump(config_dict, default_flow_style=False, allow_unicode=True)
        encoded = base64.b64encode(yaml_content.encode()).decode()
        # base64 output contains only [A-Za-z0-9+/=] — safe to interpolate in sh without quoting
        cmd = f"mkdir -p /config && echo {encoded} | base64 -d > /config/config.yaml"
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
                raise RuntimeError(f"write_config_to_volume failed for {volume_name}: {exc}") from exc

        await loop.run_in_executor(None, _run)

    async def read_config_from_volume(self, volume_name: str) -> dict:
        """Read /config/config.yaml from a named volume via a temporary busybox container."""
        import yaml
        loop = asyncio.get_running_loop()

        def _run() -> dict:
            import docker
            try:
                raw = self._client.containers.run(
                    "busybox",
                    command=["sh", "-c", "cat /config/config.yaml 2>/dev/null || true"],
                    volumes={volume_name: {"bind": "/config", "mode": "ro"}},
                    remove=True,
                )
                text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
                return yaml.safe_load(text) or {}
            except docker.errors.APIError as exc:
                raise RuntimeError(
                    f"read_config_from_volume failed for {volume_name}: {exc}"
                ) from exc

        return await loop.run_in_executor(None, _run)

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
                logs: str = container.logs(
                    stdout=True, stderr=True
                ).decode(errors="replace")
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
                raise TimeoutError(
                    f"config-assist timed out after {timeout_seconds}s"
                )
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
                yield chunk if isinstance(chunk, bytes) else (chunk.encode() if chunk is not None else b"")
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
        images_size = sum(img.get("Size", 0) for img in images)
        builder_size = result.get("BuilderSize", 0)
        reclaimable = sum(
            item.get("Size", 0)
            for item in build_cache
            if not item.get("InUse", True)
        )
        return DockerDfStats(
            images_size_bytes=images_size,
            build_cache_size_bytes=builder_size,
            build_cache_reclaimable_bytes=reclaimable,
        )
