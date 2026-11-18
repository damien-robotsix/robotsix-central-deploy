"""Volume-ownership helpers for the Docker SDK backend.

Mixed into :class:`~robotsix_central_deploy.lifecycle.backends._volume_ops.VolumeOps`;
these methods resolve a Docker ``user`` string to numeric uid/gid via the host
user/group database and fix a named volume root's ownership/permissions via a
one-shot busybox container.  They rely on ``self._client`` (the shared Docker
client) provided by the composing ``VolumeOps`` instance.
"""

from __future__ import annotations

from typing import Any


class OwnershipMixin:
    """Volume ownership / uid-gid helpers for :class:`VolumeOps`.

    Uses ``self._client`` (the Docker client shared with the owning
    ``DockerSdkBackend``) supplied by the composing ``VolumeOps`` instance.
    """

    _client: Any

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
