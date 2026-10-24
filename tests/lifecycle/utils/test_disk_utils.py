"""Unit tests for ``_disk_utils.py``: resolve_target_disk and discover_mounted_disks."""

from __future__ import annotations

import io
import os
import subprocess
from collections import namedtuple

import pytest

from robotsix_central_deploy.lifecycle._disk_utils import (
    DiskInfo,
    _deduplicate_disks,
    _discover_via_proc_mounts,
    _is_container_bind_mount,
    _is_mount_point,
    _mount_point_of,
    _pick_canonical,
    _real_device,
    discover_mounted_disks,
    resolve_target_disk,
)

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


# ---------------------------------------------------------------------------
# resolve_target_disk
# ---------------------------------------------------------------------------


class TestResolveTargetDisk:
    def test_empty_identifier_raises(self):
        with pytest.raises(ValueError, match="empty"):
            resolve_target_disk("")
        with pytest.raises(ValueError, match="empty"):
            resolve_target_disk("   ")

    def test_device_path_nonexistent_raises(self):
        with pytest.raises(ValueError, match="is not mounted"):
            resolve_target_disk("/dev/nonexistent_xyz")

    def test_device_path_exists_but_not_mounted_raises(self, monkeypatch):
        # Have os.path.exists return True for /dev/sdz99 so the device-path
        # branch activates, but mock _mount_point_of to return None (device
        # not mounted).
        monkeypatch.setattr(os.path, "exists", lambda path: True)
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle._disk_utils._mount_point_of",
            lambda source_spec: None,
        )
        with pytest.raises(ValueError, match="is not mounted"):
            resolve_target_disk("/dev/sdz99")

    def test_device_path_resolves_to_mount(self, monkeypatch):
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle._disk_utils._mount_point_of",
            lambda source_spec: "/mnt/data",
        )
        monkeypatch.setattr(os.path, "exists", lambda path: True)
        result = resolve_target_disk("/dev/sdb1")
        assert result == "/mnt/data"

    def test_mount_point_used_directly(self, monkeypatch, tmp_path):
        mount = tmp_path / "mnt"
        mount.mkdir()
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle._disk_utils._is_mount_point",
            lambda path: True,
        )
        result = resolve_target_disk(str(mount))
        assert result == str(mount)

    def test_filesystem_label_resolves(self, monkeypatch):
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle._disk_utils._mount_point_of",
            lambda source_spec: (
                "/mnt/label_disk" if source_spec == "LABEL=mydisk" else None
            ),
        )
        monkeypatch.setattr(os.path, "isdir", lambda path: False)
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle._disk_utils._is_mount_point",
            lambda path: False,
        )
        result = resolve_target_disk("mydisk")
        assert result == "/mnt/label_disk"

    def test_unresolvable_identifier_raises(self, monkeypatch, tmp_path):
        # Ensure nothing matches: not a device, not a directory mount point,
        # not a label.
        monkeypatch.setattr(os.path, "exists", lambda path: False)
        monkeypatch.setattr(os.path, "isdir", lambda path: False)
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle._disk_utils._is_mount_point",
            lambda path: False,
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle._disk_utils._mount_point_of",
            lambda source_spec: None,
        )
        with pytest.raises(ValueError, match="Could not resolve target_disk"):
            resolve_target_disk("garbage")


# ---------------------------------------------------------------------------
# _mount_point_of
# ---------------------------------------------------------------------------


class TestMountPointOf:
    def test_returns_mount_point(self, monkeypatch):
        def fake_check_output(args, text, timeout):
            return "/mnt/data\n"

        monkeypatch.setattr(subprocess, "check_output", fake_check_output)
        result = _mount_point_of("/dev/sdb1")
        assert result == "/mnt/data"

    def test_returns_none_on_empty_output(self, monkeypatch):
        monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "\n")
        result = _mount_point_of("/dev/sdb1")
        assert result is None

    def test_returns_none_on_findmnt_error(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda *a, **kw: (_ for _ in ()).throw(
                subprocess.CalledProcessError(1, "findmnt")
            ),
        )
        result = _mount_point_of("/dev/sdb1")
        assert result is None

    def test_returns_none_on_file_not_found(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()),
        )
        result = _mount_point_of("/dev/sdb1")
        assert result is None


