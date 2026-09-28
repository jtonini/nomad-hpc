#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Workstation mount probe for NØMAÐ.

Reads /proc/mounts, filters to NFS mounts + non-system local mounts, and
for each one runs an independent stat() with a strict wall-clock timeout.
A dead NFS mount will hang stat() forever; the timeout catches that and
reports the mount as unresponsive even though /proc/mounts still lists it.

Output: one JSON object per line on stdout. Each object corresponds to
one mount. Non-interesting mounts (proc, sys, cgroup, tmpfs, etc.) are
skipped.

Ship this file alongside cgroup_probe.py. Designed to run standalone as a
script (`python3 mount_probe.py`) so the collector can scp it to remote
hosts and invoke it over ssh, exactly like cgroup_probe.py.

Python 3.6+. No third-party dependencies.

Schema (mirrors workstation_mount_state DB table):

    hostname:       str  (from socket.gethostname)
    mountpoint:     str  (absolute path)
    fstype:         str  ("nfs", "nfs4", "ext4", "xfs", ...)
    source:         str  (for NFS: "server:/export"; else device path)
    is_mounted:     int  (always 1 for emitted rows; 0 only if we knew
                          of a required mount and it vanished — not
                          implemented in this minimal version)
    is_responsive:  int  (1 if stat() returned within the timeout)
    response_ms:    float (milliseconds stat() took, or timeout_ms on timeout)
    collected_at:   int  (unix timestamp)
    probe_version:  str  ("1")
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time


PROBE_VERSION = "1"

# Filesystem types that are definitely NOT user-facing storage.
# These are either kernel-internal (cgroup, proc, sys) or local
# pseudo-filesystems that don't benefit from responsiveness checks
# (tmpfs for /tmp, /run — if these hang, you have much bigger problems
# than anything we'd want to report).
SKIP_FSTYPES = {
    "proc", "sysfs", "cgroup", "cgroup2", "devpts", "devtmpfs",
    "tmpfs", "mqueue", "hugetlbfs", "pstore", "bpf", "debugfs",
    "tracefs", "fusectl", "configfs", "securityfs", "rpc_pipefs",
    "autofs", "binfmt_misc", "fuse.gvfsd-fuse", "fuse.portal",
    "ramfs", "squashfs",  # read-only distro stuff
    "overlay",            # container storage, not user data
    "nsfs", "selinuxfs", "efivarfs",
}

# Mountpoints that are conventionally system-owned and not user-facing.
# Defensive skip — even if the fstype isn't in SKIP_FSTYPES, these
# paths aren't interesting for a "can users access their files" check.
SKIP_MOUNTPOINT_PREFIXES = (
    "/proc", "/sys", "/dev", "/run", "/boot", "/var/lib/docker",
    "/var/lib/containers", "/var/lib/kubelet", "/snap",
)

# Treat these as always-interesting even if something weird happens.
# Any NFS variant gets included regardless of mountpoint.
NFS_FSTYPES = {"nfs", "nfs3", "nfs4", "cifs", "smb", "smb3"}

# Default wall-clock timeout for a single stat() call, in seconds.
# NFS clients use the filesystem's own RTO which can be tens of seconds;
# 3s is aggressive but catches most "hanging" mounts without being jumpy.
DEFAULT_STAT_TIMEOUT_SEC = 3.0


class StatTimeout(Exception):
    """Raised when stat() exceeds the wall-clock budget."""


def _parse_proc_mounts():
    """Yield (source, mountpoint, fstype, options) tuples.

    /proc/mounts format is 6 space-separated fields per line. Octal
    escapes (\\040 for space in mountpoint names) are decoded.
    """
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.rstrip("\n").split()
                if len(parts) < 4:
                    continue
                source, mountpoint, fstype, options = parts[0], parts[1], parts[2], parts[3]
                # Decode octal escapes in paths (e.g., "\\040" for space)
                mountpoint = _decode_escapes(mountpoint)
                source = _decode_escapes(source)
                yield source, mountpoint, fstype, options
    except OSError:
        return


