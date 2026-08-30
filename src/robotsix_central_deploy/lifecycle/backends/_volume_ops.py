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
_FILE_EXISTS = "\x01robotsix-file-exists"

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

    async def write_volume_file(
        self,
        volume_name: str,
        rel_path: str,
        content: str,
        overwrite: bool,
    ) -> dict[str, Any]:
        """Create-or-overwrite ``/vol/<rel_path>`` with *content* (UTF-8 text)
        via a one-shot busybox container.

        Parent directories are created as needed.  The write is create-only
        unless *overwrite* is True: when the target already exists and
        *overwrite* is False, raises ``FileExistsError``.  When the target is
        an existing directory, raises ``IsADirectoryError``.  The busybox
        helper mounts only the named volume at ``/vol``, so a symlink pointing
        outside the volume resolves within the ephemeral container filesystem
        and cannot escape to the host.  Returns ``{"size_bytes": int}`` — the
        number of content bytes written.
        """
        import io
        import tarfile

        import docker

        data = content.encode("utf-8")
        size_bytes = len(data)
        overwrite_flag = "1" if overwrite else "0"
        # The file bytes are streamed into the helper container via
        # ``put_archive`` (a tar sent over the Docker API), NOT inlined into the
        # shell command.  Inlining would place the (base64-inflated) content in a
        # single ``execve`` argv element, which Linux caps at MAX_ARG_STRLEN
        # (128 KiB) — any write past ~96 KiB raw would fail with E2BIG even
        # though the advertised cap is 1 MiB.  Streaming lets a write succeed up
        # to the full ``chat_volume_write_max_bytes`` cap.  $1 = rel_path,
        # $2 = overwrite flag ("1"/"0").  The busybox helper runs as root while
        # fleet components run as 1000:1000, so the created file is chowned to
        # 1000:1000 (matching write_config_to_volume) or the component cannot
        # read its own file.
        script = (
            'target="/vol/$1"\n'
            'overwrite="$2"\n'
            'if [ -d "$target" ]; then\n'
            f"  printf '{_IS_A_DIR}\\n'\n"
            "  exit 0\n"
            "fi\n"
            'if [ -e "$target" ] && [ "$overwrite" != "1" ]; then\n'
            f"  printf '{_FILE_EXISTS}\\n'\n"
            "  exit 0\n"
            "fi\n"
            'dir=$(dirname "$target")\n'
            'mkdir -p "$dir"\n'
            'cp /robotsix-staging/payload "$target"\n'
            'chown 1000:1000 "$target" 2>/dev/null || true\n'
            'chmod 600 "$target" 2>/dev/null || true\n'
        )

        # Tar carrying the raw file bytes, extracted into the container at
        # /robotsix-staging/payload before the script runs.
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            dir_info = tarfile.TarInfo(name="robotsix-staging")
            dir_info.type = tarfile.DIRTYPE
            dir_info.mode = 0o755
            tar.addfile(dir_info)
            file_info = tarfile.TarInfo(name="robotsix-staging/payload")
            file_info.size = size_bytes
            file_info.mode = 0o600
            tar.addfile(file_info, io.BytesIO(data))
        payload = tar_buf.getvalue()

        loop = asyncio.get_running_loop()

        def _run() -> bytes:
            container = None
            try:
                container = self._client.containers.create(
                    "busybox",
                    command=["sh", "-c", script, "sh", rel_path, overwrite_flag],
                    volumes={volume_name: {"bind": "/vol", "mode": "rw"}},
                )
                container.put_archive("/", payload)
                container.start()
                container.wait()
                raw = container.logs(stdout=True, stderr=True)
                return raw if isinstance(raw, bytes) else raw.encode()
            except (docker.errors.APIError, docker.errors.ContainerError) as exc:
                raise RuntimeError(
                    f"write_volume_file failed for {volume_name}: {exc}"
                ) from exc
            finally:
                if container is not None:
                    try:
                        container.remove(force=True)
                    except docker.errors.APIError:
                        pass

        raw = await loop.run_in_executor(None, _run)
        if raw.startswith(_IS_A_DIR.encode()):
            raise IsADirectoryError(rel_path)
        if raw.startswith(_FILE_EXISTS.encode()):
            raise FileExistsError(rel_path)
        return {"size_bytes": size_bytes}

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
        self,
        volume_name: str,
        target_disk_path: str,
        container_user: str | None = None,
    ) -> dict[str, Any]:
        """Relocate *volume_name*'s data to *target_disk_path*.

        Copies all data from the current volume backing store to
        ``{target_disk_path}/robotsix-volumes/{volume_name}``, verifies
        the copy, removes the old volume, and creates a new volume at
        the target path using Docker's local driver with bind options.

        *container_user* is the owning component's Docker ``user`` string
        (e.g. ``"1000:1000"``); it is used to resolve the uid/gid for the
        post-relocation ownership fix, mirroring ``_prepare_volumes``.

        Returns a dict ``{"status": "ok"|"failed", "detail": str}``.
        On failure the source volume is left intact; the target
        directory (if created) is removed.
        """
        import docker

        loop = asyncio.get_running_loop()

        # Resolve the owning component's uid/gid so a non-default ``user``
        # keeps its volume root owned accordingly.  Defaults to the server's
        # own uid/gid (as _prepare_volumes does) when no override is given.
        user_str = container_user or f"{os.getuid()}:{os.getgid()}"
        chown_uid, chown_gid = self.resolve_user_to_uid_gid(user_str)

        # 1. Inspect current volume to find source data path.
        def _inspect_volume() -> dict[str, Any]:
            try:
                attrs: dict[str, Any] = dict(
                    self._client.volumes.get(volume_name).attrs
                )
                return attrs
            except docker.errors.NotFound:
                raise RuntimeError(f"Volume {volume_name!r} not found") from None
            except docker.errors.APIError as exc:
                # A daemon-unreachable APIError must not surface as a raw
                # 500 — convert it to RuntimeError so the outer handler can
                # return a graceful {"status": "failed", ...}.
                raise RuntimeError(
                    f"Failed to inspect volume {volume_name!r}: {exc}"
                ) from exc

        try:
            attrs = await loop.run_in_executor(None, _inspect_volume)
        except RuntimeError as exc:
            return {"status": "failed", "detail": str(exc)}

        # For a bind-mount volume the data lives at Options.device;
        # for a regular Docker volume it's at the Mountpoint.
        options = attrs.get("Options") or {}
        source_path = options.get("device") or attrs.get("Mountpoint", "")
        if not source_path:
            return {
                "status": "failed",
                "detail": f"Could not determine source path for volume {volume_name!r}",
            }

        # 2. Guard: only bind-mount volumes can be safely relocated.
        #    For a non-bind-mount (regular Docker volume), the backing data
        #    is managed by the Docker storage driver and `remove(force=True)`
        #    would destroy it.  Check before creating the target directory
        #    so we don't waste I/O on an unsupported volume type.
        if options.get("type") != "none":
            return {
                "status": "failed",
                "detail": (
                    f"Volume {volume_name!r} is not a bind-mount volume "
                    "and cannot be relocated. Only bind-mount volumes "
                    "(type=none) are supported."
                ),
            }

        # 3. Create target directory (via executor — blocking I/O).
        target_volume_path = os.path.join(
            target_disk_path, "robotsix-volumes", volume_name
        )
        try:
            await loop.run_in_executor(
                None,
                lambda: os.makedirs(target_volume_path, mode=0o755, exist_ok=True),
            )
        except OSError as exc:
            return {
                "status": "failed",
                "detail": f"Failed to create target directory {target_volume_path!r}: {exc}",
            }

        # 4. Fail-fast: verify the directory we just created is the SAME
        #     directory the Docker daemon will bind into the copy container.
        #     ``os.makedirs`` above runs inside the central-deploy container,
        #     while the busybox ``/dst`` bind below resolves the path on the
        #     Docker *host*.  They agree only when the target disk is
        #     bind-mounted into central-deploy at the identical path.  A
        #     mismatch would silently copy the data to a different (empty)
        #     host directory and then remove the source — a data-loss shape —
        #     so we probe with a sentinel file before touching any data.
        import secrets

        sentinel = f".robotsix-relocate-probe-{secrets.token_hex(8)}"
        marker_path = os.path.join(target_volume_path, sentinel)

        def _write_probe() -> None:
            with open(marker_path, "w", encoding="utf-8") as fh:
                fh.write("robotsix-probe")

        try:
            await loop.run_in_executor(None, _write_probe)
        except OSError as exc:
            return {
                "status": "failed",
                "detail": (
                    f"Target directory {target_volume_path!r} is not writable: {exc}"
                ),
            }
        try:
            raw_probe: bytes = await loop.run_in_executor(
                None,
                lambda: self._client.containers.run(
                    "busybox",
                    command=[
                        "sh",
                        "-c",
                        f'cat "/dst/{sentinel}" 2>/dev/null || echo MISSING',
                    ],
                    volumes={target_volume_path: {"bind": "/dst", "mode": "ro"}},
                    remove=True,
                ),
            )
            probe_text = (
                raw_probe.decode(errors="replace").strip()
                if isinstance(raw_probe, bytes)
                else str(raw_probe).strip()
            )
            if probe_text != "robotsix-probe":
                return {
                    "status": "failed",
                    "detail": (
                        f"Target directory {target_volume_path!r} is not reachable "
                        "at the same path on the Docker host. The target disk must "
                        "be bind-mounted into central-deploy at an identical path."
                    ),
                }
        except docker.errors.APIError as exc:
            return {
                "status": "failed",
                "detail": (
                    f"Failed to probe target directory {target_volume_path!r}: {exc}"
                ),
            }
        finally:
            try:
                await loop.run_in_executor(None, os.remove, marker_path)
            except OSError:  # best-effort; never mask the probe result
                pass

        # 5. Copy data from source to target via a busybox container.
        #    Mount source (the Docker volume, which Docker resolves to the
        #    correct backing path) at /src ro, and target at /dst rw.
        #    ``containers.run()`` does NOT accept a ``timeout`` kwarg
        #    (``timeout`` is forwarded to ``Container.stop()``), so we use
        #    ``detach=True`` and ``container.wait(timeout=1800)`` for a
        #    configurable deadline.
        copy_ok = False
        try:

            def _run_copy() -> bool:
                container = self._client.containers.run(
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
                    detach=True,
                )
                try:
                    exit_info = container.wait(timeout=1800)
                except Exception:
                    try:
                        container.remove(force=True)
                    except Exception:  # noqa: BLE001,S110
                        pass
                    raise
                container.remove()
                return bool(exit_info.get("StatusCode", 1) == 0)

            copy_ok = await loop.run_in_executor(None, _run_copy)
        except Exception:
            logger.warning(
                "relocate_volume %s: copy container failed", volume_name, exc_info=True
            )

        if not copy_ok:
            # Clean up the target directory on failure (via executor).
            try:
                import shutil

                await loop.run_in_executor(None, shutil.rmtree, target_volume_path)
            except OSError:  # best-effort cleanup; ignore if dir is already gone
                pass
            return {
                "status": "failed",
                "detail": f"Data copy failed for volume {volume_name!r}",
            }

        # 6. Verify content integrity with diff -rq in a busybox container.
        #    This catches corruption that preserves file sizes, which a
        #    byte-count-only check would miss.
        verify_ok = False
        try:

            def _run_verify() -> bool:
                container = self._client.containers.run(
                    "busybox",
                    command=["diff", "-rq", "/src", "/dst"],
                    volumes={
                        volume_name: {"bind": "/src", "mode": "ro"},
                        target_volume_path: {"bind": "/dst", "mode": "ro"},
                    },
                    detach=True,
                )
                try:
                    exit_info = container.wait(timeout=600)
                except Exception:
                    try:
                        container.remove(force=True)
                    except Exception:  # noqa: BLE001,S110
                        pass
                    raise
                container.remove()
                return bool(exit_info.get("StatusCode", 1) == 0)

            verify_ok = await loop.run_in_executor(None, _run_verify)
        except Exception:
            logger.warning(
                "relocate_volume %s: content verification failed",
                volume_name,
                exc_info=True,
            )

        if not verify_ok:
            try:
                import shutil

                await loop.run_in_executor(None, shutil.rmtree, target_volume_path)
            except OSError:  # best-effort cleanup; ignore if dir is already gone
                pass
            return {
                "status": "failed",
                "detail": (f"Content verification failed for volume {volume_name!r}"),
            }

        # 7. Create new volume pointing to target path.  Try first — the
        #    old volume still exists under the same name so we expect a 409
        #    Conflict.  On 409 we remove the old volume and retry.  For any
        #    other error the old volume is left intact (safe).
        # Capture the old labels *before* any removal so the new volume
        # preserves them (review #436, minor: on-success label preservation).
        old_labels: dict[str, str] = dict(attrs.get("Labels") or {})

        def _create_new() -> None:
            self._client.volumes.create(
                volume_name,
                driver="local",
                driver_opts={
                    "type": "none",
                    "device": target_volume_path,
                    "o": "bind",
                },
                labels=old_labels,
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
                def _snapshot_old_volume() -> tuple[
                    str, dict[str, Any], dict[str, str]
                ]:
                    old_volume = self._client.volumes.get(volume_name)
                    old_attrs: dict[str, Any] = dict(old_volume.attrs)
                    old_driver: str = old_attrs.get("Driver", "local")
                    old_driver_opts: dict[str, Any] = dict(
                        old_attrs.get("Options") or {}
                    )
                    old_labels: dict[str, str] = dict(old_attrs.get("Labels") or {})
                    return old_driver, old_driver_opts, old_labels

                old_driver, old_driver_opts, old_labels = await loop.run_in_executor(
                    None, _snapshot_old_volume
                )
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

        # 8. Fix ownership on the new volume.  The volume root must be owned
        #    by the owning container's user so the container can write to it.
        #    ``cp -a`` preserved ownership of files *inside* the volume; this
        #    chown ensures the mount point itself is also accessible.  The
        #    uid/gid are resolved from the component's ``user`` override (or
        #    the server's own uid/gid as the default), not hardcoded 1000:1000.
        await loop.run_in_executor(
            None,
            self.ensure_volume_ownership,
            volume_name,
            chown_uid,
            chown_gid,
            0o755,
        )

        # 9. Best-effort cleanup of the old bind-mount source directory.
        #    For bind-mount volumes, the volume `remove(force=True)` above
        #    only removes the Docker metadata — the host directory is
        #    preserved.  Since the relocation intent is to free disk space,
        #    we remove the old source data here.  A failure is logged and
        #    does not abort the successful relocation.
        if source_path:
            try:
                import shutil

                await loop.run_in_executor(None, shutil.rmtree, source_path)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "relocate %s: failed to remove old source %s (non-fatal)",
                    volume_name,
                    source_path,
                )

        return {
            "status": "ok",
            "detail": (
                f"Volume {volume_name!r} relocated to {target_volume_path!r} "
                f"(content verified)"
            ),
        }
