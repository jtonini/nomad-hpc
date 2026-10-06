# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Tests for the mount probe: nomad/collectors/mount_probe.py.

Self-contained — tests exercise pure-Python helpers with string fixtures
and real filesystem calls to /tmp. No root required, no remote hosts.

Run standalone:  python3 tests/test_mount_probe.py
Run via pytest:  pytest tests/test_mount_probe.py -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Path-independent import (mirrors test_workstation_user_collectors.py)
THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
import sys
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nomad.collectors import mount_probe as mp


# -------- Sample /proc/mounts snippets, taken from real systems --------

# Typical Rocky Linux 10.1 workstation (modeled on aamy)
PROC_MOUNTS_TYPICAL = """\
/dev/nvme0n1p5 / xfs rw,relatime,attr2,inode64,logbufs=8 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0
devtmpfs /dev devtmpfs rw,nosuid,seclabel,size=4096k,nr_inodes=8192 0 0
tmpfs /run tmpfs rw,nosuid,nodev,seclabel,size=393216k 0 0
cgroup2 /sys/fs/cgroup cgroup2 rw,nosuid,nodev,noexec,relatime 0 0
/dev/sda3 /home xfs rw,relatime,attr2,inode64,logbufs=8 0 0
/dev/sda4 /scratch xfs rw,relatime,attr2,inode64,logbufs=8 0 0
141.166.186.35:/mnt/usrlocal/intel-tools /opt/intel nfs ro,relatime,vers=4.2 0 0
141.166.186.35:/mnt/usrlocal/8 /usr/local/chem.sw nfs ro,relatime,vers=4.2 0 0
tmpfs /tmp tmpfs rw,nosuid,nodev,seclabel 0 0
devpts /dev/pts devpts rw,nosuid,noexec,relatime 0 0
"""

# Exotic / edge cases: octal escapes, SMB share, sshfs, overlay
PROC_MOUNTS_EDGE = r"""\
/dev/sda1 /mnt/with\040space ext4 rw,relatime 0 0
//fileserver/share /mnt/smbshare cifs rw,vers=3.0 0 0
admin@box:/remote /mnt/remote fuse.sshfs rw,user_id=1000,group_id=1000 0 0
overlay /var/lib/docker/overlay2/a overlay rw,lowerdir=/x,upperdir=/y,workdir=/z 0 0
"""


class TestDecodeEscapes(unittest.TestCase):
    """_decode_escapes: /proc/mounts path escape handling."""

    def test_no_escapes_passthrough(self):
        self.assertEqual(mp._decode_escapes("/home"), "/home")

    def test_space_escape(self):
        self.assertEqual(mp._decode_escapes(r"/mnt/with\040space"), "/mnt/with space")

    def test_tab_escape(self):
        self.assertEqual(mp._decode_escapes(r"/mnt/with\011tab"), "/mnt/with\ttab")

    def test_partial_backslash_passthrough(self):
        # A backslash not followed by 3 octal digits is left alone
        self.assertEqual(mp._decode_escapes(r"/path\with\backslash"),
                         r"/path\with\backslash")


