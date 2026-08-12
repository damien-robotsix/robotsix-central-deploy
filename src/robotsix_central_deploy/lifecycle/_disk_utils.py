"""Disk resolution and discovery utilities.

Resolves target-disk identifiers (device path, mount point, or filesystem
label) to a canonical mount-point path, and discovers all mounted data
disks for multi-disk usage reporting.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Canonical path to findmnt (avoid S607 partial-path warnings).
_FINDMNT = "/usr/bin/findmnt"

# Bind-mount paths to exclude from disk listing — these are
# container-internal files that Docker bind-mounts into every
# container; they are not real disks.
_CONTAINER_BIND_PATHS: frozenset[str] = frozenset(
    {
        "/etc/resolv.conf",
        "/etc/hostname",
        "/etc/hosts",
    }
)

# Substrings that identify per-container Docker bind paths.
# Matching on a substring (rather than a prefix) handles the case
# where the host root is mounted at e.g. ``/host_root`` inside the
# central-deploy container.
_CONTAINER_BIND_SUBSTRINGS: tuple[str, ...] = ("/docker/containers/",)

# Pseudo and virtual filesystem types to exclude from disk discovery.
#
# ``findmnt --real`` already skips pseudo filesystems, but this list acts
# as a belt-and-suspenders safety net for older findmnt versions that may
# lack the ``--real`` flag, and for the /proc/mounts fallback path which
# has no kernel-side pseudo-fs filtering.
_PSEUDO_FS_TYPES: frozenset[str] = frozenset(
    {
        "proc",
        "sysfs",
        "devtmpfs",
        "devpts",
        "tmpfs",
        "cgroup",
        "cgroup2",
        "pstore",
        "bpf",
        "debugfs",
        "tracefs",
        "hugetlbfs",
        "fuse.gvfsd-fuse",
        "fuse.portal",
        "overlay",
        "squashfs",
        "snapfuse",
        "autofs",
        "binfmt_misc",
        "configfs",
        "efivarfs",
        "fusectl",
        "mqueue",
        "rpc_pipefs",
        "securityfs",
        "nfsd",
        "ramfs",
    }
)


@dataclass
class DiskInfo:
    """Summary of a single mounted data disk."""

    device: str  # e.g. "/dev/sdb1"
    mount_point: str  # e.g. "/mnt/data"
    fs_type: str  # e.g. "ext4"
    total_bytes: int
    used_bytes: int
    free_bytes: int


def resolve_target_disk(identifier: str) -> str:
    """Resolve a target-disk identifier to a canonical mount-point path.

    Resolution order:
    1. Device path (e.g. ``/dev/sdb`` or ``/dev/sdb1``) → its mount point.
    2. Mount point (e.g. ``/mnt/data``) → used directly after verification.
    3. Filesystem label → the device with that label → its mount point.

    Returns the absolute mount-point path, or raises ``ValueError`` when
    the identifier cannot be resolved.
    """
    if not identifier or not identifier.strip():
        raise ValueError("target_disk identifier is empty")

    # Reject null bytes in any identifier.
    if "\0" in identifier:
        raise ValueError("target_disk identifier contains null bytes")

    identifier = identifier.strip()

    # 1. Device path: ask findmnt directly (safe — uses execve semantics).
    if identifier.startswith("/dev/"):
        mp = _mount_point_of(identifier)
        if mp is not None:
            return mp
        raise ValueError(f"device {identifier!r} is not mounted")

    # 2. Mount point: ask findmnt whether *identifier* is a mount point.
    #    findmnt --target resolves symlinks internally.
    if identifier.startswith("/"):
        if _is_mount_point(identifier):
            return identifier
        # Maybe it's a device path that doesn't start with /dev/.
        mp = _mount_point_of(identifier)
        if mp is not None:
            return mp
        raise ValueError(f"{identifier!r} is not a recognised mount point")

    # 3. Filesystem label.  findmnt --source uses execve semantics so
    #    shell metacharacters in *identifier* are harmless.
    mp = _mount_point_of(f"LABEL={identifier}")
    if mp is not None:
        return mp

    raise ValueError(
        f"Could not resolve target_disk {identifier!r}: "
        "not a valid device path, mount point, or filesystem label"
    )


def _is_container_bind_mount(mount_point: str) -> bool:
    """Return True when *mount_point* is a container-internal bind mount."""
    if mount_point in _CONTAINER_BIND_PATHS:
        return True
    return any(sub in mount_point for sub in _CONTAINER_BIND_SUBSTRINGS)


def _real_device(device: str) -> str:
    """Strip any ``[subpath]`` suffix from a findmnt device string.

    ``/dev/sdb1[/var/lib/docker/volumes/...]`` → ``/dev/sdb1``.
    """
    return device.split("[", 1)[0]


def _deduplicate_disks(disks: list[DiskInfo]) -> list[DiskInfo]:
    """De-duplicate *disks* by backing device, keeping one canonical row each.

    1. Filter out container-internal bind mounts.
    2. Group by real backing device (stripping ``[subpath]`` suffixes).
    3. For each group pick the canonical mount — prefer non-bind mounts,
       then the shortest mount-point path.
    """
    # Step 1 — filter.
    kept = [d for d in disks if not _is_container_bind_mount(d.mount_point)]

    # Step 2 — group by real backing device.
    by_device: dict[str, list[DiskInfo]] = {}
    for d in kept:
        by_device.setdefault(_real_device(d.device), []).append(d)

    # Step 3 — pick canonical row per device.
    result: list[DiskInfo] = []
    for entries in by_device.values():
        result.append(_pick_canonical(entries))

    # Stable output order.
    result.sort(key=lambda d: d.mount_point)
    return result


def _pick_canonical(entries: list[DiskInfo]) -> DiskInfo:
    """Pick the canonical ``DiskInfo`` from a group sharing the same device."""
    if len(entries) == 1:
        return entries[0]

    # Prefer non-bind mounts: device field has no ``[...]`` suffix.
    non_bind = [d for d in entries if "[" not in d.device]
    candidates = non_bind if non_bind else entries

    # Prefer the shortest mount-point path (closest to the real mount),
    # tie-breaking on entries that report non-zero total size.
    def _sort_key(d: DiskInfo) -> tuple[int, int]:
        return (len(d.mount_point), 0 if d.total_bytes > 0 else 1)

    candidates.sort(key=_sort_key)
    return candidates[0]


def discover_mounted_disks() -> list[DiskInfo]:
    """Return information for every mounted data disk on the host.

    Pseudo-filesystems (proc, sysfs, tmpfs, etc.) and loopback devices
    are excluded.
    """
    disks: list[DiskInfo] = []
    # Prefer findmnt for a structured machine-readable listing.
    try:
        raw = subprocess.check_output(  # noqa: S603
            [
                _FINDMNT,
                "--real",  # skip pseudo filesystems
                "--output",
                "SOURCE,TARGET,FSTYPE,SIZE,USED,AVAIL",
                "--bytes",
                "--noheadings",
            ],
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        raw = ""

    if not raw.strip():
        # Fall back to /proc/mounts + shutil.disk_usage
        return _deduplicate_disks(_discover_via_proc_mounts())

    for line in raw.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 3:
            continue
        device, mount_point, fs_type = parts[0], parts[1], parts[2]
        if fs_type in _PSEUDO_FS_TYPES:
            continue
        if device.startswith("/dev/loop"):
            continue
        try:
            total = int(parts[3]) if len(parts) > 3 else 0
            used = int(parts[4]) if len(parts) > 4 else 0
            avail = int(parts[5]) if len(parts) > 5 else 0
        except ValueError:
            total = used = avail = 0
        disks.append(
            DiskInfo(
                device=device,
                mount_point=mount_point,
                fs_type=fs_type,
                total_bytes=total,
                used_bytes=used,
                free_bytes=avail,
            )
        )

    if not disks:
        return _deduplicate_disks(_discover_via_proc_mounts())

    return _deduplicate_disks(disks)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _mount_point_of(source_spec: str) -> str | None:
    """Return the mount point of *source_spec* (device path or LABEL=…), or None."""
    try:
        out = subprocess.check_output(  # noqa: S603
            [_FINDMNT, "-n", "-o", "TARGET", "--source", source_spec],
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    mp = out.strip()
    return mp if mp else None


def _is_mount_point(path: str) -> bool:
    """Return True when *path* is a mount point."""
    try:
        subprocess.check_call(  # noqa: S603
            [_FINDMNT, "--target", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


def _discover_via_proc_mounts() -> list[DiskInfo]:
    """Fallback disk discovery via /proc/mounts + shutil.disk_usage."""
    import shutil

    disks: list[DiskInfo] = []
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                device, mount_point, fs_type = parts[0], parts[1], parts[2]
                if fs_type in _PSEUDO_FS_TYPES:
                    continue
                if device.startswith("/dev/loop"):
                    continue
                try:
                    usage = shutil.disk_usage(mount_point)
                except OSError:
                    continue
                disks.append(
                    DiskInfo(
                        device=device,
                        mount_point=mount_point,
                        fs_type=fs_type,
                        total_bytes=usage.total,
                        used_bytes=usage.used,
                        free_bytes=usage.free,
                    )
                )
    except OSError:
        logger.warning("Could not read /proc/mounts for disk discovery")
    return disks