# ---------------------------------------------------------------------------
# _is_mount_point
# ---------------------------------------------------------------------------


class TestIsMountPoint:
    def test_true_when_findmnt_succeeds(self, monkeypatch):
        monkeypatch.setattr(subprocess, "check_call", lambda *a, **kw: 0)
        assert _is_mount_point("/mnt/data") is True

    def test_false_when_findmnt_fails(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "check_call",
            lambda *a, **kw: (_ for _ in ()).throw(
                subprocess.CalledProcessError(1, "findmnt")
            ),
        )
        assert _is_mount_point("/mnt/data") is False

    def test_false_on_oserror(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "check_call",
            lambda *a, **kw: (_ for _ in ()).throw(OSError()),
        )
        assert _is_mount_point("/mnt/data") is False


# ---------------------------------------------------------------------------
# discover_mounted_disks
# ---------------------------------------------------------------------------


FAKE_FINDMNT_OUTPUT = """\
/dev/sda1 / ext4 100000000000 60000000000 40000000000
/dev/sdb1 /mnt/data ext4 500000000000 100000000000 400000000000
/dev/loop0 /snap/core/123 squashfs 100000000 100000000 0
tmpfs /dev/shm tmpfs 8000000000 0 8000000000
"""


class TestDiscoverMountedDisks:
    def test_findmnt_output_parsed(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda args, text, timeout: FAKE_FINDMNT_OUTPUT,
        )
        disks = discover_mounted_disks()
        assert len(disks) == 2  # loop and tmpfs excluded
        assert disks[0].device == "/dev/sda1"
        assert disks[0].mount_point == "/"
        assert disks[0].fs_type == "ext4"
        assert disks[0].total_bytes == 100_000_000_000
        assert disks[0].used_bytes == 60_000_000_000
        assert disks[0].free_bytes == 40_000_000_000
        assert disks[1].device == "/dev/sdb1"
        assert disks[1].mount_point == "/mnt/data"

    def test_findmnt_empty_falls_back_to_proc(self, monkeypatch, tmp_path):
        # Return empty output → fallback to /proc/mounts
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda args, text, timeout: "",
        )
        proc_mounts = tmp_path / "proc_mounts"
        proc_mounts.write_text("/dev/sda1 / ext4 rw 0 0\n")
        monkeypatch.setattr(
            "builtins.open",
            lambda path, mode="r": io.open(str(proc_mounts)),  # noqa: UP020, SIM115
        )
        import shutil as shutil_mod

        monkeypatch.setattr(
            shutil_mod,
            "disk_usage",
            lambda path: DiskUsage(1000, 500, 500),
        )
        disks = discover_mounted_disks()
        assert len(disks) >= 1

    def test_findmnt_error_falls_back(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()),
        )
        proc_mounts = tmp_path / "proc_mounts"
        proc_mounts.write_text("/dev/sda1 / ext4 rw 0 0\n")
        monkeypatch.setattr(
            "builtins.open",
            lambda path, mode="r": io.open(str(proc_mounts)),  # noqa: UP020, SIM115
        )
        import shutil as shutil_mod

        monkeypatch.setattr(
            shutil_mod,
            "disk_usage",
            lambda path: DiskUsage(1000, 500, 500),
        )
        disks = discover_mounted_disks()
        assert len(disks) >= 1

    def test_findmnt_no_disks_falls_back(self, monkeypatch, tmp_path):
        # All lines are pseudo-fs or loop → resulting list empty → fallback
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda args, text, timeout: "tmpfs /dev/shm tmpfs 1000 500 500\n",
        )
        proc_mounts = tmp_path / "proc_mounts"
        proc_mounts.write_text("/dev/sda1 / ext4 rw 0 0\n")
        monkeypatch.setattr(
            "builtins.open",
            lambda path, mode="r": io.open(str(proc_mounts)),  # noqa: UP020, SIM115
        )
        import shutil as shutil_mod

        monkeypatch.setattr(
            shutil_mod,
            "disk_usage",
            lambda path: DiskUsage(1000, 500, 500),
        )
        disks = discover_mounted_disks()
        assert len(disks) >= 1

    def test_partial_columns_default_to_zero(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda args, text, timeout: "/dev/sda1 / ext4\n",
        )
        disks = discover_mounted_disks()
        assert len(disks) == 1
        assert disks[0].total_bytes == 0
        assert disks[0].used_bytes == 0
        assert disks[0].free_bytes == 0

    def test_invalid_size_columns_default_to_zero(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda args, text, timeout: "/dev/sda1 / ext4 NAN NOT_A_NUM NOPE\n",
        )
        disks = discover_mounted_disks()
        assert len(disks) == 1
        assert disks[0].total_bytes == 0


