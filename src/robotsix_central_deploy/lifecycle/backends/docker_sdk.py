"""Docker SDK backend — executes lifecycle actions via the Docker Python SDK."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from typing import TYPE_CHECKING, Any

from ..._ghcr_auth import GhcrCredentialResolver
from ...registry.constants import PROXY_NETWORK
from ...registry.traefik_labels import traefik_labels
from ..models import (
    ComponentInspect,
    HealthStatus,
    ServiceRecord,
    ServiceState,
)
from ._auth_ops import CLAUDE_AUTH_VOLUME, AuthOps
from ._self_mgmt_ops import SelfMgmtDelegationMixin, SelfMgmtOps
from ._util import (
    docker_status_to_service_state,
)
from ._volume_ops import VolumeOps
from .base import ExecutionBackend
from .docker_sdk_deploy import DeployOpsMixin
from .docker_sdk_logs import LogOpsMixin
from .docker_sdk_maintenance import MaintenanceOpsMixin

if TYPE_CHECKING:
    from ...registry.models import ComponentConfig

logger = logging.getLogger(__name__)


class DockerSdkBackend(
    DeployOpsMixin,
    LogOpsMixin,
    MaintenanceOpsMixin,
    SelfMgmtDelegationMixin,
    ExecutionBackend,
):
    """Executes lifecycle actions via the Docker Python SDK against the local socket.

    Deploy/rollback, log streaming, disk maintenance and self-management live
    in the mixins composed here; this module keeps construction, container
    lifecycle, diagnostics and the volume/auth/config-assist passthroughs.
    """

    def __init__(
        self,
        socket_url: str = "unix:///var/run/docker.sock",
        timeout: int = 120,
        github_app_id: str = "",
        github_app_private_key: str = "",
        installation_id: str = "",
        ghcr_pull_token: str = "",
        gateway_base_domain: str = "",
        remote_bind_ip: str = "",
    ) -> None:
        import docker

        self._gateway_base_domain = gateway_base_domain
        # Non-empty marks a REMOTE-host backend: the daemon at socket_url is
        # not the one Traefik watches, so containers publish their ports on
        # this address (the remote host's tunnel IP) instead of joining the
        # proxy network and carrying routing labels.
        self._remote_bind_ip = remote_bind_ip
        self._client = docker.DockerClient(base_url=socket_url, timeout=timeout)
        self._auth = AuthOps(self._client)
        self._ghcr_credentials = GhcrCredentialResolver(
            github_app_id=github_app_id,
            github_app_private_key=github_app_private_key,
            installation_id=installation_id,
            ghcr_pull_token=ghcr_pull_token,
        )
        self._volume = VolumeOps(self._client)
        self._self_mgmt = SelfMgmtOps(
            self._client, self._get_container, self._build_auth_configs
        )

    @property
    def ghcr_credentials(self) -> GhcrCredentialResolver:
        """The shared GHCR credential resolver, also used by the update check."""
        return self._ghcr_credentials

    @property
    def ghcr_pull_token(self) -> str:
        """The fleet-wide read:packages PAT for private GHCR pulls."""
        return self._ghcr_credentials.pull_token

    @ghcr_pull_token.setter
    def ghcr_pull_token(self, value: str) -> None:
        self._ghcr_credentials.pull_token = value

    # -- helpers ------------------------------------------------------------

    def _container_name(self, service: ServiceRecord) -> str:
        return service.container_name if service.container_name else service.name

    @staticmethod
    def _state_from_docker(status: str) -> ServiceState:
        return docker_status_to_service_state(status)

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
        """Return a ComponentInspect with state, image revision, health status, and running digest."""
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
            except Exception:  # Gracefully degrade; label stays empty  # noqa: BLE001
                pass

            # health check result
            health = ""
            try:
                health_obj = container.attrs["State"].get("Health")
                if health_obj:
                    health = health_obj.get("Status", "")
            except Exception:  # Gracefully degrade; health stays empty  # noqa: BLE001
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
            except Exception:  # noqa: BLE001
                pass  # Gracefully degrade; digest stays ""

            return ComponentInspect(
                state=state,
                image_revision=revision,
                health=health,
                running_digest=running_digest,
            )

        return await loop.run_in_executor(None, _inspect)

    async def start(self, service: ServiceRecord) -> ServiceState:
        """Start the container for *service*. Returns RUNNING on success, FAILED otherwise."""
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
        """Stop the container for *service*. Returns STOPPED on success (or if already gone), FAILED otherwise."""
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
        """Remove the managed container for *service* (best-effort, already stopped)."""
        import docker

        loop = asyncio.get_running_loop()
        name = self._container_name(service)
        container = await self._get_container(name)
        if container is None:
            return
        try:
            await loop.run_in_executor(None, lambda: container.remove(force=True))
        except docker.errors.NotFound:  # Container already removed
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("remove_container %s: %s", name, exc)

    async def restart(self, service: ServiceRecord) -> ServiceState:
        """Restart the container for *service*. Returns RUNNING on success, FAILED otherwise."""
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

    def _create_container(self, config: ComponentConfig, image_ref: str) -> Any:
        """Create a Docker container from a ComponentConfig spec (synchronous)."""

        # Containers that mount the host Docker socket must keep their
        # image's default user: the socket is root:docker on the host, and
        # forcing the non-root default uid locks them out (haproxy in the
        # tecnativa socket-proxy got EACCES on every request and answered
        # 503 — took down mill's sandboxes on 2026-07-04).
        if config.user:
            user = config.user
        elif config.host_docker_sock:
            user = None
        else:
            user = f"{os.getuid()}:{os.getgid()}"

        # Host ports are intentionally NOT published locally: the Traefik edge
        # reaches managed containers over the central-deploy-proxy network by
        # container_name:container_port. Publishing host ports caused "port is
        # already allocated" conflicts with existing host-bound services.
        # On a REMOTE host the proxy network does not exist — the edge dials
        # ports published on the host's tunnel address instead, so bind them
        # there (and only there: never on the remote host's LAN interfaces).
        ports: dict[str, Any] = {}
        if self._remote_bind_ip:
            ports = {
                f"{p.container}/{p.protocol}": (self._remote_bind_ip, p.host)
                for p in config.ports
            }
        volumes = {
            m.host: {"bind": m.container, "mode": "ro" if m.read_only else "rw"}
            for m in config.mounts
        }
        if config.claude_mount:
            # NOTE: the dict is keyed by volume name, so an explicit
            # config.mounts entry for CLAUDE_AUTH_VOLUME would be silently
            # clobbered here — claude_mount_path is the supported way to
            # relocate the credentials (it must match the image user's
            # $HOME/.claude; mill runs as `mill`, not `app`).
            volumes[CLAUDE_AUTH_VOLUME] = {
                "bind": config.claude_mount_path,
                "mode": "rw",
            }
        if config.host_docker_sock:
            volumes["/var/run/docker.sock"] = {
                "bind": "/var/run/docker.sock",
                "mode": "ro",
            }
        healthcheck: dict[str, Any] | None = None
        if config.health_check:
            hc = config.health_check
            if hc.disable:
                healthcheck = {"Test": ["NONE"]}
            else:
                healthcheck = {
                    "Test": hc.test,
                    "Interval": hc.interval_seconds * int(1e9),
                    "Timeout": hc.timeout_seconds * int(1e9),
                    "Retries": hc.retries,
                    "StartPeriod": hc.start_period_seconds * int(1e9),
                }
        # Routing lives on the container, not in central-deploy: Traefik watches
        # the Docker API and picks these up with no reload. A component with no
        # port (or an unconfigured base domain) gets none and Traefik ignores it.
        # Remote-host containers get no labels (the edge cannot see that
        # daemon) and no proxy network (it does not exist there) — their
        # routing is a file-provider fragment written by the deploy path.
        if self._remote_bind_ip:
            labels: dict[str, str] = {}
            network: str | None = None
        else:
            labels = traefik_labels(config, self._gateway_base_domain, PROXY_NETWORK)
            network = PROXY_NETWORK

        return self._client.containers.create(
            image=image_ref,
            name=config.container_name,
            command=config.command,
            entrypoint=config.entrypoint,
            environment=config.env,
            volumes=volumes,
            healthcheck=healthcheck,
            ports=ports,
            labels=labels,
            tmpfs={p: "" for p in config.tmpfs} if config.tmpfs else None,
            detach=True,
            user=user,
            restart_policy={"Name": "unless-stopped"},  # type: ignore[arg-type]  # types-docker stubs are incomplete for restart policy names
            network=network,
            mem_limit=config.mem_limit,
            memswap_limit=config.memswap_limit,
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
                container.reload()  # noqa: B023
                h = container.attrs["State"].get("Health")  # noqa: B023
                return (
                    h["Status"] if h else HealthStatus.HEALTHY
                )  # no healthcheck → treat as healthy

            status = await loop.run_in_executor(None, _poll)
            if status == HealthStatus.HEALTHY:
                return
            if status == HealthStatus.UNHEALTHY:
                # Capture healthcheck failure detail for actionable error message.
                detail_parts: list[str] = []
                try:
                    h = container.attrs["State"].get("Health") or {}
                    failing_streak = h.get("FailingStreak", 0)
                    detail_parts.append(f"failing streak: {failing_streak}")
                    log_entries = h.get("Log", []) or []
                    if log_entries:
                        last = log_entries[-1]
                        exit_code = last.get("ExitCode", "?")
                        output = (last.get("Output") or "").strip()
                        detail_parts.append(f"last check: exit code {exit_code}")
                        if output:
                            detail_parts.append(f"output: {output[:500]}")
                except Exception:  # noqa: BLE001
                    # Best-effort operation — failure is non-critical here.
                    pass
                detail = (
                    "; ".join(detail_parts)
                    if detail_parts
                    else "no healthcheck detail available"
                )
                raise RuntimeError(
                    f"Container {name} is unhealthy after deploy ({detail})"
                )
            await asyncio.sleep(2)
        logger.warning(
            "Health wait timed out for %s after %.0fs — proceeding", name, timeout
        )

    def _stop_and_remove(self, container: Any) -> None:
        """Stop and force-remove a container (synchronous, best-effort stop)."""
        try:
            container.stop(timeout=30)
        except Exception:  # Best-effort stop; proceed to force-remove  # noqa: BLE001
            pass
        container.remove(force=True)

    # -- claude-auth API (delegated to AuthOps) -----------------------------

    async def check_claude_auth(self, volume_name: str) -> dict[str, Any]:
        """Check whether *volume_name* holds valid Claude credentials."""
        return await self._auth.check_claude_auth(volume_name)

    async def write_claude_credentials(
        self, volume_name: str, credentials_json: str
    ) -> dict[str, Any]:
        """Write *credentials_json* into *volume_name* as ``.credentials.json``."""
        return await self._auth.write_claude_credentials(volume_name, credentials_json)

    async def read_claude_credentials(self, volume_name: str) -> dict[str, Any]:
        """Read and return the parsed ``.credentials.json`` from *volume_name*."""
        return await self._auth.read_claude_credentials(volume_name)

    async def get_container_diagnostics(self, service: ServiceRecord) -> dict[str, Any]:
        """Return diagnostic information about the running container.

        Returns a dict with keys: ``exists``, ``labels``, ``networks``,
        ``state``, ``health``, ``started_at``, ``restart_count``,
        ``exit_code``, ``image_digest``.  When the container is not found,
        only ``exists=False`` is returned.
        """
        name = self._container_name(service)
        container = await self._get_container(name)
        if container is None:
            return {"exists": False}

        loop = asyncio.get_running_loop()

        def _inspect() -> dict[str, Any]:
            container.reload()
            attrs = container.attrs
            state_obj = attrs.get("State", {})
            networks_raw = attrs.get("NetworkSettings", {}).get("Networks", {})
            networks: dict[str, Any] = {}
            for net_name, net_cfg in networks_raw.items():
                networks[net_name] = {
                    "aliases": net_cfg.get("Aliases") or [],
                    "ip": net_cfg.get("IPAddress", ""),
                }

            health = ""
            health_obj = state_obj.get("Health")
            if health_obj:
                health = health_obj.get("Status", "")

            image_digest = ""
            try:
                image_id = attrs.get("Image", "")
                if image_id:
                    img = self._client.images.get(image_id)
                    repo_digests = img.attrs.get("RepoDigests", [])
                    prefix = service.image.rsplit(":", 1)[0] + "@"
                    for rd in repo_digests:
                        if rd.startswith(prefix):
                            image_digest = rd.split("@", 1)[1]
                            break
                    if not image_digest:
                        for rd in repo_digests:
                            if "@sha256:" in rd:
                                image_digest = rd.split("@", 1)[1]
                                break
            except Exception:  # noqa: BLE001
                pass

            return {
                "exists": True,
                "labels": attrs.get("Config", {}).get("Labels", {}),
                "networks": networks,
                "state": state_obj.get("Status", ""),
                "health": health,
                "started_at": state_obj.get("StartedAt", ""),
                "restart_count": attrs.get("RestartCount", 0),
                "exit_code": state_obj.get("ExitCode", 0),
                "image_digest": image_digest,
            }

        return await loop.run_in_executor(None, _inspect)

    async def _remove_old_container(self, name: str, existing: Any) -> str:
        """Stop + remove *existing* container, returning its prior image digest."""
        import docker

        loop = asyncio.get_running_loop()
        prior_digest = ""
        try:
            prior_digest = await loop.run_in_executor(None, lambda: existing.image.id)
        except (
            Exception  # noqa: BLE001
        ):  # Gracefully degrade; prior_digest stays empty
            pass

        try:
            await loop.run_in_executor(None, lambda: self._stop_and_remove(existing))
        except docker.errors.APIError as exc:
            raise RuntimeError(
                f"Failed to remove existing container {name!r}: {exc}"
            ) from exc
        return prior_digest

    # -- config volume helpers (delegated to VolumeOps) ---------------------

    async def write_config_to_volume(
        self, volume_name: str, config_dict: dict[str, Any]
    ) -> None:
        """Write *config_dict* as JSON into a Docker named volume."""
        await self._volume.write_config_to_volume(volume_name, config_dict)

    async def write_llmio_tier_config_to_volume(
        self, volume_name: str, tier_config: dict[str, Any]
    ) -> None:
        """Write *tier_config* as ``llmio_tier_config.json`` into a Docker named volume."""
        await self._volume.write_llmio_tier_config_to_volume(volume_name, tier_config)

    async def read_config_from_volume(self, volume_name: str) -> dict[str, Any]:
        """Read /config/config.json from a named volume."""
        return await self._volume.read_config_from_volume(volume_name)

    # -- volume inspection (delegated to VolumeOps) -------------------------

    async def prune_volume_files(
        self,
        volume_name: str,
        rel_path: str,
        glob: str,
        max_age_days: int,
    ) -> dict[str, int]:
        """See :meth:`VolumeOps.prune_volume_files`."""
        return await self._volume.prune_volume_files(
            volume_name, rel_path, glob, max_age_days
        )

    async def remove_stale_helpers(self) -> int:
        """See :meth:`VolumeOps.remove_stale_helpers`."""
        return await self._volume.remove_stale_helpers()

    async def measure_volume_bytes(self, volume_name: str) -> int | None:
        """Return effective total bytes for *volume_name*, excluding SQLite sidecars."""
        return await self._volume.measure_volume_bytes(volume_name)

    async def list_volume_dir(
        self, volume_name: str, rel_path: str
    ) -> list[dict[str, Any]]:
        """List immediate children of /vol/<rel_path>."""
        return await self._volume.list_volume_dir(volume_name, rel_path)

    async def read_volume_file(
        self, volume_name: str, rel_path: str, max_bytes: int
    ) -> dict[str, Any]:
        """Read ``/vol/<rel_path>`` via a one-shot busybox container."""
        return await self._volume.read_volume_file(volume_name, rel_path, max_bytes)

    async def write_volume_file(
        self,
        volume_name: str,
        rel_path: str,
        content: str,
        overwrite: bool,
    ) -> dict[str, Any]:
        """Create-or-overwrite ``/vol/<rel_path>`` via a one-shot busybox container."""
        return await self._volume.write_volume_file(
            volume_name, rel_path, content, overwrite
        )

    async def remove_volume(self, volume_name: str) -> None:
        """Remove the Docker named volume *volume_name* (best-effort)."""
        await self._volume.remove_volume(volume_name)

    async def relocate_volume(
        self,
        volume_name: str,
        target_disk_path: str,
        container_user: str | None = None,
    ) -> dict[str, Any]:
        """Relocate a named volume's data to *target_disk_path*."""
        return await self._volume.relocate_volume(
            volume_name, target_disk_path, container_user
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
                except (
                    Exception  # noqa: BLE001
                ):  # Best-effort kill; container may already be gone
                    pass
                raise TimeoutError(f"config-assist timed out after {timeout_seconds}s")
            finally:
                try:
                    container.remove(force=True)
                except Exception:  # noqa: BLE001
                    # Best-effort operation — failure is non-critical here.
                    pass

        return await loop.run_in_executor(None, _run)
