"""Volume delegation wrappers for :class:`DockerSdkBackend`.

Thin async wrappers that forward config-volume and volume-inspection calls to
the composed :class:`VolumeOps` helper.  Split out of ``docker_sdk.py`` as a
mixin; ``DockerSdkBackend`` remains the public import path.
"""

from __future__ import annotations

from typing import Any

from ._volume_ops import VolumeOps


class VolumeDelegatesMixin:
    """Mixin forwarding volume operations to ``self._volume`` (a ``VolumeOps``)."""

    _volume: VolumeOps

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
