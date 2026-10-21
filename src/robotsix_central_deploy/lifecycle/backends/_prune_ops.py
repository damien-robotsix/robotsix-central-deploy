"""Build-cache and dangling-image pruning for :class:`DockerSdkBackend`.

Split out of ``docker_sdk.py`` as a mixin; ``DockerSdkBackend`` remains the
public import path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ._util import PruneImagesResult, inflight_image_refs

if TYPE_CHECKING:
    from docker import DockerClient

logger = logging.getLogger(__name__)


class PruneOpsMixin:
    """Mixin providing ``prune_builds`` and ``prune_images``."""

    _client: DockerClient

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
