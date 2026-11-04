"""Disk-usage reporting and pruning for the Docker SDK backend.

Mixed into :class:`~robotsix_central_deploy.lifecycle.backends.docker_sdk.DockerSdkBackend`,
which provides ``self._client`` (the shared Docker client).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..models import DockerDfStats, VolumeStat
from ._util import PruneImagesResult, inflight_image_refs

if TYPE_CHECKING:
    from docker import DockerClient

logger = logging.getLogger(__name__)


class MaintenanceOpsMixin:
    """Docker disk usage and prune operations for :class:`DockerSdkBackend`."""

    _client: DockerClient

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
