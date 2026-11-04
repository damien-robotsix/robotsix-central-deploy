"""Deploy, image-pull and rollback operations for the Docker SDK backend.

Mixed into :class:`~robotsix_central_deploy.lifecycle.backends.docker_sdk.DockerSdkBackend`.
The methods rely on the Docker client, the composed ``AuthOps`` / ``VolumeOps``
helpers, the GHCR credential resolver and the container helpers
(``_create_container``, ``_wait_healthy`` …) provided by the composing class.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..._ghcr_auth import GHCR_HOST
from ..models import (
    DeployHealthError,
    DeployOutcome,
    RollbackOutcome,
    ServiceRecord,
    ServiceState,
)
from ._auth_ops import CLAUDE_AUTH_VOLUME
from ._util import register_inflight_image_refs, release_inflight_image_refs

if TYPE_CHECKING:
    from docker import DockerClient

    from ..._ghcr_auth import GhcrCredentialResolver
    from ...registry.models import ComponentConfig
    from ._auth_ops import AuthOps
    from ._volume_ops import VolumeOps

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


class DeployOpsMixin:
    """Deploy / pull / rollback for :class:`DockerSdkBackend`."""

    _client: DockerClient
    _auth: AuthOps
    _volume: VolumeOps
    _ghcr_credentials: GhcrCredentialResolver

    if TYPE_CHECKING:

        def _container_name(self, service: ServiceRecord) -> str: ...

        async def _get_container(self, name: str) -> Any: ...

        def _create_container(self, config: ComponentConfig, image_ref: str) -> Any: ...

        async def _wait_healthy(self, name: str, timeout: float = 60.0) -> None: ...

        def _stop_and_remove(self, container: Any) -> None: ...

        async def _remove_old_container(self, name: str, existing: Any) -> str: ...

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
            candidates = await self._ghcr_credentials.resolve_all(image_ref)
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
                try:
                    await self._wait_healthy(name, timeout=60.0)
                except Exception as exc:
                    # The replacement container is already running the new
                    # image — surface the digests so the record reflects what
                    # is actually deployed (new) and what it replaced (old).
                    # Without this, a later rollback would target a stale,
                    # possibly-pruned digest (2026-10-12 linkedin incident).
                    raise DeployHealthError(
                        str(exc),
                        deployed_digest=new_digest,
                        previous_digest=prior_digest,
                    ) from exc
        finally:
            release_inflight_image_refs(inflight_refs)

        return DeployOutcome(
            deployed_digest=new_digest,
            previous_digest=prior_digest,
            state=ServiceState.RUNNING,
            warnings=deploy_warnings,
        )

    @staticmethod
    def _digest_pull_ref(image: str, digest: str) -> str:
        """Build a registry-pullable ``repo@digest`` ref from *image*.

        *digest* is a bare ``sha256:...`` that Docker can only pull when
        paired with its repository. Strips any ``@digest`` / ``:tag`` from
        *image* to recover the repo, mirroring the router's rollback ref
        derivation.  A *digest* that is already a full ref (contains ``@`` or
        ``/``) is returned unchanged.
        """
        if "@" in digest or "/" in digest:
            return digest
        bare = image.split("@", 1)[0]
        if "/" in bare:
            prefix, image_name = bare.rsplit("/", 1)
            image_name = (
                image_name.rsplit(":", 1)[0] if ":" in image_name else image_name
            )
            repo = f"{prefix}/{image_name}"
        else:
            repo = bare.rsplit(":", 1)[0] if ":" in bare else bare
        return f"{repo}@{digest}"

    async def _ensure_image_present(self, target_digest: str, image: str) -> None:
        """Ensure *target_digest* exists locally, pulling repo@digest if absent.

        Raises ``RuntimeError`` when the image is not present locally and the
        registry pull fails — callers MUST NOT stop/remove the running
        container in that case.
        """
        import docker

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._client.images.get, target_digest)
            return
        except docker.errors.ImageNotFound:
            pass
        except docker.errors.APIError as exc:
            raise RuntimeError(
                f"Could not check for rollback target image {target_digest!r}: {exc}"
            ) from exc

        pull_ref = self._digest_pull_ref(image, target_digest)
        logger.info(
            "rollback target %s not present locally — pulling %s",
            target_digest,
            pull_ref,
        )
        auth_configs = await self._build_auth_configs(pull_ref)
        try:
            await self._pull_with_fallback(pull_ref, auth_configs, loop, docker)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Rollback target image {target_digest!r} is absent locally and "
                f"could not be pulled as {pull_ref!r}: {exc}. Leaving the running "
                "container untouched."
            ) from exc

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

        # Ensure the rollback target image is present BEFORE touching the
        # running container — pulling repo@digest from the registry when it is
        # absent locally. A pruned rollback target must never leave the
        # component with no container at all (2026-10-12 linkedin incident):
        # if the pull fails we raise here, leaving the container untouched.
        await self._ensure_image_present(target_digest, config.image)

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
