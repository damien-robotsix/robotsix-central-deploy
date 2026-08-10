"""Volume helpers for the Docker SDK backend.

Ownership, inspection, and config-volume read/write via one-shot
busybox containers.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from robotsix_central_deploy.lifecycle._yaml_utils import (
    InvalidConfigStructureError,
    YamlParseError,
)

logger = logging.getLogger(__name__)

#: Markers the busybox helpers print instead of output, mapped back to
#: exceptions by the callers.  A plain empty result cannot say *why*.
_NOT_A_DIR = "\x01robotsix-not-a-dir"
_IS_A_DIR = "\x01robotsix-is-a-dir"

#: Shell function printing the apparent-bytes recursive size of its argument,
#: excluding SQLite transient sidecars.  Shared by the whole-volume measure and
#: the per-directory browser sizes so the two always agree.
_DU_BYTES_FN = (
    "du_bytes() {\n"
    '  find "$1" -type f '
    "! -name '*.db-wal' ! -name '*.db-shm' ! -name '*.db-journal' "
    "-exec du -b {} + 2>/dev/null | awk '{s+=$1}END{print s+0}'\n"
    "}\n"
)


class VolumeOps:
    """Stateful helper for Docker named-volume operations.

    Shares the Docker client with the owning ``DockerSdkBackend``.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def resolve_user_to_uid_gid(user_str: str) -> tuple[int, int]:
        """Resolve a Docker user string (``uid:gid``, ``uid``, or username)
        to numeric (uid, gid) using the host user/group database.
        """
        import grp
        import pwd

        if ":" in user_str:
            u_part, g_part = user_str.split(":", 1)
        else:
            u_part = g_part = user_str

        def _resolve_uid(s: str) -> int:
            try:
                return int(s)
            except ValueError:
                return pwd.getpwnam(s).pw_uid

        def _resolve_gid(s: str) -> int:
            try:
                return int(s)
            except ValueError:
                try:
                    return grp.getgrnam(s).gr_gid
                except KeyError:
                    return pwd.getpwnam(s).pw_gid

        return _resolve_uid(u_part), _resolve_gid(g_part)

    def ensure_volume_ownership(
        self, vol_name: str, uid: int, gid: int, mode: int
    ) -> None:
        """Chown the root of a newly-created named volume to *uid:gid*
        and set its permissions to *mode* (e.g. ``0o755``).

        Runs synchronously — callers must wrap in an executor.
        """
        self._client.containers.run(
            "busybox",
            command=[
                "sh",
                "-c",
                f"chown {uid}:{gid} /mnt && chmod {mode:03o} /mnt",
            ],
            volumes={vol_name: {"bind": "/mnt", "mode": "rw"}},
            remove=True,
        )

    # -- config volume helpers ----------------------------------------------

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

    # -- volume inspection helpers ------------------------------------------

    async def measure_volume_bytes(self, volume_name: str) -> int:
        """Return effective total bytes for *volume_name*, excluding SQLite
        transient sidecars (*.db-wal, *.db-shm, *.db-journal).
        Returns 0 on error or when the volume is inaccessible.
        """
        loop = asyncio.get_running_loop()
        cmd = _DU_BYTES_FN + "du_bytes /vol\n"
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("measure_volume_bytes(%r) failed: %s", volume_name, exc)
            return 0

    async def list_volume_dir(
        self, volume_name: str, rel_path: str
    ) -> list[dict[str, Any]]:
        """List immediate children of /vol/<rel_path> via busybox.

        Directory entries carry their **recursive** size, measured the same
        way as :meth:`measure_volume_bytes` so the browser and the Disk Usage
        table agree.

        Raises ``NotADirectoryError`` when the path is not a directory in the
        volume (including a path that does not exist).
        """
        loop = asyncio.get_running_loop()
        # $1 = rel_path ("" for the volume root).  Anchor every glob at the
        # resolved directory: globbing "$1"/* with an empty $1 expands to /*,
        # which listed the *helper container's* root filesystem for every
        # volume (2026-08-08).
        script = (
            _DU_BYTES_FN + "dir=/vol\n"
            '[ -n "$1" ] && dir="/vol/$1"\n'
            'if [ ! -d "$dir" ]; then\n'
            f"  printf '{_NOT_A_DIR}\\n'\n"
            "  exit 0\n"
            "fi\n"
            'cd "$dir" || exit 0\n'
            "for f in * .*; do\n"
            '  [ -e "$f" ] || continue\n'
            '  [ "$f" = . ] && continue\n'
            '  [ "$f" = .. ] && continue\n'
            '  if [ -d "$f" ]; then\n'
            '    sz=$(du_bytes "$f")\n'
            '    printf "dir\\t%s\\t%s\\n" "${sz:-0}" "$f"\n'
            "  else\n"
            '    sz=$(stat -c "%s" "$f" 2>/dev/null || echo 0)\n'
            '    printf "file\\t%s\\t%s\\n" "$sz" "$f"\n'
            "  fi\n"
            "done\n"
            "exit 0\n"
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
            if line == _NOT_A_DIR:
                raise NotADirectoryError(rel_path)
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

        Raises ``IsADirectoryError`` when the path is a directory — reading one
        used to return its 4096-byte inode as an empty file, which read as a
        successful (but blank) file fetch.
        """
        loop = asyncio.get_running_loop()
        # $1 = rel_path, $2 = max_bytes+1 (head limit)
        script = (
            'target="/vol/$1"\n'
            "maxp1=$2\n"
            'if [ -d "$target" ]; then\n'
            f"  printf '{_IS_A_DIR}\\n'\n"
            "  exit 0\n"
            "fi\n"
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
        if raw.startswith(_IS_A_DIR.encode()):
            raise IsADirectoryError(rel_path)

        # First line is the file size; the rest is the file content.
        lines = raw.split(b"\n", 1)
        try:
            size_bytes = int(lines[0].strip())
        except (ValueError, IndexError):
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("remove_volume %s: %s", volume_name, exc)

    async def relocate_volume(
        self, volume_name: str, target_disk_path: str
    ) -> dict[str, Any]:
        """Relocate *volume_name*'s data to *target_disk_path*.

        Copies all data from the current volume backing store to
        ``{target_disk_path}/robotsix-volumes/{volume_name}``, verifies
        the copy, removes the old volume, and creates a new volume at
        the target path using Docker's local driver with bind options.

        Returns a dict ``{"status": "ok"|"failed", "detail": str}``.
        On failure the source volume is left intact; the target
        directory (if created) is removed.
        """
        import docker

        loop = asyncio.get_running_loop()

        # 1. Inspect current volume to find source data path.
        def _inspect_volume() -> dict[str, Any]:
            try:
                attrs: dict[str, Any] = dict(
                    self._client.volumes.get(volume_name).attrs
                )
                return attrs
            except docker.errors.NotFound:
                raise RuntimeError(f"Volume {volume_name!r} not found") from None

        try:
            attrs = await loop.run_in_executor(None, _inspect_volume)
        except RuntimeError:
            return {"status": "failed", "detail": f"Volume {volume_name!r} not found"}

        # For a bind-mount volume the data lives at Options.device;
        # for a regular Docker volume it's at the Mountpoint.
        options = attrs.get("Options") or {}
        source_path = options.get("device") or attrs.get("Mountpoint", "")
        if not source_path:
            return {
                "status": "failed",
                "detail": f"Could not determine source path for volume {volume_name!r}",
            }

        # 2. Create target directory.
        target_volume_path = os.path.join(
            target_disk_path, "robotsix-volumes", volume_name
        )
        try:
            os.makedirs(target_volume_path, mode=0o755, exist_ok=True)
        except OSError as exc:
            return {
                "status": "failed",
                "detail": f"Failed to create target directory {target_volume_path!r}: {exc}",
            }

        # 3. Copy data from source to target via a busybox container.
        #    Mount source (the Docker volume, which Docker resolves to the
        #    correct backing path) at /src ro, and target at /dst rw.
        copy_ok = False
        try:
            await loop.run_in_executor(
                None,
                lambda: self._client.containers.run(
                    "busybox",
                    command=[
                        "sh",
                        "-c",
                        "cp -a /src/. /dst/ 2>&1 || { echo COPY_FAILED; exit 1; }",
                    ],
                    volumes={
                        volume_name: {"bind": "/src", "mode": "ro"},
                        target_volume_path: {"bind": "/dst", "mode": "rw"},
                    },
                    remove=True,
                ),
            )
            copy_ok = True
        except docker.errors.ContainerError as exc:
            # Container exited non-zero — the copy failed.
            logger.warning(
                "relocate_volume %s: copy container failed: %s", volume_name, exc
            )
        except docker.errors.APIError as exc:
            logger.warning(
                "relocate_volume %s: Docker API error during copy: %s", volume_name, exc
            )

        if not copy_ok:
            # Clean up the target directory on failure.
            try:
                import shutil

                shutil.rmtree(target_volume_path)
            except OSError:
                pass
            return {
                "status": "failed",
                "detail": f"Data copy failed for volume {volume_name!r}",
            }

        # 4. Verify content integrity with diff -rq in a busybox container.
        #    This catches corruption that preserves file sizes, which a
        #    byte-count-only check would miss.
        try:
            await loop.run_in_executor(
                None,
                lambda: self._client.containers.run(
                    "busybox",
                    command=["diff", "-rq", "/src", "/dst"],
                    volumes={
                        volume_name: {"bind": "/src", "mode": "ro"},
                        target_volume_path: {"bind": "/dst", "mode": "ro"},
                    },
                    remove=True,
                ),
            )
        except docker.errors.ContainerError as exc:
            logger.warning(
                "relocate_volume %s: content verification failed — diff exit %s",
                volume_name,
                exc.exit_status,
            )
            try:
                import shutil

                shutil.rmtree(target_volume_path)
            except OSError:
                pass
            return {
                "status": "failed",
                "detail": (f"Content verification failed for volume {volume_name!r}"),
            }

        # 5. Create new volume pointing to target path.  Try first — the
        #    old volume still exists under the same name so we expect a 409
        #    Conflict.  On 409 we remove the old volume and retry.  For any
        #    other error the old volume is left intact (safe).
        def _create_new() -> None:
            self._client.volumes.create(
                volume_name,
                driver="local",
                driver_opts={
                    "type": "none",
                    "device": target_volume_path,
                    "o": "bind",
                },
            )

        try:
            await loop.run_in_executor(None, _create_new)
        except docker.errors.APIError as exc:
            if exc.status_code == 409:
                logger.info(
                    "Volume %s already exists, removing old and recreating "
                    "at new location %s",
                    volume_name,
                    target_volume_path,
                )
                # Snapshot old volume attributes before removal so we can
                # restore it at its original location if recreation fails.
                # Without this, a failed recreation after removal leaves
                # no volume at all — data is lost for non-bind-mount volumes.
                old_volume = self._client.volumes.get(volume_name)
                old_attrs: dict[str, Any] = dict(old_volume.attrs)
                old_driver: str = old_attrs.get("Driver", "local")
                old_driver_opts: dict[str, Any] = dict(old_attrs.get("Options") or {})
                old_labels: dict[str, str] = dict(old_attrs.get("Labels") or {})
                try:
                    await loop.run_in_executor(
                        None,
                        lambda: self._client.volumes.get(volume_name).remove(
                            force=True
                        ),
                    )
                    await loop.run_in_executor(None, _create_new)
                except Exception as retry_exc:  # noqa: BLE001
                    # Attempt to restore the old volume at its original
                    # location so the data is not orphaned.
                    try:
                        await loop.run_in_executor(
                            None,
                            lambda: self._client.volumes.create(
                                volume_name,
                                driver=old_driver,
                                driver_opts=old_driver_opts,
                                labels=old_labels,
                            ),
                        )
                    except Exception as restore_exc:  # noqa: BLE001
                        logger.warning(
                            "relocate_volume %s: failed to restore old "
                            "volume after failed recreation: %s",
                            volume_name,
                            restore_exc,
                        )
                    return {
                        "status": "failed",
                        "detail": (
                            f"Failed to recreate volume {volume_name!r}: {retry_exc}"
                        ),
                    }
            else:
                return {
                    "status": "failed",
                    "detail": f"Failed to create new volume: {exc.explanation or exc}",
                }
        except docker.errors.DockerException as exc:
            return {
                "status": "failed",
                "detail": f"Docker daemon unreachable: {exc}",
            }

        # 6. Fix ownership on the new volume.  The volume root must be owned
        #    by the container user (1000:1000, matching every fleet component)
        #    so the container can write to it.  ``cp -a`` preserved ownership
        #    of files *inside* the volume; this chown ensures the mount point
        #    itself is also accessible.
        await loop.run_in_executor(
            None,
            self.ensure_volume_ownership,
            volume_name,
            1000,
            1000,
            0o755,
        )

        return {
            "status": "ok",
            "detail": (
                f"Volume {volume_name!r} relocated to {target_volume_path!r} "
                f"(content verified)"
            ),
        }


def _du_host_path(path: str) -> int:
    """Return recursive byte count of *path* on the host, excluding SQLite
    transient sidecars.  Returns 0 on error (including inaccessible paths)."""
    import subprocess

    try:
        proc = subprocess.run(  # noqa: S603
            [
                "/usr/bin/find",
                path,
                "-type",
                "f",
                "!",
                "-name",
                "*.db-wal",
                "!",
                "-name",
                "*.db-shm",
                "!",
                "-name",
                "*.db-journal",
                "-exec",
                "du",
                "-b",
                "{}",
                "+",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        raw = proc.stdout
    except (FileNotFoundError, OSError):
        return 0
    total = 0
    for line in raw.splitlines():
        parts = line.split()
        if parts:
            try:
                total += int(parts[0])
            except ValueError:
                pass
    return total
