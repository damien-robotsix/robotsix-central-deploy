"""Config-volume read/write helpers for the Docker SDK backend.

Mixed into :class:`~robotsix_central_deploy.lifecycle.backends._volume_ops.VolumeOps`;
these methods read and write JSON config files (``config.json``,
``llmio_tier_config.json``) into a component's config-only named volume via
one-shot busybox containers.  They rely on ``self._client`` (the shared Docker
client) provided by the composing ``VolumeOps`` instance.
"""

from __future__ import annotations

import asyncio
from typing import Any

from robotsix_central_deploy.lifecycle._yaml_utils import (
    InvalidConfigStructureError,
    YamlParseError,
)


class ConfigVolumeOpsMixin:
    """Config-volume JSON I/O for :class:`VolumeOps`.

    Uses ``self._client`` (the Docker client shared with the owning
    ``DockerSdkBackend``) supplied by the composing ``VolumeOps`` instance.
    """

    _client: Any

    async def _write_json_to_volume(
        self,
        volume_name: str,
        filename: str,
        config_dict: dict[str, Any],
    ) -> None:
        """Write *config_dict* as JSON into *filename* on a Docker named volume
        via a temporary busybox container.

        The volume **must** already exist; this method only writes to it.
        """
        import base64
        import json

        import docker

        json_content = json.dumps(config_dict, indent=2, sort_keys=True)
        encoded = base64.b64encode(json_content.encode()).decode()
        # base64 output contains only [A-Za-z0-9+/=] — safe to interpolate in sh without quoting
        # The busybox helper runs as root while fleet components run as
        # 1000:1000, so the tightened 700/600 permissions must come with a
        # chown or the component is locked out of its own config (chat
        # crash-looped on PermissionError after the 777/666 → 700/600 change).
        cmd = (
            f"mkdir -p /config && echo {encoded} | base64 -d > /config/{filename}"
            f" && chown 1000:1000 /config /config/{filename}"
            f" && chmod 700 /config && chmod 600 /config/{filename}"
        )
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
                raise RuntimeError(
                    f"{filename} write failed for {volume_name}: {exc}"
                ) from exc

        await loop.run_in_executor(None, _run)

    async def write_config_to_volume(
        self, volume_name: str, config_dict: dict[str, Any]
    ) -> None:
        """Write *config_dict* as JSON into a Docker named volume via a
        temporary busybox container.

        The volume **must** already exist; this method only writes to it.
        """
        await self._write_json_to_volume(volume_name, "config.json", config_dict)

    async def write_llmio_tier_config_to_volume(
        self, volume_name: str, tier_config: dict[str, Any]
    ) -> None:
        """Write *tier_config* as ``llmio_tier_config.json`` into a Docker named
        volume via a temporary busybox container.

        The volume **must** already exist; this method only writes to it.
        """
        await self._write_json_to_volume(
            volume_name, "llmio_tier_config.json", tier_config
        )

    async def read_config_from_volume(self, volume_name: str) -> dict[str, Any]:
        """Read /config/config.json from a named volume via a temporary busybox container."""
        import json

        loop = asyncio.get_running_loop()

        def _run() -> dict[str, Any]:
            import docker

            try:
                raw = self._client.containers.run(
                    "busybox",
                    command=["sh", "-c", "cat /config/config.json 2>/dev/null || true"],
                    volumes={volume_name: {"bind": "/config", "mode": "ro"}},
                    remove=True,
                )
                text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw

                if not text.strip():
                    return {}
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise InvalidConfigStructureError(
                        f"Expected a mapping in Docker volume {volume_name}, "
                        f"got {type(data).__name__}"
                    )
                return data
            except (json.JSONDecodeError, ValueError) as exc:
                raise YamlParseError(
                    f"JSON parse error in Docker volume {volume_name}: {exc}"
                ) from exc
            except docker.errors.APIError as exc:
                raise RuntimeError(
                    f"read_config_from_volume failed for {volume_name}: {exc}"
                ) from exc

        return await loop.run_in_executor(None, _run)
