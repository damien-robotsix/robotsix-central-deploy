"""Config-volume helper — reads/writes config files on Docker named volumes.

Extracted from ``DockerSdkBackend`` to keep the backend class lean.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from robotsix_central_deploy._yaml_utils import (
    InvalidConfigStructureError,
    YamlParseError,
)

logger = logging.getLogger(__name__)


class ConfigVolumeHelper:
    """Operates on Docker named volumes via a ``docker.DockerClient``.

    Handles writing, reading, listing, measuring, and removing config files
    stored on named volumes through one-shot busybox containers.
    """

    def __init__(self, client: Any) -> None:
        """*client* is a ``docker.DockerClient``."""
        self._client = client

    # -- config volume helpers ----------------------------------------------

    async def write_config_to_volume(
        self, volume_name: str, config_dict: dict[str, Any]
    ) -> None:
        """Write *config_dict* as JSON into a Docker named volume via a
        temporary busybox container.

        The volume **must** already exist; this method only writes to it.
        """
        import base64
        import json

        import docker

        json_content = json.dumps(config_dict, indent=2, sort_keys=True)
        encoded = base64.b64encode(json_content.encode()).decode()
        # base64 output contains only [A-Za-z0-9+/=] — safe to interpolate in sh without quoting
        cmd = f"mkdir -p /config && echo {encoded} | base64 -d > /config/config.json && chmod 777 /config && chmod 666 /config/config.json"
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
                    f"write_config_to_volume failed for {volume_name}: {exc}"
                ) from exc

        await loop.run_in_executor(None, _run)

    async def write_llmio_tier_config_to_volume(
        self, volume_name: str, tier_config: dict[str, Any]
    ) -> None:
        """Write *tier_config* as ``llmio_tier_config.json`` into a Docker named
        volume via a temporary busybox container.

        The volume **must** already exist; this method only writes to it.
        """
        import base64
        import json

        import docker

        json_content = json.dumps(tier_config, indent=2, sort_keys=True)
        encoded = base64.b64encode(json_content.encode()).decode()
        cmd = f"mkdir -p /config && echo {encoded} | base64 -d > /config/llmio_tier_config.json && chmod 777 /config && chmod 666 /config/llmio_tier_config.json"
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
                    f"write_llmio_tier_config_to_volume failed for {volume_name}: {exc}"
                ) from exc

        await loop.run_in_executor(None, _run)

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

    # -- volume inspection helpers ------------------------------------------

    async def measure_volume_bytes(self, volume_name: str) -> int:
        """Return effective total bytes for *volume_name*, excluding SQLite transient sidecars (*.db-wal, *.db-shm, *.db-journal). Returns 0 on error or when the volume is inaccessible."""
        loop = asyncio.get_running_loop()
        cmd = (
            "find /vol -type f "
            "! -name '*.db-wal' ! -name '*.db-shm' ! -name '*.db-journal' "
            "-exec du -b {} + 2>/dev/null "
            "| awk '{s+=$1}END{print s+0}'"
        )
        try:
            raw: bytes = await loop.run_in_executor(
                None,
                lambda: self._client.containers.run(
                    "busybox",
                    command=["sh", "-c", cmd],
                    volumes={volume_name: {"bind": "/vol", "mode": "ro"}},
                    remove=True,
                ),
            )
            return int(raw.strip() or b"0")
        except Exception as exc:
            logger.warning("measure_volume_bytes(%r) failed: %s", volume_name, exc)
            return 0

    async def list_volume_dir(
        self, volume_name: str, rel_path: str
    ) -> list[dict[str, Any]]:
        """List immediate children of /vol/<rel_path> via busybox.

        Uses ``find`` with ``-maxdepth 1`` for consistency.
        """
        loop = asyncio.get_running_loop()
        script = (
            'cd /vol && for f in "$1"/* "$1"/.*; do\n'
            '  [ -e "$f" ] || continue\n'
            '  bn="${f##*/}"\n'
            '  [ "$bn" = . ] && continue\n'
            '  [ "$bn" = .. ] && continue\n'
            '  if [ -d "$f" ]; then\n'
            '    printf "dir\\t0\\t%s\\n" "$bn"\n'
            "  else\n"
            '    sz=$(stat -c "%s" "$f" 2>/dev/null || echo 0)\n'
            '    printf "file\\t%s\\t%s\\n" "$sz" "$bn"\n'
            "  fi\n"
            "done\n"
        )
        raw: bytes = await loop.run_in_executor(
            None,
            lambda: self._client.containers.run(
                "busybox",
                command=["sh", "-c", script, "sh", rel_path],
                volumes={volume_name: {"bind": "/vol", "mode": "ro"}},
                remove=True,
            ),
        )
        entries: list[dict[str, Any]] = []
        for line in raw.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            typ, size_str, name = parts
            try:
                size_bytes = int(size_str)
            except ValueError:
                size_bytes = 0
            entries.append({"name": name, "type": typ, "size_bytes": size_bytes})
        return entries

    async def read_volume_file(
        self, volume_name: str, rel_path: str, max_bytes: int
    ) -> dict[str, Any]:
        """Read ``/vol/<rel_path>`` via a one-shot busybox container.

        Returns size, content (or None for binary), binary flag, truncated flag.
        """
        loop = asyncio.get_running_loop()
        # $1 = rel_path, $2 = max_bytes+1 (head limit)
        script = (
            'target="/vol/$1"\n'
            "maxp1=$2\n"
            'stat -c "%s" "$target" 2>/dev/null || echo 0\n'
            'head -c "$maxp1" "$target" 2>/dev/null || true\n'
        )
        raw: bytes = await loop.run_in_executor(
            None,
            lambda: self._client.containers.run(
                "busybox",
                command=["sh", "-c", script, "sh", rel_path, str(max_bytes + 1)],
                volumes={volume_name: {"bind": "/vol", "mode": "ro"}},
                remove=True,
            ),
        )
        # First line is the file size; the rest is the file content.
        lines = raw.split(b"\n", 1)
        try:
            size_bytes = int(lines[0].strip())
        except ValueError, IndexError:
            size_bytes = 0
        body = lines[1] if len(lines) > 1 else b""

        truncated = len(body) > max_bytes
        if truncated:
            body = body[:max_bytes]

        binary = b"\x00" in body
        content: str | None = None
        if not binary:
            try:
                content = body.decode("utf-8")
            except UnicodeDecodeError:
                binary = True

        return {
            "size_bytes": size_bytes,
            "content": content,
            "binary": binary,
            "truncated": truncated,
        }

    async def remove_volume(self, volume_name: str) -> None:
        """Remove the Docker named volume *volume_name* (best-effort).

        Swallows ``docker.errors.NotFound`` (already gone) and logs a
        warning on any other error — never raises, so a failed volume
        removal cannot abort a component delete.
        """
        import docker

        loop = asyncio.get_running_loop()

        def _remove() -> None:
            self._client.volumes.get(volume_name).remove(force=True)

        try:
            await loop.run_in_executor(None, _remove)
        except docker.errors.NotFound:  # Volume already removed
            pass
        except Exception as exc:
            logger.warning("remove_volume %s: %s", volume_name, exc)