class TestParseProcMounts(unittest.TestCase):
    """_parse_proc_mounts: reads /proc/mounts via open(), so patch that."""

    def _parse_from_string(self, contents):
        """Helper: simulate reading /proc/mounts from a string."""
        import io
        with mock.patch("builtins.open", mock.mock_open(read_data=contents)):
            return list(mp._parse_proc_mounts())

    def test_parses_typical_content(self):
        rows = self._parse_from_string(PROC_MOUNTS_TYPICAL)
        # At minimum we expect the root and the NFS entries
        mountpoints = [r[1] for r in rows]
        self.assertIn("/", mountpoints)
        self.assertIn("/home", mountpoints)
        self.assertIn("/opt/intel", mountpoints)
        self.assertIn("/usr/local/chem.sw", mountpoints)

    def test_yields_four_tuple(self):
        rows = self._parse_from_string(PROC_MOUNTS_TYPICAL)
        for r in rows:
            self.assertEqual(len(r), 4, f"Expected 4-tuple, got {r}")
            source, mountpoint, fstype, options = r
            self.assertIsInstance(source, str)
            self.assertIsInstance(mountpoint, str)
            self.assertIsInstance(fstype, str)
            self.assertIsInstance(options, str)

    def test_nfs_source_preserved(self):
        rows = self._parse_from_string(PROC_MOUNTS_TYPICAL)
        for source, mp_path, fstype, _ in rows:
            if mp_path == "/opt/intel":
                self.assertEqual(source, "141.166.186.35:/mnt/usrlocal/intel-tools")
                self.assertEqual(fstype, "nfs")
                return
        self.fail("/opt/intel not found in parsed output")

    def test_handles_escaped_space_in_mountpoint(self):
        rows = self._parse_from_string(PROC_MOUNTS_EDGE)
        mountpoints = [r[1] for r in rows]
        self.assertIn("/mnt/with space", mountpoints)

    def test_handles_missing_proc_mounts(self):
        """If /proc/mounts can't be opened, yields nothing (doesn't raise)."""
        with mock.patch("builtins.open", side_effect=OSError("nope")):
            rows = list(mp._parse_proc_mounts())
        self.assertEqual(rows, [])


class TestIsInteresting(unittest.TestCase):
    """_is_interesting: categorize which mounts are worth monitoring."""

    def test_nfs_variants_always_interesting(self):
        for fs in ["nfs", "nfs3", "nfs4"]:
            self.assertTrue(
                mp._is_interesting("server:/export", "/home", fs),
                f"{fs} should be interesting"
            )

    def test_cifs_smb_interesting(self):
        self.assertTrue(
            mp._is_interesting("//server/share", "/mnt/share", "cifs"))
        self.assertTrue(
            mp._is_interesting("//server/share", "/mnt/share", "smb3"))

    def test_pseudo_filesystems_skipped(self):
        for fs in ["proc", "sysfs", "cgroup", "cgroup2", "devpts",
                   "devtmpfs", "tmpfs", "mqueue", "pstore", "bpf",
                   "debugfs", "tracefs", "fusectl"]:
            self.assertFalse(
                mp._is_interesting("none", "/anywhere", fs),
                f"{fs} should NOT be interesting"
            )

    def test_system_mountpoint_prefixes_skipped(self):
        # ext4 on /run/something should still be skipped
        self.assertFalse(
            mp._is_interesting("/dev/sda1", "/run/lock", "ext4"))
        self.assertFalse(
            mp._is_interesting("/dev/sda1", "/dev/shm", "ext4"))
        self.assertFalse(
            mp._is_interesting("/dev/sda1", "/boot/efi", "vfat"))

    def test_user_facing_local_fs_interesting(self):
        self.assertTrue(
            mp._is_interesting("/dev/sda3", "/home", "xfs"))
        self.assertTrue(
            mp._is_interesting("/dev/nvme0n1p5", "/", "xfs"))
        self.assertTrue(
            mp._is_interesting("/dev/sda4", "/scratch", "ext4"))

    def test_overlay_skipped(self):
        # Docker overlays etc. aren't useful to monitor
        self.assertFalse(
            mp._is_interesting("overlay", "/var/lib/docker/overlay2/x",
                               "overlay"))

    def test_snap_skipped(self):
        self.assertFalse(
            mp._is_interesting("/dev/loop0", "/snap/core/1234", "squashfs"))