def _decode_escapes(s):
    """Decode /proc/mounts octal escapes."""
    if "\\" not in s:
        return s
    out = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 3 < len(s) and s[i+1:i+4].isdigit():
            try:
                out.append(chr(int(s[i+1:i+4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(s[i])
        i += 1
    return "".join(out)


def _is_interesting(source, mountpoint, fstype):
    """Decide whether this mount is worth checking."""
    # Always include NFS variants
    if fstype in NFS_FSTYPES:
        return True
    # Skip kernel-internal and pseudo-filesystems
    if fstype in SKIP_FSTYPES:
        return False
    # Skip system-owned mountpoints
    for prefix in SKIP_MOUNTPOINT_PREFIXES:
        if mountpoint == prefix or mountpoint.startswith(prefix + "/"):
            return False
    # Root and user-facing storage: worth monitoring
    # (ext4, xfs, zfs, btrfs on / or /home or /scratch or wherever)
    return True


def _check_mount_responsive(mountpoint, timeout_sec):
    """Run stat() on a mountpoint with a hard wall-clock timeout.

    Returns (is_responsive: bool, elapsed_ms: float).

    Implementation uses SIGALRM (Unix). Cannot be called from
    non-main threads. The collector invokes this probe in its own
    subprocess, so that's fine.
    """
    def _handler(signum, frame):
        raise StatTimeout()

    # Install alarm handler. Save previous handler to restore afterwards
    # so repeated calls don't stack.
    prev_handler = signal.signal(signal.SIGALRM, _handler)
    # SIGALRM resolution is 1 second; use setitimer for sub-second precision.
    signal.setitimer(signal.ITIMER_REAL, timeout_sec)

    start = time.monotonic()
    try:
        os.stat(mountpoint)
        elapsed_ms = (time.monotonic() - start) * 1000.0
        return True, elapsed_ms
    except StatTimeout:
        elapsed_ms = timeout_sec * 1000.0
        return False, elapsed_ms
    except OSError as e:
        # Permission denied, ENOENT, etc. Mount exists per /proc/mounts but
        # we can't access it. That's still informative — report as
        # responsive (stat returned) but include the error type in a
        # future enhancement. For this minimal version: treat as
        # unresponsive so it flags in the dashboard.
        elapsed_ms = (time.monotonic() - start) * 1000.0
        # Use a small negative sentinel to signal "error", but keep
        # response_ms positive in output:
        return False, elapsed_ms
    finally:
        # Cancel pending alarm and restore handler
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev_handler)


def probe(stat_timeout_sec=DEFAULT_STAT_TIMEOUT_SEC):
    """Yield per-mount JSON-ready dicts for each interesting mount.

    stat_timeout_sec applies to each mount independently.
    """
    hostname = socket.gethostname()
    collected_at = int(time.time())

    for source, mountpoint, fstype, _options in _parse_proc_mounts():
        if not _is_interesting(source, mountpoint, fstype):
            continue

        is_responsive, response_ms = _check_mount_responsive(
            mountpoint, stat_timeout_sec)

        yield {
            "hostname": hostname,
            "mountpoint": mountpoint,
            "fstype": fstype,
            "source": source,
            "is_mounted": 1,
            "is_responsive": 1 if is_responsive else 0,
            "response_ms": round(response_ms, 2),
            "collected_at": collected_at,
            "probe_version": PROBE_VERSION,
        }


def main(argv=None):
    argv = argv or sys.argv[1:]
    timeout = DEFAULT_STAT_TIMEOUT_SEC
    # Minimal CLI: --timeout SECONDS
    if argv:
        if argv[0] == "--timeout" and len(argv) >= 2:
            try:
                timeout = float(argv[1])
            except ValueError:
                print("error: --timeout requires a numeric value",
                      file=sys.stderr)
                return 2
        elif argv[0] in ("-h", "--help"):
            print(__doc__)
            return 0

    rows = list(probe(stat_timeout_sec=timeout))
    for row in rows:
        print(json.dumps(row))

    # Also emit a summary line to stderr so ad-hoc invocation is readable
    count_ok = sum(1 for r in rows if r["is_responsive"])
    count_bad = len(rows) - count_ok
    print(f"{len(rows)} mount(s) reported: {count_ok} responsive, "
          f"{count_bad} unresponsive", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
