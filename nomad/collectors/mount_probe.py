#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Workstation mount probe for NØMAÐ.

Reads /proc/mounts, filters to NFS mounts + non-system local mounts, and
checks them all at once, each in its own thread: stat(), then statvfs() for
the sizes, within a strict wall-clock timeout. A dead NFS mount hangs those
calls, and no signal short of SIGKILL interrupts them (nfs(5)); a thread
left hanging is abandoned, the mount reported as unresponsive even though
/proc/mounts still lists it, and the script ends with os._exit(), which
takes the stuck threads with it.

Output: one JSON object per line on stdout. Each object corresponds to
one mount. Non-interesting mounts (proc, sys, cgroup, tmpfs, etc.) are
skipped.

Ship this file alongside cgroup_probe.py. Designed to run standalone as a
script (`python3 mount_probe.py`) so the collector can scp it to remote
hosts and invoke it over ssh, exactly like cgroup_probe.py.

Python 3.6+ (it runs on each workstation's own python3). No third-party
dependencies.

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
    total_bytes:    int  (size of the filesystem behind the mount, as df
                          shows it; None when it did not answer)
    used_bytes:     int  (df's "Used")
    avail_bytes:    int  (df's "Avail": what users can still write)
    collected_at:   int  (unix timestamp)
    probe_version:  str  ("2"; "1" had no sizes)

For an NFS mount the sizes are the server's: the export's filesystem as the
server reports it. On a ZFS server, exports that are datasets of one pool
each report their own "used" but share the pool's free space.
"""

import json
import os
import socket
import sys
import threading
import time


PROBE_VERSION = "2"

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

# Default wall-clock timeout for one mount's check (stat, then statvfs),
# in seconds. NFS clients use the filesystem's own RTO which can be tens of
# seconds; 3s is aggressive but catches most "hanging" mounts without being
# jumpy. The mounts are checked at the same time, so a probe of several dead
# mounts still ends after about one timeout.
DEFAULT_STAT_TIMEOUT_SEC = 3.0


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


class _Check(object):
    """One mount's check, run in a daemon thread: stat(), then statvfs()."""

    def __init__(self, mountpoint):
        self.mountpoint = mountpoint
        self.started = time.monotonic()
        self.stat_ms = None          # set once stat() has answered
        self.sizes = None
        self.failed = False          # stat() raised (EACCES, ENOENT, ...)
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        try:
            self.thread.start()
            self.started_ok = True
        except RuntimeError:         # "can't start new thread": out of processes
            self.started_ok = False

    def _run(self):
        try:
            os.stat(self.mountpoint)
            self.stat_ms = (time.monotonic() - self.started) * 1000.0
            self.sizes = _sizes(os.statvfs(self.mountpoint))
        except Exception:
            if self.stat_ms is None:
                self.failed = True
        finally:
            self.finished.set()

    def result(self, timeout_sec):
        """(is_responsive, response_ms, sizes) once it finished or its time
        ran out; a thread still hanging then is abandoned. None when the
        check could not start: nothing is known about the mount this run."""
        if not self.started_ok:
            return None
        left = self.started + timeout_sec - time.monotonic()
        self.finished.wait(max(left, 0))
        stat_ms = self.stat_ms
        if stat_ms is not None:      # stat() answered; sizes may not have
            return True, stat_ms, (self.sizes if self.finished.is_set() else None)
        if self.failed:
            # Permission denied, ENOENT, etc. Mount exists per /proc/mounts
            # but we can't access it: treat as unresponsive so it flags in
            # the dashboard.
            return False, (time.monotonic() - self.started) * 1000.0, None
        return False, timeout_sec * 1000.0, None


def _check_mount_responsive(mountpoint, timeout_sec):
    """Run stat() on a mountpoint with a hard wall-clock timeout.

    Returns (is_responsive: bool, elapsed_ms: float, sizes), where sizes is
    (total_bytes, used_bytes, avail_bytes) from statvfs() -- df's numbers --
    or None. statvfs() runs only once stat() has answered, inside the same
    timeout; a mount that stops answering between the two counts as
    responsive (stat() answered) with no sizes. When no thread can be
    started for the check, it runs here, without the timeout.
    """
    check = _Check(mountpoint)
    if check.started_ok:
        return check.result(timeout_sec)
    check._run()
    if check.stat_ms is not None:
        return True, check.stat_ms, check.sizes
    return False, (time.monotonic() - check.started) * 1000.0, None


def _sizes(st):
    """(total, used, avail) bytes from a statvfs result, as df computes them;
    None for a filesystem that reports no size (some pseudo and fuse ones)."""
    if not st.f_blocks or not st.f_frsize:
        return None
    unit = st.f_frsize
    return (st.f_blocks * unit, (st.f_blocks - st.f_bfree) * unit, st.f_bavail * unit)


def probe(stat_timeout_sec=DEFAULT_STAT_TIMEOUT_SEC):
    """Yield per-mount JSON-ready dicts for each interesting mount.

    stat_timeout_sec applies to each mount independently.
    """
    hostname = socket.gethostname()
    collected_at = int(time.time())

    # All at once: a dead server's mounts each hang their own thread.
    checks = [(source, mountpoint, fstype, _Check(mountpoint))
              for source, mountpoint, fstype, _options in _parse_proc_mounts()
              if _is_interesting(source, mountpoint, fstype)]
    for source, mountpoint, fstype, check in checks:
        result = check.result(stat_timeout_sec)
        if result is None:
            # Not "not responding": unknown. Better no row than a false alarm.
            print(f"{mountpoint}: not checked (no thread could be started)",
                  file=sys.stderr)
            continue
        is_responsive, response_ms, sizes = result
        total_bytes, used_bytes, avail_bytes = sizes or (None, None, None)

        yield {
            "hostname": hostname,
            "mountpoint": mountpoint,
            "fstype": fstype,
            "source": source,
            "is_mounted": 1,
            "is_responsive": 1 if is_responsive else 0,
            "response_ms": round(response_ms, 2),
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "avail_bytes": avail_bytes,
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


def _run():
    """As a script: a check still hanging must not keep the process (and the
    collector's ssh session) open, so it ends with os._exit()."""
    try:
        code = main()
    except SystemExit as e:
        code = 0 if e.code is None else e.code if isinstance(e.code, int) else 1
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    _run()
