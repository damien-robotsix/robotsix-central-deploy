"""Volume file-I/O helpers for the Docker SDK backend.

Mixed into :class:`~robotsix_central_deploy.lifecycle.backends._volume_ops.VolumeOps`;
these methods measure, prune, list, read, write and remove files inside a
Docker named volume via one-shot busybox containers.  They rely on
``self._client`` (the shared Docker client) provided by the composing
``VolumeOps`` instance.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

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


class VolumeFileOpsMixin:
    """Volume file-I/O helpers for :class:`VolumeOps`.

    Uses ``self._client`` (the Docker client shared with the owning
    ``DockerSdkBackend``) supplied by the composing ``VolumeOps`` instance.
    """

    _client: Any

    # -- members supplied by sibling mixins ----------------------------------
    # The one-shot helper mechanics and their deadlines live in
    # :class:`ContainerHelperOpsMixin`; they reach this mixin's methods
    # through the composed ``VolumeOps`` MRO.  Declared here so mypy can
    # resolve the references on this mixin in isolation.
    _ONE_SHOT_TIMEOUT_S: int
    _MEASURE_TIMEOUT_S: int
    _MEASURE_ATTEMPTS: int
    _MEASURE_RETRY_DELAY_S: int

    def _run_one_shot_sync(
        self,
        command: list[str],
        volumes: dict[str, dict[str, str]],
        timeout_s: int | None = None,
    ) -> bytes:
        raise NotImplementedError  # real impl lives in ContainerHelperOpsMixin

    def _wait_container_exit_sync(self, container: Any, timeout_s: float) -> int:
        raise NotImplementedError  # real impl lives in ContainerHelperOpsMixin

    async def measure_volume_bytes(self, volume_name: str) -> int | None:
        """Return effective total bytes for *volume_name*, excluding SQLite
        transient sidecars (*.db-wal, *.db-shm, *.db-journal).

        Robust for large volumes: runs with the longer measure-specific
        deadline and retries transient Docker API errors ("Response ended
        prematurely").  A deadline timeout is NOT retried — the helper was
        already killed by the finally-remove, and an immediate re-run of the
        same du on the same loaded host only repeats the grind (2026-09-07).
        Returns ``None`` — not 0 — when the size could not be determined, so
        callers can surface a measurement-failed finding instead of silently
        recording a bogus 0.
        """
        loop = asyncio.get_running_loop()
        cmd = _DU_BYTES_FN + "du_bytes /vol\n"
        for attempt in range(1, self._MEASURE_ATTEMPTS + 1):
            try:
                raw: bytes = await loop.run_in_executor(
                    None,
                    lambda: self._run_one_shot_sync(
                        command=["sh", "-c", cmd],
                        volumes={volume_name: {"bind": "/vol", "mode": "ro"}},
                        timeout_s=self._MEASURE_TIMEOUT_S,
                    ),
                )
                return int(raw.strip() or b"0")
            except TimeoutError as exc:
                logger.warning(
                    "measure_volume_bytes(%r) gave up: %s (deadline %ds; "
                    "not retried — a re-run would only repeat the IO grind)",
                    volume_name,
                    exc,
                    self._MEASURE_TIMEOUT_S,
                )
                return None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "measure_volume_bytes(%r) attempt %d/%d failed: %s",
                    volume_name,
                    attempt,
                    self._MEASURE_ATTEMPTS,
                    exc,
                )
                if attempt < self._MEASURE_ATTEMPTS:
                    await asyncio.sleep(self._MEASURE_RETRY_DELAY_S)
        logger.error(
            "measure_volume_bytes(%r) failed after %d attempts",
            volume_name,
            self._MEASURE_ATTEMPTS,
        )
        return None

    async def prune_volume_files(
        self,
        volume_name: str,
        rel_path: str,
        glob: str,
        max_age_days: int,
    ) -> dict[str, int]:
        """Delete files under ``/vol/<rel_path>`` matching *glob* whose mtime
        is older than *max_age_days* days, then remove directories the
        deletions emptied.  Returns ``{"removed": n, "bytes": total}``.

        Age-based retention for append-only file trees (e.g. Claude SDK
        transcript directories).  NEVER point a rule at a database-backed
        store — deleting old files inside e.g. LanceDB corrupts it.
        """
        loop = asyncio.get_running_loop()
        # $1 = rel_path ("" = volume root), $2 = glob, $3 = mtime days.
        script = (
            "dir=/vol\n"
            '[ -n "$1" ] && dir="/vol/$1"\n'
            '[ -d "$dir" ] || { echo "0 0"; exit 0; }\n'
            "n=0; s=0\n"
            'find "$dir" -type f -name "$2" -mtime "+$3" > /tmp/prune-list 2>/dev/null || true\n'
            "while IFS= read -r f; do\n"
            '  sz=$(stat -c %s "$f" 2>/dev/null || echo 0)\n'
            '  rm -f "$f" 2>/dev/null && { n=$((n+1)); s=$((s+sz)); }\n'
            "done < /tmp/prune-list\n"
            'find "$dir" -mindepth 1 -depth -type d -empty -delete 2>/dev/null\n'
            'printf "%s %s\\n" "$n" "$s"\n'
        )
        raw: bytes = await loop.run_in_executor(
            None,
            lambda: self._run_one_shot_sync(
                command=["sh", "-c", script, "sh", rel_path, glob, str(max_age_days)],
                volumes={volume_name: {"bind": "/vol", "mode": "rw"}},
            ),
        )
        try:
            n_str, s_str = raw.strip().split()
            return {"removed": int(n_str), "bytes": int(s_str)}
        except ValueError:
            logger.warning(
                "prune_volume_files(%r): unparsable helper output %r",
                volume_name,
                raw[:100],
            )
            return {"removed": 0, "bytes": 0}

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
            lambda: self._run_one_shot_sync(
                command=["sh", "-c", script, "sh", rel_path],
                volumes={volume_name: {"bind": "/vol", "mode": "ro"}},
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
                self._wait_container_exit_sync(container, self._ONE_SHOT_TIMEOUT_S)
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
