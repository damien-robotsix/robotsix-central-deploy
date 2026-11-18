"""Volume helpers for the Docker SDK backend.

Ownership, inspection, and config-volume read/write via one-shot
busybox containers.  The :class:`VolumeOps` implementation is composed
from mixins split by concern (ownership, helper-container mechanics,
volume file I/O, config-volume JSON); relocation stays here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from robotsix_central_deploy.lifecycle.backends._config_volume_ops import (
    ConfigVolumeOpsMixin,
)
from robotsix_central_deploy.lifecycle.backends._volume_container_ops import (
    ContainerHelperOpsMixin,
)
from robotsix_central_deploy.lifecycle.backends._volume_file_ops import (
    _DU_BYTES_FN,
    _FILE_EXISTS,
    _IS_A_DIR,
    _NOT_A_DIR,
    VolumeFileOpsMixin,
)
from robotsix_central_deploy.lifecycle.backends._volume_ownership_ops import (
    OwnershipMixin,
)

logger = logging.getLogger(__name__)

#: Result vocabulary for volume lifecycle operations (relocate, prune, ...).
#: Consumers parse ``payload["status"]`` on these exact strings across the
#: backends and routers, so the vocabulary has a single definition point here.
STATUS_OK = "ok"
STATUS_FAILED = "failed"

__all__ = [
    "STATUS_FAILED",
    "STATUS_OK",
    "_DU_BYTES_FN",
    "_FILE_EXISTS",
    "_IS_A_DIR",
    "_NOT_A_DIR",
    "VolumeOps",
]


class VolumeOps(
    OwnershipMixin,
    ContainerHelperOpsMixin,
    VolumeFileOpsMixin,
    ConfigVolumeOpsMixin,
):
    """Stateful helper for Docker named-volume operations.

    Shares the Docker client with the owning ``DockerSdkBackend``.

    Ownership / uid-gid helpers (:class:`OwnershipMixin`), one-shot
    helper-container mechanics (:class:`ContainerHelperOpsMixin`), volume
    file I/O (:class:`VolumeFileOpsMixin`) and config-volume JSON
    read/write (:class:`ConfigVolumeOpsMixin`) come from the composed
    mixins; relocation lives here.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

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
            return {"status": STATUS_FAILED, "detail": str(exc)}

        # For a bind-mount volume the data lives at Options.device;
        # for a regular Docker volume it's at the Mountpoint.
        options = attrs.get("Options") or {}
        source_path = options.get("device") or attrs.get("Mountpoint", "")
        if not source_path:
            return {
                "status": STATUS_FAILED,
                "detail": f"Could not determine source path for volume {volume_name!r}",
            }

        # 2. Guard: only bind-mount volumes can be safely relocated.
        #    For a non-bind-mount (regular Docker volume), the backing data
        #    is managed by the Docker storage driver and `remove(force=True)`
        #    would destroy it.  Check before creating the target directory
        #    so we don't waste I/O on an unsupported volume type.
        if options.get("type") != "none":
            return {
                "status": STATUS_FAILED,
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
                "status": STATUS_FAILED,
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
                "status": STATUS_FAILED,
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
                    "status": STATUS_FAILED,
                    "detail": (
                        f"Target directory {target_volume_path!r} is not reachable "
                        "at the same path on the Docker host. The target disk must "
                        "be bind-mounted into central-deploy at an identical path."
                    ),
                }
        except docker.errors.APIError as exc:
            return {
                "status": STATUS_FAILED,
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
        #    ``detach=True`` and a poll-wait with an 1800s deadline.
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
                    exit_code = self._wait_container_exit_sync(container, 1800)
                except Exception:
                    try:
                        container.remove(force=True)
                    except Exception:  # noqa: BLE001,S110
                        pass
                    raise
                container.remove()
                return exit_code == 0

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
                "status": STATUS_FAILED,
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
                    exit_code = self._wait_container_exit_sync(container, 600)
                except Exception:
                    try:
                        container.remove(force=True)
                    except Exception:  # noqa: BLE001,S110
                        pass
                    raise
                container.remove()
                return exit_code == 0

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
                "status": STATUS_FAILED,
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
                        "status": STATUS_FAILED,
                        "detail": (
                            f"Failed to recreate volume {volume_name!r}: {retry_exc}"
                        ),
                    }
            else:
                return {
                    "status": STATUS_FAILED,
                    "detail": f"Failed to create new volume: {exc.explanation or exc}",
                }
        except docker.errors.DockerException as exc:
            return {
                "status": STATUS_FAILED,
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
            "status": STATUS_OK,
            "detail": (
                f"Volume {volume_name!r} relocated to {target_volume_path!r} "
                f"(content verified)"
            ),
        }
