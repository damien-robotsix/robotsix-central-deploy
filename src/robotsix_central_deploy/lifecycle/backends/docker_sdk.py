"""Docker SDK backend — executes lifecycle actions via the Docker Python SDK."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..._ghcr_auth import GHCR_HOST, GhcrCredentialResolver
from ...registry.constants import PROXY_NETWORK
from ...registry.traefik_labels import traefik_labels
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
from ._auth_ops import CLAUDE_AUTH_VOLUME, AuthOps
from ._self_mgmt_ops import SelfMgmtOps
from ._util import (
    PruneImagesResult,
    docker_status_to_service_state,
    inflight_image_refs,
    register_inflight_image_refs,
    release_inflight_image_refs,
)
from ._volume_ops import VolumeOps
from .base import ExecutionBackend

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
        github_app_id: str = "",
        github_app_private_key: str = "",
        installation_id: str = "",
        ghcr_pull_token: str = "",
        gateway_base_domain: str = "",
    ) -> None:
        import docker

        self._gateway_base_domain = gateway_base_domain
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

        # Host ports are intentionally NOT published: the Traefik edge reaches
        # managed containers over the central-deploy-proxy network by
        # container_name:container_port. Publishing host ports caused "port is
        # already allocated" conflicts with existing host-bound services.
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
        labels = traefik_labels(config, self._gateway_base_domain, PROXY_NETWORK)

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

    async def _prepare_volumes(self, config: ComponentConfig) -> list[str]:
        """Pre-create named volumes and validate claude credentials.

        Returns deploy warnings collected during credential validation.

        When *config.target_disk* is set, volumes are created under
        ``{target_disk}/robotsix-volumes/{vol_name}`` using the local
        driver's bind mount option so data lives on the chosen disk.
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

        # Resolve target disk path for volume placement
        target_disk_path: str = ""
        if config.target_disk:
            # Callers (deploy endpoint, onboard flow) already resolve the
            # identifier via resolve_target_disk() and store the canonical
            # mount point in config.target_disk.  If we received an absolute
            # directory path, use it directly to avoid a redundant findmnt
            # call; otherwise fall back to full resolution for callers that
            # pass a raw identifier.
            candidate = os.path.realpath(config.target_disk)
            if os.path.isdir(candidate):
                target_disk_path = candidate
            else:
                from robotsix_central_deploy.lifecycle._disk_utils import (
                    resolve_target_disk,
                )

                try:
                    target_disk_path = resolve_target_disk(config.target_disk)
                except ValueError as exc:
                    raise RuntimeError(
                        f"Invalid target_disk for component {config.id!r}: {exc}"
                    ) from exc

        for vol_name in volumes_to_create:
            vol_path: str = ""
            driver_opts: dict[str, str] | None = None
            if target_disk_path:
                vol_path = os.path.join(target_disk_path, "robotsix-volumes", vol_name)
                # Create the backing directory on the target disk before
                # handing it to Docker's local driver via bind options.
                try:
                    os.makedirs(vol_path, mode=0o755, exist_ok=True)
                except OSError as exc:
                    raise RuntimeError(
                        f"Failed to create volume directory {vol_path!r}: {exc}"
                    ) from exc
                driver_opts = {
                    "type": "none",
                    "device": vol_path,
                    "o": "bind",
                }

            def _create_volume(
                vol_name: str = vol_name,
                driver_opts: dict[str, str] | None = driver_opts,
            ) -> None:
                if driver_opts:
                    self._client.volumes.create(
                        vol_name,
                        driver="local",
                        driver_opts=driver_opts,
                    )
                else:
                    self._client.volumes.create(vol_name)

            try:
                await loop.run_in_executor(None, _create_volume)
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
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "claude-auth credential check failed (non-fatal): %s", exc
                )

        return deploy_warnings

    async def _try_restore(
        self, name: str, config: ComponentConfig, prior_digest: str
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
        except Exception as restore_exc:  # noqa: BLE001
            logger.error("Restore of %s also failed: %s", name, restore_exc)

    async def _build_auth_configs(self, image_ref: str) -> list[dict[str, str]]:
        """Return auth config dicts for *image_ref*, most-preferred first.

        Only ``ghcr.io`` images are authenticated; the credentials come from
        the shared resolver, so the pull and the update check present the same
        identities.  An empty list means anonymous pull, which works for public
        images only — a 401 on a private image surfaces a diagnostic error.

        The list matters: a rejected credential must be able to fall through to
        the next one, or a stale PAT silently shadows a working GitHub App.
        """
        if _image_registry_host(image_ref) != GHCR_HOST:
            return []

        try:
            candidates = await self._ghcr_credentials.resolve_all()
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} (pull of {image_ref!r})") from exc
        return [
            {
                "username": creds.username,
                "password": creds.password,
                "serveraddress": GHCR_HOST,
            }
            for creds in candidates
        ]

    async def _pull_with_fallback(
        self,
        image_ref: str,
        auth_configs: list[dict[str, str]],
        loop: Any,
        docker: Any,
    ) -> Any:
        """Pull *image_ref*, trying each auth config until one is accepted.

        Raises ``RuntimeError`` with a diagnostic that distinguishes the two
        very different ghcr.io failures: *no* credential was presented (401 on
        a private package — nothing is configured) versus a credential was
        presented and **rejected** (403 ``denied`` — what a revoked PAT looks
        like).  Conflating them sends operators hunting for a missing token
        that is in fact present and dead.
        """
        is_ghcr = _image_registry_host(image_ref) == GHCR_HOST
        attempts: list[dict[str, str] | None] = list(auth_configs) or [None]
        last_exc: Exception | None = None

        for index, auth_config in enumerate(attempts):
            try:
                return await loop.run_in_executor(
                    None,
                    lambda ac=auth_config: self._client.images.pull(
                        image_ref, auth_config=ac
                    ),
                )
            except docker.errors.APIError as exc:
                last_exc = exc
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                if status not in (401, 403) or index == len(attempts) - 1:
                    break
                logger.warning(
                    "ghcr.io rejected credential %d/%d (%s) for %s — trying the next one",
                    index + 1,
                    len(attempts),
                    status,
                    image_ref,
                )

        response = getattr(last_exc, "response", None)
        status = getattr(response, "status_code", None)
        if is_ghcr and status in (401, 403):
            if not auth_configs:
                raise RuntimeError(
                    f"Image pull failed for {image_ref!r}: ghcr.io returned {status} "
                    "and no credential was presented. Configure ghcr_pull_token "
                    "(a read:packages PAT) or github_app_id / "
                    "github_app_private_key / installation_id to authenticate."
                ) from last_exc
            tried = ", ".join(sorted({ac["username"] for ac in auth_configs}))
            raise RuntimeError(
                f"Image pull failed for {image_ref!r}: ghcr.io rejected every "
                f"configured credential ({tried}) with {status}. The credentials "
                "are present but not accepted — check whether ghcr_pull_token is "
                "revoked or expired (note it overrides config.json when set in "
                "system_settings.json), and that the GitHub App installation "
                "still grants read access to this package."
            ) from last_exc

        raise RuntimeError(
            f"Image pull failed for {image_ref!r}: {last_exc}"
        ) from last_exc

    async def deploy(
        self, service: ServiceRecord, config: ComponentConfig, image_ref: str
    ) -> DeployOutcome:
        """Pull *image_ref*, recreate the container from *config*, return outcome."""
        import docker

        name = self._container_name(service)
        loop = asyncio.get_running_loop()

        # Step 1 — pull target image; obtain its digest
        auth_configs = await self._build_auth_configs(image_ref)
        image = await self._pull_with_fallback(image_ref, auth_configs, loop, docker)
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

            # Step 4 — health wait (if configured and not disabled)
            if config.health_check and not config.health_check.disable:
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
        self, service: ServiceRecord, config: ComponentConfig
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
                except Exception as exc:  # noqa: BLE001
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

        if config.health_check and not config.health_check.disable:
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
                    Exception  # noqa: BLE001
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
        import docker

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
        import docker

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
        # Dangling images — use the same ``images.list(dangling=True)``
        # source as ``prune_images()`` so the metric matches what the
        # reclaim endpoint can actually remove.  Only leaf dangling images
        # (those not referenced as a parent by any other image) are
        # actually prunable — intermediate parent layers fail with a 409
        # "image has dependent child images" at prune time.
        try:
            all_non_intermediate = await loop.run_in_executor(
                None, self._client.images.list
            )
        except docker.errors.APIError as exc:
            logger.warning("docker image list failed: %s", exc)
            all_non_intermediate = []
        parent_ids: set[str] = set()
        for img in all_non_intermediate:
            pid = img.attrs.get("ParentId", "")
            if pid:
                parent_ids.add(pid)
        try:
            dangling_images = await loop.run_in_executor(
                None,
                lambda: self._client.images.list(filters={"dangling": True}),
            )
        except docker.errors.APIError as exc:
            logger.warning("docker image list (dangling) failed: %s", exc)
            dangling_images = []
        dangling_size = sum(int(img.attrs.get("Size", 0)) for img in dangling_images)
        reclaimable_dangling_size = sum(
            int(img.attrs.get("Size", 0))
            for img in dangling_images
            if img.id not in parent_ids
        )
        return DockerDfStats(
            images_size_bytes=images_size,
            dangling_images_bytes=dangling_size,
            dangling_images_reclaimable_bytes=reclaimable_dangling_size,
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

    async def prune_images(
        self, protected_refs: set[str], *, force: bool = False
    ) -> PruneImagesResult:
        """Remove dangling (untagged) images one by one, skipping protected refs.

        Docker's bulk prune API has no exclusion list, so images are removed
        individually. An image is protected when its id or any of its repo
        digests appears in *protected_refs*; images still used by a container
        fail removal with a 409, which is tracked in the result.

        When *force* is ``True``, stopped containers are removed first so
        that images they hold references to can be pruned.
        """
        import docker

        loop = asyncio.get_running_loop()

        def _prune() -> PruneImagesResult:
            result = PruneImagesResult()

            # -- force: remove stopped containers first -----------------------
            if force:
                try:
                    stopped = self._client.containers.list(
                        all=True,
                        filters={"status": "exited"},
                    )
                except docker.errors.APIError as exc:
                    logger.warning(
                        "image prune: list stopped containers failed: %s", exc
                    )
                    stopped = []
                for c in stopped:
                    try:
                        c.remove()
                        result.stopped_containers_removed += 1
                    except docker.errors.APIError as exc:
                        logger.debug(
                            "image prune: skip stopped container %s: %s",
                            c.id,
                            exc,
                        )

            # -- list dangling images ----------------------------------------
            try:
                dangling = self._client.images.list(filters={"dangling": True})
            except docker.errors.APIError as exc:
                logger.warning("image prune: list failed: %s", exc)
                return result

            errors: list[str] = []

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
                    result.skipped_protected += 1
                    continue
                size = int(img.attrs.get("Size", 0))
                try:
                    assert (  # noqa: S101
                        img.id is not None
                    )  # Docker images always have an id
                    self._client.images.remove(img.id)
                    result.space_reclaimed_bytes += size
                    result.removed_count += 1
                except docker.errors.APIError as exc:
                    msg = str(exc)
                    if "dependent child images" in msg:
                        result.skipped_intermediate += 1
                    elif (
                        "image is being used" in msg
                        or "image is referenced" in msg
                        or "conflict" in msg.lower()
                    ):
                        result.skipped_in_use += 1
                    else:
                        result.skipped_error += 1
                        if len(errors) < 3:
                            short_id = (img.id or "???")[:19]
                            errors.append(f"{short_id}: {msg}")
                    logger.debug("image prune: skipped %s: %s", img.id, exc)

            if errors:
                result.error_summary = "; ".join(errors)
            return result

        return await loop.run_in_executor(None, _prune)

    # ── self-management delegation (→ SelfMgmtOps) ──────────────────────

    async def inspect_self(self) -> SelfInspect | None:
        """Resolve the server's own container via the container-id hostname."""
        return await self._self_mgmt.inspect_self()

    async def heal_self_network_alias(self, network_name: str) -> str | None:
        """Repair the server's own service alias on *network_name*."""
        return await self._self_mgmt.heal_self_network_alias(network_name)

    async def trigger_self_update(
        self,
        target: SelfInspect,
        watchtower_image: str,
        docker_host_url: str,
        docker_api_version: str,
    ) -> str:
        """Launch a one-shot watchtower container that updates *target*."""
        return await self._self_mgmt.trigger_self_update(
            target, watchtower_image, docker_host_url, docker_api_version
        )

    async def trigger_self_restart(self, target: SelfInspect) -> str:
        """Restart the container identified by *target* via the Docker API."""
        return await self._self_mgmt.trigger_self_restart(target)
