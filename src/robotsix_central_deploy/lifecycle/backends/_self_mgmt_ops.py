"""Self-management operations for the central-deploy server container.

These operations allow the server to inspect, restart, and update its own
container — extracted from ``DockerSdkBackend`` into a composed helper
following the same pattern as ``AuthOps`` and ``VolumeOps``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from ..models import SelfInspect

logger = logging.getLogger(__name__)


class SelfMgmtOps:
    """Self-management operations for the server's own Docker container.

    Composed as a delegate on ``DockerSdkBackend`` (not a mixin).
    """

    def __init__(
        self,
        client: Any,
        get_container: Callable[..., Any],
        build_auth_config: Callable[..., Any],
    ) -> None:
        self._client = client
        self._get_container = get_container
        self._build_auth_config = build_auth_config

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
            # ``sparse=True`` is required, not an optimisation: a non-sparse
            # ``list()`` hydrates every summary with its own inspect call, so a
            # container destroyed between the list and its hydration makes
            # ``list()`` itself raise NotFound. On this host mill sandboxes are
            # created and destroyed continuously, so that race fires often; the
            # escaping NotFound made ``inspect_self`` return None and the
            # caretaker's self-skip guard fail open (2026-07-31 outage).
            for summary in self._client.containers.list(sparse=True):
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

    async def inspect_self(self) -> SelfInspect | None:
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
            # Deliberately not "daemon unreachable": a NotFound raised by a
            # container vanishing mid-scan reached here too, and the wrong
            # wording sent the 2026-07-31 outage investigation off course.
            logger.warning("inspect_self: self-inspection failed: %s", exc)
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

    async def heal_self_network_alias(self, network_name: str) -> str | None:
        """Repair the server's own service alias on *network_name*.

        The watchtower self-update recreate re-attaches networks via raw
        ``POST /networks/{id}/connect`` calls that carry no aliases, so
        after every self-update the container sits on the proxy network
        reachable only by container id: components resolving the compose
        service name (e.g. ``http://central-deploy:8100``) get NXDOMAIN
        while every health probe stays green (2026-08-03 incident — chat
        lost the deploy API for four days).

        The alias is read from the container's own
        ``com.docker.compose.service`` label, keeping engine code free of
        service names. Docker cannot add an alias to a live attachment,
        so an attached-but-aliasless endpoint is disconnected first, then
        reconnected with ``[service, container_name]``. Conservative:
        touches only this server's own container on *network_name*, and
        only when the alias is absent. Returns the repaired alias, or
        ``None`` when nothing needed doing (or the repair failed).
        """
        import socket

        import docker

        hostname = socket.gethostname()
        try:
            container = await self._get_container(hostname)
            if container is None:
                container = await self._find_by_config_hostname(hostname)
        except docker.errors.APIError as exc:
            logger.warning("alias self-heal: self-inspection failed: %s", exc)
            return None
        if container is None:
            return None

        attrs = container.attrs
        service_alias: str = ((attrs.get("Config") or {}).get("Labels") or {}).get(
            "com.docker.compose.service", ""
        )
        if not service_alias:
            logger.debug(
                "alias self-heal: no com.docker.compose.service label; skipping"
            )
            return None

        endpoint = ((attrs.get("NetworkSettings") or {}).get("Networks") or {}).get(
            network_name
        )
        known = (endpoint or {}).get("Aliases") or []
        # Newer daemons report the effective DNS names separately.
        known = known + ((endpoint or {}).get("DNSNames") or [])
        if endpoint is not None and service_alias in known:
            return None

        container_name = (attrs.get("Name") or "").lstrip("/")
        aliases = [service_alias]
        if container_name:
            aliases.append(container_name)
        loop = asyncio.get_running_loop()

        def _reattach() -> None:
            net = self._client.networks.get(network_name)
            if endpoint is not None:
                net.disconnect(container)
            net.connect(container, aliases=aliases)

        try:
            await loop.run_in_executor(None, _reattach)
        except docker.errors.APIError as exc:
            logger.warning(
                "alias self-heal: repair on %r failed: %s", network_name, exc
            )
            return None
        logger.warning(
            "alias self-heal: reattached %r to %r with aliases %r",
            container_name or attrs.get("Id", ""),
            network_name,
            aliases,
        )
        return service_alias

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

        # Mint the auth config before entering the executor — token minting
        # does network I/O and cannot run inside a thread that's already
        # inside run_in_executor.
        auth_config = await self._build_auth_config(watchtower_image)

        def _run() -> str:
            api = self._client.api
            self._client.images.pull(
                watchtower_image,
                auth_config=auth_config,
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
