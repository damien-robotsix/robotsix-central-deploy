"""Log streaming and Docker disk-usage operations for :class:`DockerSdkBackend`.

Split out of ``docker_sdk.py`` as a mixin; ``DockerSdkBackend`` remains the
public import path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..models import DockerDfStats, ServiceRecord, VolumeStat

if TYPE_CHECKING:
    from docker import DockerClient

logger = logging.getLogger(__name__)


class LogsOpsMixin:
    """Mixin providing ``stream_logs``, ``get_container_logs`` and ``disk_df``."""

    # Provided by the concrete backend (``DockerSdkBackend``).
    _client: DockerClient
    _container_name: Callable[[ServiceRecord], str]
    _get_container: Callable[[str], Awaitable[Any]]

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