class TestCheckMountResponsive(unittest.TestCase):
    """_check_mount_responsive: stat() with timeout."""

    def test_healthy_mount_returns_quickly(self):
        # /tmp always exists and is fast to stat
        ok, ms, sizes = mp._check_mount_responsive("/tmp", timeout_sec=3.0)
        self.assertTrue(ok)
        self.assertEqual(len(sizes), 3)          # total, used, avail (df's)
        self.assertLess(ms, 1000.0,
                        "stat on /tmp should return in <1s")

    def test_tmpdir_responsive(self):
        with tempfile.TemporaryDirectory() as d:
            ok, ms, _sizes = mp._check_mount_responsive(d, timeout_sec=3.0)
            self.assertTrue(ok)
            self.assertLess(ms, 1000.0)

    def test_nonexistent_path_not_responsive(self):
        # os.stat raises ENOENT; we report as not responsive.
        ok, ms, sizes = mp._check_mount_responsive(
            "/nonexistent_path_that_will_never_exist_42",
            timeout_sec=3.0,
        )
        self.assertFalse(ok)
        self.assertIsNone(sizes)
        # ms should be a non-negative number (<= timeout)
        self.assertGreaterEqual(ms, 0.0)


class TestProbe(unittest.TestCase):
    """End-to-end probe() — yields dicts with the full schema."""

    def test_probe_output_json_schema(self):
        # Mock /proc/mounts so we have predictable content.
        with mock.patch("builtins.open",
                        mock.mock_open(read_data=PROC_MOUNTS_TYPICAL)):
            rows = list(mp.probe(stat_timeout_sec=2.0))

        expected_keys = {
            "hostname", "mountpoint", "fstype", "source",
            "is_mounted", "is_responsive", "response_ms",
            "total_bytes", "used_bytes", "avail_bytes",
            "collected_at", "probe_version",
        }
        for row in rows:
            self.assertEqual(set(row.keys()), expected_keys,
                             f"Row has unexpected keys: {row}")
            self.assertIsInstance(row["hostname"], str)
            self.assertIsInstance(row["mountpoint"], str)
            self.assertIn(row["is_mounted"], (0, 1))
            self.assertIn(row["is_responsive"], (0, 1))

    def test_probe_filters_out_pseudo_filesystems(self):
        with mock.patch("builtins.open",
                        mock.mock_open(read_data=PROC_MOUNTS_TYPICAL)):
            rows = list(mp.probe(stat_timeout_sec=2.0))

        mountpoints = [r["mountpoint"] for r in rows]
        # Pseudo-filesystems should be gone
        self.assertNotIn("/proc", mountpoints)
        self.assertNotIn("/sys", mountpoints)
        self.assertNotIn("/dev", mountpoints)
        self.assertNotIn("/run", mountpoints)
        self.assertNotIn("/sys/fs/cgroup", mountpoints)
        self.assertNotIn("/tmp", mountpoints)  # tmpfs on /tmp not interesting
        self.assertNotIn("/dev/pts", mountpoints)

    def test_probe_keeps_nfs_and_user_storage(self):
        with mock.patch("builtins.open",
                        mock.mock_open(read_data=PROC_MOUNTS_TYPICAL)):
            rows = list(mp.probe(stat_timeout_sec=2.0))

        mountpoints = [r["mountpoint"] for r in rows]
        # These should all be kept
        self.assertIn("/", mountpoints)
        self.assertIn("/home", mountpoints)
        self.assertIn("/scratch", mountpoints)
        self.assertIn("/opt/intel", mountpoints)
        self.assertIn("/usr/local/chem.sw", mountpoints)

    def test_probe_output_is_json_serializable(self):
        with mock.patch("builtins.open",
                        mock.mock_open(read_data=PROC_MOUNTS_TYPICAL)):
            rows = list(mp.probe(stat_timeout_sec=2.0))

        for row in rows:
            # Must serialize cleanly — this is how the collector ingests it
            encoded = json.dumps(row)
            decoded = json.loads(encoded)
            self.assertEqual(decoded["mountpoint"], row["mountpoint"])

    def test_probe_hostname_matches_socket(self):
        import socket
        with mock.patch("builtins.open",
                        mock.mock_open(read_data=PROC_MOUNTS_TYPICAL)):
            rows = list(mp.probe(stat_timeout_sec=2.0))

        expected = socket.gethostname()
        for row in rows:
            self.assertEqual(row["hostname"], expected)


if __name__ == "__main__":
    unittest.main()
