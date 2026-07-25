"""Disk resolution and discovery utilities.

Resolves target-disk identifiers (device path, mount point, or filesystem
label) to a canonical mount-point path, and discovers all mounted data
disks for multi-disk usage reporting.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Canonical path to findmnt (avoid S607 partial-path warnings).
_FINDMNT = "/usr/bin/findmnt"

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


def _resolve_user_path(raw: str) -> str:
    """Resolve a user-supplied absolute path to a canonical, safe form.

    Returns the resolved absolute path, or raises ``ValueError`` for
    inputs that contain null bytes or resolve to a non-absolute path.
    """
    if "\0" in raw:
        raise ValueError("target_disk path contains null bytes")
    resolved = os.path.realpath(raw)
    # realpath always returns an absolute path for existing paths; for
    # non-existing paths it may return a relative one.  Reject those.
    if not resolved.startswith("/"):
        raise ValueError(f"target_disk path resolution failed: {raw!r}")
    return resolved


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

    # Reject null bytes in any identifier (label or path).
    if "\0" in identifier:
        raise ValueError("target_disk identifier contains null bytes")

    identifier = identifier.strip()

    # Resolve all path-like identifiers through realpath early so
    # that every downstream filesystem operation receives a canonical,
    # already-validated path.
    if identifier.startswith("/"):
        identifier = _resolve_user_path(identifier)

    # 1. Device path
    if identifier.startswith("/dev/"):
        if not os.path.exists(identifier):
            raise ValueError(f"device path does not exist: {identifier!r}")
        mp = _mount_point_of(identifier)
        if mp is None:
            raise ValueError(
                f"device {identifier!r} is not mounted — cannot resolve to mount point"
            )
        return mp

    # 2. Mount point (must be a directory and a mount point).
    #    *identifier* is already resolved at this point.
    if os.path.isdir(identifier) and _is_mount_point(identifier):
        return identifier

    # 3. Filesystem label (non-path identifier — already checked for
    #    null bytes above, and *findmnt --source* uses execve semantics
    #    so shell metacharacters are harmless).
    mp = _mount_point_of(f"LABEL={identifier}")
    if mp is not None:
        return mp

    raise ValueError(
        f"Could not resolve target_disk {identifier!r}: "
        "not a valid device path, mount point, or filesystem label"
    )


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
        return _discover_via_proc_mounts()

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
        return _discover_via_proc_mounts()

    return disks


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