# ---------------------------------------------------------------------------
# _real_device
# ---------------------------------------------------------------------------


class TestRealDevice:
    def test_plain_device_unchanged(self):
        assert _real_device("/dev/sdb1") == "/dev/sdb1"

    def test_bind_mount_subpath_stripped(self):
        assert (
            _real_device("/dev/sdb1[/var/lib/docker/volumes/chat/_data]") == "/dev/sdb1"
        )

    def test_nested_brackets(self):
        assert _real_device("/dev/sda1[/some/path[with]brackets]") == "/dev/sda1"


# ---------------------------------------------------------------------------
# _is_container_bind_mount
# ---------------------------------------------------------------------------


class TestIsContainerBindMount:
    def test_resolv_conf_is_bind(self):
        assert _is_container_bind_mount("/etc/resolv.conf") is True

    def test_hostname_is_bind(self):
        assert _is_container_bind_mount("/etc/hostname") is True

    def test_hosts_is_bind(self):
        assert _is_container_bind_mount("/etc/hosts") is True

    def test_containers_subpath_is_bind(self):
        assert (
            _is_container_bind_mount("/var/lib/docker/containers/abc123/resolv.conf")
            is True
        )

    def test_containers_subpath_with_host_root_prefix(self):
        assert (
            _is_container_bind_mount(
                "/host_root/var/lib/docker/containers/abc123/resolv.conf"
            )
            is True
        )

    def test_real_mount_is_not_bind(self):
        assert _is_container_bind_mount("/") is False
        assert _is_container_bind_mount("/mnt/data") is False
        assert _is_container_bind_mount("/host_root") is False

    def test_volume_path_is_not_bind(self):
        # Docker volume _data paths are NOT container-internal bind mounts
        # — they are dealt with by device-level de-duplication.
        assert (
            _is_container_bind_mount("/var/lib/docker/volumes/chat-data/_data") is False
        )


# ---------------------------------------------------------------------------
# _pick_canonical
# ---------------------------------------------------------------------------


