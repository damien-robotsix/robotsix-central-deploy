"""Docker SDK backend — executes lifecycle actions via the Docker Python SDK."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlparse

from ._auth_ops import CLAUDE_AUTH_VOLUME, AuthOps
from ._util import (
    docker_status_to_service_state,
    inflight_image_refs,
    register_inflight_image_refs,
    release_inflight_image_refs,
)
from ._volume_ops import VolumeOps
from .base import ExecutionBackend
from ...gateway.proxy import PROXY_NETWORK
from ..models import (
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
    from ...registry.models import ComponentConfig

logger = logging.getLogger(__name__)


def _image_registry_host(image_ref: str) -> str | None:
    """Return the registry host from an image reference, or *None*.

    Handles standard Docker image refs (``registry/owner/repo:tag``) and
    malformed refs that include a URL scheme.
    """
    # If the ref contains :// it's a URL — parse it properly.
    if "://" in image_ref:
        return urlparse(image_ref).hostname
    # Standard Docker image ref: host/rest
    return image_ref.split("/")[0] if "/" in image_ref else None


class DockerSdkBackend(ExecutionBackend):
    """Executes lifecycle actions via the Docker Python SDK against the local socket."""

    def __init__(
        self,
        socket_url: str = "unix:///var/run/docker.sock",
        timeout: int = 120,
        ghcr_token: str = "",
    ) -> None:
        import docker

        self._client = docker.DockerClient(base_url=socket_url, timeout=timeout)
        self._auth = AuthOps(self._client)
        self._ghcr_token = ghcr_token.strip()
        self._volume = VolumeOps(self._client)

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
            except Exception:  # Gracefully degrade; label stays empty
                pass

            # health check result
            health = ""
            try:
                health_obj = container.attrs["State"].get("Health")
                if health_obj:
                    health = health_obj.get("Status", "")
            except Exception:  # Gracefully degrade; health stays empty
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
        except Exception as exc:
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

    def _create_container(self, config: "ComponentConfig", image_ref: str) -> Any:
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
            tmpfs={p: "" for p in config.tmpfs} if config.tmpfs else None,
            detach=True,
            user=user,
            restart_policy={"Name": "unless-stopped"},  # type: ignore[arg-type]  # types-docker stubs are incomplete for restart policy names
            network=PROXY_NETWORK,
            mem_limit=config.mem_limit,
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
                except Exception:
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
        except Exception:  # Best-effort stop; proceed to force-remove
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

    async def _remove_old_container(self, name: str, existing: Any) -> str:
        """Stop + remove *existing* container, returning its prior image digest."""
        import docker

        loop = asyncio.get_running_loop()
        prior_digest = ""
        try:
            prior_digest = await loop.run_in_executor(None, lambda: existing.image.id)
        except Exception:  # Gracefully degrade; prior_digest stays empty
            pass

        try:
            await loop.run_in_executor(None, lambda: self._stop_and_remove(existing))
        except docker.errors.APIError as exc:
            raise RuntimeError(
                f"Failed to remove existing container {name!r}: {exc}"
            ) from exc
        return prior_digest

    async def _prepare_volumes(self, config: "ComponentConfig") -> list[str]:
        """Pre-create named volumes and validate claude credentials.

        Returns deploy warnings collected during credential validation.
        """
        import docker

        loop = asyncio.get_running_loop()
        deploy_warnings: list[str] = []

        # Determine container user for volume ownership
        container_user = config.user or f"{os.getuid()}:{os.getgid()}"
        chown_uid, chown_gid = self._volume.resolve_user_to_uid_gid(container_user)

        # Pre-create named volumes (including claude-auth when needed)
        volumes_to_create: list[str] = list(config.named_volumes)
        if config.claude_mount:
            volumes_to_create.append(CLAUDE_AUTH_VOLUME)

        for vol_name in volumes_to_create:
            try:
                await loop.run_in_executor(None, self._client.volumes.create, vol_name)
            except docker.errors.APIError as exc:
                if exc.status_code == 409:
                    logger.info("Volume %s already exists, skipping creation", vol_name)
                    continue
                raise RuntimeError(
                    f"Failed to create volume {vol_name!r}: {exc.explanation or exc}"
                ) from exc
            except docker.errors.DockerException as exc:
                raise RuntimeError(
                    f"Docker daemon unreachable while creating volume {vol_name!r}: {exc}"
                ) from exc

            # Freshly-created volume — fix ownership so the container
            # user can write to it.
            vol_mode = 0o700 if vol_name == CLAUDE_AUTH_VOLUME else 0o755
            await loop.run_in_executor(
                None,
                self._volume.ensure_volume_ownership,
                vol_name,
                chown_uid,
                chown_gid,
                vol_mode,
            )

        # Validate claude credentials (non-fatal)
        if config.claude_mount:
            try:
                cred_warnings = await loop.run_in_executor(
                    None, self._auth.check_claude_credentials
                )
                if cred_warnings:
                    deploy_warnings.extend(cred_warnings)
                    for w in cred_warnings:
                        logger.warning(w)
            except Exception as exc:
                logger.warning(
                    "claude-auth credential check failed (non-fatal): %s", exc
                )

        return deploy_warnings

    async def _try_restore(
        self, name: str, config: "ComponentConfig", prior_digest: str
    ) -> None:
        """Best-effort restore of a container from *prior_digest* after a failed deploy."""
        if not prior_digest:
            return

        loop = asyncio.get_running_loop()
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

    def _build_auth_config(self, image_ref: str) -> dict[str, str] | None:
        """Return an auth config dict for *image_ref*, or *None* for anonymous pull.

        Only ``ghcr.io`` images are authenticated.  The token is read from
        ``LifecycleConfig.ghcr_token`` (set in ``config/config.json``); when
        it is absent or empty, anonymous pull is used and a 401 on a private
        image will surface a diagnostic error.
        """
        if _image_registry_host(image_ref) != "ghcr.io":
            return None
        if not self._ghcr_token:
            return None
        # GHCR ignores the username field when a personal access token is
        # supplied as the password — any non-empty string works here.
        return {
            "username": "USERNAME",
            "password": self._ghcr_token,
            "serveraddress": "ghcr.io",
        }

    async def deploy(
        self, service: ServiceRecord, config: "ComponentConfig", image_ref: str
    ) -> DeployOutcome:
        """Pull *image_ref*, recreate the container from *config*, return outcome."""
        import docker

        name = self._container_name(service)
        loop = asyncio.get_running_loop()

        # Step 1 — pull target image; obtain its digest
        auth_config = self._build_auth_config(image_ref)
        try:
            image = await loop.run_in_executor(
                None,
                lambda: self._client.images.pull(image_ref, auth_config=auth_config),
            )
        except docker.errors.APIError as exc:
            response = getattr(exc, "response", None)
            if (
                response is not None
                and response.status_code == 401
                and _image_registry_host(image_ref) == "ghcr.io"
                and not auth_config
            ):
                raise RuntimeError(
                    f"Image pull failed for {image_ref!r}: received 401 Unauthorized "
                    "from ghcr.io. Set ghcr_token in config/config.json to a GitHub "
                    "personal access token with read:packages scope."
                ) from exc
            raise RuntimeError(f"Image pull failed for {image_ref!r}: {exc}") from exc
        # Derive manifest digest from RepoDigests (comparable to registry
        # Docker-Content-Digest header), falling back to config digest.
        # Strip a digest suffix first (repo@sha256:… — the caretaker deploys
        # pinned refs), then the tag, so RepoDigests matching works for both.
        repo_without_tag = image_ref.split("@", 1)[0].rsplit(":", 1)[0]
        repo_digests = image.attrs.get("RepoDigests", [])
        new_digest: str = next(
            (
                rd.split("@")[1]
                for rd in repo_digests
                if rd.startswith(repo_without_tag + "@")
            ),
            image.id or "",
        )

        # The pulled image stays untagged (dangling) until its container
        # exists — shield it from concurrent prunes until the deploy ends.
        inflight_refs = {ref for ref in (image.id, new_digest) if ref}
        register_inflight_image_refs(inflight_refs)
        try:
            # Step 2 — snapshot + remove old container (if present)
            prior_digest = ""
            existing = await self._get_container(name)
            if existing is not None:
                prior_digest = await self._remove_old_container(name, existing)

            # Step 3 — create + start new container
            deploy_warnings: list[str] = []
            try:
                deploy_warnings = await self._prepare_volumes(config)

                new_container = await loop.run_in_executor(
                    None, lambda: self._create_container(config, image_ref)
                )
                await loop.run_in_executor(None, new_container.start)
            except Exception as exc:
                await self._try_restore(name, config, prior_digest)
                raise RuntimeError(
                    f"Container create/start failed for {name!r}: {exc}"
                ) from exc

            # Step 4 — health wait (if configured)
            if config.health_check:
                await self._wait_healthy(name, timeout=60.0)
        finally:
            release_inflight_image_refs(inflight_refs)

        return DeployOutcome(
            deployed_digest=new_digest,
            previous_digest=prior_digest,
            state=ServiceState.RUNNING,
            warnings=deploy_warnings,
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

        rollback_warnings: list[str] = []

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
            # Validate claude credentials (non-fatal)
            if config.claude_mount:
                try:
                    cred_warnings = await loop.run_in_executor(
                        None, self._auth.check_claude_credentials
                    )
                    if cred_warnings:
                        rollback_warnings.extend(cred_warnings)
                        for w in cred_warnings:
                            logger.warning(w)
                except Exception as exc:
                    logger.warning(
                        "claude-auth credential check failed during rollback (non-fatal): %s",
                        exc,
                    )

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
            deployed_digest=target_digest,
            state=ServiceState.RUNNING,
            warnings=rollback_warnings,
        )

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

    async def measure_volume_bytes(self, volume_name: str) -> int:
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

    async def remove_volume(self, volume_name: str) -> None:
        """Remove the Docker named volume *volume_name* (best-effort)."""
        await self._volume.remove_volume(volume_name)

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
                except Exception:  # Best-effort kill; container may already be gone
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
        """Stream container logs. Returns an async iterator of text chunks."""
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
                except (
                    Exception
                ):  # Best-effort close; iterator may already be exhausted
                    pass

    async def get_container_logs(
        self,
        service: ServiceRecord,
        tail: int = 200,
    ) -> str:
        """Return the last *tail* lines of a container's logs as a string.

        Returns an empty string if the container is not found or an error
        occurs.
        """
        import docker  # noqa: PLC0415

        loop = asyncio.get_running_loop()
        name = self._container_name(service)

        try:
            container = await self._get_container(name)
        except docker.errors.APIError:
            return ""

        if container is None:
            return ""

        try:
            raw = await loop.run_in_executor(
                None,
                lambda: container.logs(stdout=True, stderr=True, tail=tail),
            )
            return raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        except Exception:
            logger.warning(
                "get_container_logs: failed to read logs for %s", name, exc_info=True
            )
            return ""

    async def disk_df(self) -> DockerDfStats:
        """Return Docker disk usage statistics."""
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
        # Untagged (dangling) images — candidates for /disk/reclaim. Sums
        # per-image ``Size`` so shared layers may be double-counted; this is
        # an indicator, not an exact reclaim prediction.
        dangling_size = sum(
            img.get("Size", 0)
            for img in images
            if not [t for t in (img.get("RepoTags") or []) if t != "<none>:<none>"]
        )
        return DockerDfStats(
            images_size_bytes=images_size,
            dangling_images_bytes=dangling_size,
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
                # Re-check in-flight refs per image: *protected_refs* is a
                # snapshot from before this loop, but a deploy may pull (and
                # register) an image while the prune is running.
                live_refs = protected_refs | inflight_image_refs()
                if img.id in live_refs or digests & live_refs:
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
        except docker.errors.APIError:  # Gracefully degrade; digest stays empty
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
            self._client.images.pull(
                watchtower_image,
                auth_config=self._build_auth_config(watchtower_image),
            )
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

    async def trigger_self_restart(self, target: SelfInspect) -> str:
        """Restart the container identified by *target* via the Docker API.

        The Docker daemon accepts the restart command and returns
        immediately, then sends SIGTERM to the container asynchronously.
        This allows the HTTP response to flush before the process is
        killed.
        """
        import docker

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self._client.api.restart(target.container_id, timeout=10)
            )
        except docker.errors.APIError as exc:
            raise RuntimeError(f"failed to restart self container: {exc}") from exc
        return target.container_id