class TestPickCanonical:
    def test_single_entry_returned(self):
        d = DiskInfo("/dev/sdb1", "/", "ext4", 1000, 500, 500)
        assert _pick_canonical([d]) is d

    def test_prefers_non_bind_mount(self):
        bind = DiskInfo("/dev/sdb1[/data]", "/data", "ext4", 1000, 500, 500)
        real = DiskInfo("/dev/sdb1", "/", "ext4", 1000, 500, 500)
        assert _pick_canonical([bind, real]) is real

    def test_shortest_path_when_all_non_bind(self):
        long_mp = DiskInfo(
            "/dev/sdb1", "/very/long/mount/point", "ext4", 1000, 500, 500
        )
        short_mp = DiskInfo("/dev/sdb1", "/", "ext4", 1000, 500, 500)
        assert _pick_canonical([long_mp, short_mp]) is short_mp

    def test_shortest_path_when_all_bind(self):
        a = DiskInfo("/dev/sdb1[/long/path]", "/mnt/a", "ext4", 1000, 500, 500)
        b = DiskInfo("/dev/sdb1[/sh]", "/b", "ext4", 1000, 500, 500)
        assert _pick_canonical([a, b]) is b

    def test_nonzero_total_beats_zero_when_same_path_length(self):
        zero = DiskInfo("/dev/sdb1[/vol]", "/mnt/x", "ext4", 0, 0, 0)
        nonzero = DiskInfo("/dev/sdb1[/vol]", "/mnt/y", "ext4", 2000, 1000, 1000)
        assert _pick_canonical([zero, nonzero]) is nonzero


# ---------------------------------------------------------------------------
# _deduplicate_disks
# ---------------------------------------------------------------------------


class TestDeduplicateDisks:
    def test_empty_list(self):
        assert _deduplicate_disks([]) == []

    def test_no_duplicates_unchanged(self):
        disks = [
            DiskInfo("/dev/sda1", "/", "ext4", 1000, 500, 500),
            DiskInfo("/dev/sdb1", "/mnt/data", "ext4", 2000, 1000, 1000),
        ]
        result = _deduplicate_disks(disks)
        assert len(result) == 2
        assert {d.device for d in result} == {"/dev/sda1", "/dev/sdb1"}

    def test_filters_resolv_conf(self):
        disks = [
            DiskInfo(
                "/dev/sdb1[/etc/resolv.conf]",
                "/etc/resolv.conf",
                "ext4",
                1000,
                500,
                500,
            ),
            DiskInfo("/dev/sdb1", "/host_root", "ext4", 1000, 500, 500),
        ]
        result = _deduplicate_disks(disks)
        assert len(result) == 1
        assert result[0].mount_point == "/host_root"

    def test_filters_hostname_and_hosts(self):
        disks = [
            DiskInfo(
                "/dev/sdb1[/etc/hostname]", "/etc/hostname", "ext4", 1000, 500, 500
            ),
            DiskInfo("/dev/sdb1[/etc/hosts]", "/etc/hosts", "ext4", 1000, 500, 500),
            DiskInfo("/dev/sdb1", "/host_root", "ext4", 1000, 500, 500),
        ]
        result = _deduplicate_disks(disks)
        assert len(result) == 1
        assert result[0].mount_point == "/host_root"

    def test_filters_docker_containers_paths(self):
        disks = [
            DiskInfo(
                "/dev/sdb1[/containers/abc/hosts]",
                "/var/lib/docker/containers/abc/hosts",
                "ext4",
                1000,
                500,
                500,
            ),
            DiskInfo("/dev/sdb1", "/host_root", "ext4", 1000, 500, 500),
        ]
        result = _deduplicate_disks(disks)
        assert len(result) == 1
        assert result[0].mount_point == "/host_root"

    def test_deduplicates_by_backing_device(self):
        # Simulate the ticket scenario: /dev/sdb1 has root mount + bind mounts
        disks = [
            DiskInfo(
                "/dev/sdb1",
                "/host_root",
                "ext4",
                78_600_000_000,
                61_600_000_000,
                17_000_000_000,
            ),
            DiskInfo(
                "/dev/sdb1[/data]",
                "/data",
                "ext4",
                78_600_000_000,
                61_600_000_000,
                17_000_000_000,
            ),
            DiskInfo(
                "/dev/sdb1[/var/lib/docker/volumes/x/_data]",
                "/host_root/var/lib/docker/volumes/x/_data",
                "ext4",
                0,
                0,
                0,
            ),
        ]
        result = _deduplicate_disks(disks)
        assert len(result) == 1
        # Keeps the non-bind mount with the shortest path and real data.
        assert result[0].mount_point == "/host_root"
        assert result[0].total_bytes == 78_600_000_000

    def test_multiple_devices_kept_separate(self):
        disks = [
            DiskInfo(
                "/dev/sda1",
                "/host_root/data2",
                "ext4",
                195_800_000_000,
                30_000_000_000,
                165_800_000_000,
            ),
            DiskInfo(
                "/dev/sdb1",
                "/host_root",
                "ext4",
                78_600_000_000,
                61_600_000_000,
                17_000_000_000,
            ),
            DiskInfo(
                "/dev/sdb15",
                "/host_root/boot/efi",
                "vfat",
                123_700_000,
                11_800_000,
                111_900_000,
            ),
            DiskInfo(
                "/dev/sdb1[/data]",
                "/data",
                "ext4",
                78_600_000_000,
                61_600_000_000,
                17_000_000_000,
            ),
            DiskInfo(
                "/dev/sda1[/var/lib/docker/volumes/chat/_data]",
                "/host_root/var/lib/docker/volumes/chat/_data",
                "ext4",
                0,
                0,
                0,
            ),
        ]
        result = _deduplicate_disks(disks)
        assert len(result) == 3
        devices = {d.device for d in result}
        assert devices == {"/dev/sda1", "/dev/sdb1", "/dev/sdb15"}
        mount_points = {d.mount_point for d in result}
        assert mount_points == {"/host_root", "/host_root/boot/efi", "/host_root/data2"}

    def test_result_sorted_by_mount_point(self):
        disks = [
            DiskInfo("/dev/sdb1", "/mnt/z_last", "ext4", 1000, 500, 500),
            DiskInfo("/dev/sda1", "/a_first", "ext4", 1000, 500, 500),
        ]
        result = _deduplicate_disks(disks)
        assert result[0].mount_point == "/a_first"
        assert result[1].mount_point == "/mnt/z_last"


# ---------------------------------------------------------------------------
# _discover_via_proc_mounts
# ---------------------------------------------------------------------------


class TestDiscoverViaProcMounts:
    def test_parses_proc_mounts(self, monkeypatch, tmp_path):
        proc_mounts = tmp_path / "proc_mounts"
        proc_mounts.write_text(
            "/dev/sda1 / ext4 rw,relatime 0 0\n"
            "/dev/sdb1 /mnt/data xfs rw,relatime 0 0\n"
            "tmpfs /dev/shm tmpfs rw,nosuid,nodev 0 0\n"
            "/dev/loop0 /snap/core/123 squashfs ro 0 0\n"
        )
        monkeypatch.setattr(
            "builtins.open",
            lambda path, mode="r": io.open(str(proc_mounts)),  # noqa: UP020, SIM115
        )
        import shutil as shutil_mod

        monkeypatch.setattr(
            shutil_mod,
            "disk_usage",
            lambda path: DiskUsage(1000, 500, 500),
        )
        disks = _discover_via_proc_mounts()
        # tmpfs excluded (pseudo), loop excluded
        assert len(disks) == 2
        assert {d.device for d in disks} == {"/dev/sda1", "/dev/sdb1"}

    def test_proc_mounts_unreadable_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "builtins.open",
            lambda path, mode="r": (_ for _ in ()).throw(OSError()),
        )
        disks = _discover_via_proc_mounts()
        assert disks == []

    def test_disk_usage_error_skips_entry(self, monkeypatch, tmp_path):
        proc_mounts = tmp_path / "proc_mounts"
        proc_mounts.write_text("/dev/sda1 / ext4 rw 0 0\n")
        monkeypatch.setattr(
            "builtins.open",
            lambda path, mode="r": io.open(str(proc_mounts)),  # noqa: UP020, SIM115
        )
        import shutil as shutil_mod

        monkeypatch.setattr(
            shutil_mod,
            "disk_usage",
            lambda path: (_ for _ in ()).throw(OSError()),
        )
        disks = _discover_via_proc_mounts()
        assert disks == []
