# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""How fast each CPU really runs when busy (1.27.0): a CPU held far below its
clock (a power brake, PROCHOT) is found the day it starts.

arachne's node01-03 lost one power supply's input on 4 October 2026 and ran
on the other; their boards answered by braking the CPUs to their lowest
clock, about 400 MHz instead of 3,700. Nothing failed and Slurm saw healthy
nodes: every job there ran ten times slower for a month. What the OS reports
(/proc/cpuinfo, scaling_cur_freq) is the clock it asked for, which can say
3,100 MHz meanwhile; the CPU's own cycle counter says what it gets.

Each read, every host runs a short busy loop (``seconds``, 0.5 of CPU time)
in its own python3 (3.6 or later, standard library, no root): the CPU's
cycles in user mode (perf_event_open for that process only, allowed at the
kernel's default perf_event_paranoid of 2) over that CPU time is the clock it
runs at, busy. A host where the counter can't be read (a virtual machine, a
paranoid setting of 3) gives its sha256 rate per CPU-second instead, judged
against its own past and hosts like it. The hosts: as the temps collector's
(``hosts``, ``workstations``, ``slurm_nodes``), one ssh call each, ``parallel``
at once, every ``every_minutes`` (15).

Every CPU package is measured, the lowest counting. A reading is low below
``min_fraction`` (0.5) of the most it should give: the highest of its own
usual clock (the 90th percentile of its last 30 days' readings that weren't
low), the most a host alike gives now (alike()), and the CPU's top clock
(cpufreq's maximum, or an Intel model's rated clock); a low clock counts
only when the sha256 rate agrees, where there is one to compare it with;
judged by the sha256 rate alone, below ``sha_min_fraction`` (0.25). A host is
throttled after ``sustain`` (2) low readings in a row: alerted (source
``clock``, subject "cpu clock"), critical when its power readings show a
supply problem too (the power collector, on the same site). Its board
unreadable in-band (no /dev/ipmi0) is said in the alert: its supplies can't be
seen from here.

    [collectors.clock]
    enabled = true
    slurm_nodes = true                 # a head node: every node Slurm lists that answers
    # hosts = ["localhost"]            # and/or these, over ssh
    # workstations = true              # and the workstation collector's
    # every_minutes = 15
    # seconds = 0.5                    # CPU time of each busy loop
    # parallel = 8
    # max_seconds = 60
    # timeout = 20
    # keep_days = 90

    [alerts.clock]
    # min_fraction = 0.5
    # sustain = 2
    # ignore = ["gpu0*"]               # hosts not alerted (shell patterns)
    [alerts.clock.node07]
    # min_fraction = 0.3               # a host's own

Tables: ``cpu_clock`` (each reading), ``cpu_clock_hosts`` (each host now:
its clock, the reference and where it came from, low since, throttled).
"""
from __future__ import annotations

import base64
import fnmatch
import logging
import re
import shlex
import sqlite3
import statistics
import subprocess
from datetime import datetime, timedelta
from typing import Any

from .base import CollectionError, registry
from .temps import TempsCollector

logger = logging.getLogger(__name__)

MARK = "__NOMAD_CLOCK__"
EVERY_MINUTES = 15
SECONDS = 0.5
KEEP_DAYS = 90
HISTORY_DAYS = 30
PEER_HOURS = 2             # hosts like it: their readings this recent
# min_fraction: of the clock; sha_min_fraction: of the sha256 rate, where there is no counter (a rate
# shared with a busy hyperthread sibling, or measured against an idle past, falls further than a clock
# does: braked node02 gave 6% of node51's)
DEFAULTS = {"enabled": True, "min_fraction": 0.5, "sha_min_fraction": 0.25, "sustain": 2}

# The probe, run by the host's own python3 (3.6+): one line, CLOCK|key=value|...
# Each socket is measured (a brake is per package: one socket braked slows every job spanning
# it), on the first CPU this process may use there. The counter counts from its opening and is
# read before and after (no ioctls: their numbers differ by architecture), with the time it was
# enabled and running: a count shared with another tool (multiplexed) is not trusted.
PROBE = r'''
import ctypes, hashlib, os, platform, struct, sys, time
out = {"v": 2}
def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None
try:
    allowed = sorted(os.sched_getaffinity(0))
except (AttributeError, OSError):
    allowed = list(range(os.cpu_count() or 1))
first = {}
for c in allowed:
    first.setdefault(read("/sys/devices/system/cpu/cpu%d/topology/physical_package_id" % c) or "0", c)
model, flags = None, ""
for line in (read("/proc/cpuinfo") or "").splitlines():
    key = line.split(":")[0].strip()
    if key in ("model name", "Model name", "cpu model") and model is None:
        model = line.split(":", 1)[1].strip().replace("|", "/")
    elif key == "flags" and not flags:
        flags = " " + line.split(":", 1)[1] + " "
out["model"] = model
out["vm"] = int(" hypervisor " in flags)
out["sha_ni"] = int(" sha_ni " in flags)
out["cpus"] = os.cpu_count()
out["pkgs"] = len(first)
out["load1"] = round(os.getloadavg()[0], 2)
cpu0 = min(first.values()) if first else 0
cf = "/sys/devices/system/cpu/cpu%d/cpufreq/" % cpu0
for key, name in (("max_khz", "cpuinfo_max_freq"), ("os_khz", "scaling_cur_freq")):
    v = read(cf + name)
    out[key] = int(v) if v and v.isdigit() else None
for key, name in (("driver", "scaling_driver"), ("governor", "scaling_governor")):
    v = read(cf + name)
    out[key] = v.replace("|", "/") if v else None
SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
def busy(seconds):
    t0 = time.process_time()
    n = 0
    while time.process_time() - t0 < seconds:
        for i in range(2000):
            n = (n * 1103515245 + 12345) & 0x7fffffff
    return time.process_time() - t0
def pin(c):
    try:
        os.sched_setaffinity(0, {c})
    except (AttributeError, OSError, ValueError):
        pass
NR = {"x86_64": 298, "aarch64": 241, "ppc64le": 319}.get(platform.machine())
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
def clock_on(c):
    # struct perf_event_attr (112 bytes): hardware, CPU cycles, read_format total time enabled and
    # running, exclude_kernel and exclude_hv; counting this process from now
    attr = ctypes.create_string_buffer(struct.pack("=IIQQQQQIIQQQQIiQIHH", 0, 112, 0, 0, 0, 1 | 2, 32 | 64,
                                                   0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0), 112)
    pin(c)
    busy(0.05)                                     # the clock up first
    fd = libc.syscall(NR, attr, 0, -1, -1, 0)
    if fd < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    try:
        v0, e0, r0 = struct.unpack("=QQQ", os.read(fd, 24))
        spent = busy(SECONDS)
        v1, e1, r1 = struct.unpack("=QQQ", os.read(fd, 24))
    finally:
        os.close(fd)
    if e1 - e0 <= 0 or (r1 - r0) / float(e1 - e0) < 0.99:
        raise OSError("the cycle counter is shared with another tool now")
    return int(round((v1 - v0) / spent / 1e6)) if spent > 0 and v1 > v0 else None
by = []
try:
    if NR is None:
        raise OSError("no perf_event_open number for " + platform.machine())
    for pkg in sorted(first, key=lambda k: first[k]):
        mhz = clock_on(first[pkg])
        if mhz:
            by.append((mhz, pkg))
    if not by:
        raise OSError("the cycle counter read 0")
    out["mhz"] = min(by)[0]
    out["mhz_pkgs"] = ",".join("%s:%d" % (pkg, mhz) for mhz, pkg in sorted(by, key=lambda x: x[1]))
except Exception as e:
    out["mhz"] = None
    out["perf"] = str(e)[:120].replace("|", "/")
pin(first[min(by)[1]] if by else cpu0)            # the sha256 rate where the clock is lowest
block = b"\0" * 16384
h = hashlib.sha256()
t0, n = time.process_time(), 0
while time.process_time() - t0 < SECONDS / 2:
    for i in range(16):
        h.update(block)
    n += 16
spent = time.process_time() - t0
out["sha_mbps"] = round(n * 16384 / spent / 1e6, 1) if spent > 0 else None
print("CLOCK|" + "|".join("%s=%s" % (k, out[k]) for k in sorted(out)))
'''.strip()

_INTEL_RATED = re.compile(r"@\s*([0-9.]+)\s*GHz", re.I)


def remote_command(seconds: float = SECONDS) -> str:
    """One line of POSIX sh: the host's python3 (else EL8's platform-python)
    runs the probe, isolated (-I: nothing of the login's Python settings)."""
    # one line, whatever the login shell (csh can't take a newline inside quotes): the probe in base64
    code = base64.b64encode(PROBE.encode()).decode()
    return ('P=$(command -v python3 2>/dev/null); [ -n "$P" ] || P=/usr/libexec/platform-python; '
            '[ -x "$P" ] || { echo "CLOCK|error=no python3 here"; exit 0; }; '
            f'exec "$P" -I -c "import base64;exec(base64.b64decode(\'{code}\'))" {float(seconds):g}')


def _num(text, kind=float):
    try:
        return kind(text) if text not in (None, "", "None") else None
    except (TypeError, ValueError):
        return None


def parse(node: str, output: str, when: str) -> dict[str, Any]:
    """The probe's line as a reading; CollectionError when it has none."""
    line = next((x for x in (output or "").splitlines() if x.startswith("CLOCK|")), None)
    if line is None:
        raise CollectionError(f"{node}: no answer from the probe" + (f" ({output.strip()[-160:]})"
                                                                     if (output or "").strip() else ""))
    f = dict(p.split("=", 1) for p in line.split("|")[1:] if "=" in p)
    if f.get("error"):
        raise CollectionError(f"{node}: {f['error']}")
    max_khz, os_khz = _num(f.get("max_khz"), int), _num(f.get("os_khz"), int)
    return {"timestamp": when, "node": node, "model": (f.get("model") or None) if f.get("model") != "None" else None,
            "cpus": _num(f.get("cpus"), int), "load1": _num(f.get("load1")), "mhz": _num(f.get("mhz"), int),
            "sha_mbps": _num(f.get("sha_mbps")), "max_mhz": round(max_khz / 1000) if max_khz else None,
            "os_mhz": round(os_khz / 1000) if os_khz else None,
            "driver": f.get("driver") if f.get("driver") not in (None, "None") else None,
            "governor": f.get("governor") if f.get("governor") not in (None, "None") else None,
            "perf": f.get("perf") or None, "vm": _num(f.get("vm"), int), "sha_ni": _num(f.get("sha_ni"), int),
            "pkgs": _num(f.get("pkgs"), int), "mhz_pkgs": f.get("mhz_pkgs") or None}


def rules(alerts_clock: dict | None, node: str) -> dict:
    """[alerts.clock] over the defaults, then the host's own table."""
    raw = alerts_clock if isinstance(alerts_clock, dict) else {}
    out = dict(DEFAULTS)
    short = node.split(".")[0]
    for layer in [raw] + [raw[k] for k in (node, short) if isinstance(raw.get(k), dict)][:1]:
        if isinstance(layer.get("enabled"), bool):
            out["enabled"] = layer["enabled"]
        for key in ("min_fraction", "sha_min_fraction"):
            v = layer.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 1:
                out[key] = float(v)
        v = layer.get("sustain")
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 20:
            out["sustain"] = v
    ignore = raw.get("ignore") or []
    if any(isinstance(p, str) and (fnmatch.fnmatchcase(node, p) or fnmatch.fnmatchcase(short, p))
           for p in (ignore if isinstance(ignore, list) else [ignore])):
        out["enabled"] = False
    return out


def _p90(values: list) -> float | None:
    v = sorted(x for x in values if x)
    if len(v) < 4:
        return None
    return v[min(len(v) - 1, int(round(0.9 * (len(v) - 1))))]


def alike(a: dict, b: dict) -> bool:
    """Hosts to compare: the same CPU model, both real or both virtual, with
    the same sha256 instructions (a VM's model name, "Common KVM processor",
    covers any hardware)."""
    return bool(a.get("model")) and a.get("model") == b.get("model") and not a.get("vm") and not b.get("vm") \
        and (a.get("sha_ni") is None or b.get("sha_ni") is None or a.get("sha_ni") == b.get("sha_ni"))


def reference(r: dict, own: list[dict], peers: list[dict]) -> tuple[float | None, str | None, str]:
    """(the most this host should give, where that comes from, "mhz" or
    "sha_mbps"): the highest of its usual clock (``own``: 30 days' readings
    that weren't low, their 90th percentile), the most a host alike gives now
    (``peers``: alike(), not low) and its CPU's top clock."""
    if r.get("mhz"):
        found = []
        mine = _p90([x.get("mhz") for x in own])
        if mine:
            found.append((mine, "its usual clock"))
        like = [p["mhz"] for p in peers if p.get("mhz")]
        if like:
            # the most one of them gives: braked ones among them (arachne's node01-03) don't lower it
            found.append((max(like), f"{len(like)} host{'s' if len(like) > 1 else ''} with its CPU"))
        if r.get("max_mhz"):
            found.append((float(r["max_mhz"]), "its CPU's top clock"))
        m = _INTEL_RATED.search(r.get("model") or "")
        if m:
            found.append((float(m.group(1)) * 1000, "its CPU's rated clock"))
        if found:
            ref, basis = max(found, key=lambda x: x[0])
            return ref, basis, "mhz"
        return None, None, "mhz"
    if r.get("sha_mbps"):
        found = []
        mine = _p90([x.get("sha_mbps") for x in own])
        if mine:
            found.append((mine, "its usual rate"))
        like = [p["sha_mbps"] for p in peers if p.get("sha_mbps")]
        if like:
            found.append((max(like), f"{len(like)} host{'s' if len(like) > 1 else ''} with its CPU"))
        if found:
            ref, basis = max(found, key=lambda x: x[0])
            return ref, basis, "sha_mbps"
    return None, None, "sha_mbps"


def judge(r: dict, own: list[dict], peers: list[dict], rule: dict) -> dict:
    """{low, reference, basis, measure, ratio, problem}: a reading against
    what this host should give. A low clock counts only when the sha256 rate
    agrees, where it has something to be compared with (a real brake lowers
    both: node02 11% and 6%; a count another tool disturbed lowers only one)."""
    ref, basis, measure = reference(r, own, peers)
    value = r.get(measure)
    out = {"low": False, "reference": round(ref) if ref else None, "basis": basis, "measure": measure,
           "ratio": None, "problem": None}
    if not ref or not value:
        return out
    ratio = value / ref
    out["ratio"] = round(ratio, 4)
    if ratio >= rule["min_fraction" if measure == "mhz" else "sha_min_fraction"]:
        return out
    if measure == "mhz" and r.get("sha_mbps"):
        sha_ref, _b, _m = reference(dict(r, mhz=None), own, peers)
        if sha_ref and r["sha_mbps"] / sha_ref >= rule["min_fraction"]:
            return out                      # its sha256 rate as usual: not a brake
    out["low"] = True
    slower = max(1, round(1 / ratio)) if ratio > 0 else None
    if measure == "mhz":
        what = f"its CPU runs at {value} MHz busy, {ratio:.0%} of {round(ref)} MHz ({basis})"
    else:
        what = (f"its CPU does {value:g} MB/s on nomad's sha256 test, {ratio:.0%} of {ref:g} ({basis}; "
                "its cycle counter can't be read here)")
    what += f": jobs there run about {slower}x slower" if slower else ""
    if r.get("os_mhz") and value and measure == "mhz" and r["os_mhz"] > 2 * value:
        what += f" (the OS says {r['os_mhz']} MHz: the clock it asks for, not the one the CPU gets)"
    if (r.get("governor") or "") == "powersave" and (r.get("driver") or "") == "acpi-cpufreq":
        what += " (its cpufreq governor is powersave: the OS holds it at its lowest clock)"
    out["problem"] = what
    return out


def power_of(conn: sqlite3.Connection, node: str, site: str | None = None) -> tuple[list[str], str | None]:
    """(its supply problems now, why its board isn't read) from the power
    collector's tables, where it runs on the same site."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "power_hosts" not in tables:
        return [], None
    by = "" if site is None else " AND source_site = ?"
    args = (node,) if site is None else (node, site)
    row = conn.execute(f"SELECT error FROM power_hosts WHERE node = ?{by}", args).fetchone()
    unread = None
    if row and row[0]:
        err = str(row[0])
        unread = ("its board can't be read in-band (no /dev/ipmi0)"
                  if "/dev/ipmi" in err or "could not open device" in err.lower()
                  else f"its board wasn't read ({err[:80]})")
    problems = []
    if "power_sensors" in tables:
        problems = [p for (p,) in conn.execute(
            f"SELECT problem FROM power_sensors WHERE node = ?{by} AND subject = 'power supply' "
            "AND severity IS NOT NULL ORDER BY sensor", args) if p]
    # what the power collector judges of the host as a whole ("one supply carries the node")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(power_hosts)")}
    if "host_problems" in cols:
        got = conn.execute(f"SELECT host_problems FROM power_hosts WHERE node = ?{by}", args).fetchone()
        try:
            import json
            problems += [x["problem"] for x in json.loads((got or [None])[0] or "[]") if isinstance(x, dict)
                         and x.get("subject") == "power supply" and x.get("severity") and x.get("problem")]
        except (ValueError, TypeError, KeyError):
            pass
    return problems, unread


ADVICE = ("Drain it until it is fixed (scontrol update nodename={node} state=drain reason=\"CPU throttled\"). "
          "A board short of power brakes its CPUs (PROCHOT): check both supplies' input (cord, outlet, PDU); "
          "once restored, if the brake stays on, reset the board controller (ipmitool mc reset cold) or "
          "power-cycle the node.")


def alert_item(node: str, host: dict, supplies: list[str], unread: str | None, where: str = "",
               every_minutes: float = EVERY_MINUTES) -> dict:
    severity = "critical" if supplies else "warning"
    message = f"{node}{where}: {host['problem']}, since {host['low_since']}."
    if supplies:
        message += f" Its power: {'; '.join(supplies)}."
    elif unread:
        message += f" Its supplies can't be seen from here: {unread}; check them out of band."
    message += " " + ADVICE.format(node=node)
    return {"host": node, "subject": "cpu clock", "severity": severity, "message": message,
            "details": {"node": node, "mhz": host.get("mhz"), "sha_mbps": host.get("sha_mbps"),
                        "reference": host.get("reference"), "basis": host.get("basis"),
                        "ratio": host.get("ratio"), "since": host.get("low_since"),
                        "supplies": supplies or None,
                        # readings every_minutes apart are one episode, not news each time
                        "episode_gap_minutes": max(60, int(every_minutes * 3))}}


class ClockCollector(TempsCollector):
    """How fast each CPU runs busy (perf cycles), here and over ssh."""

    name = "clock"
    description = "How fast each CPU really runs when busy (its cycle counter), here and over ssh"
    default_interval = EVERY_MINUTES * 60

    def __init__(self, config: dict[str, Any], db_path):
        super().__init__(config, db_path)
        every = self.config.get("every_minutes", EVERY_MINUTES)
        every = float(every) if isinstance(every, (int, float)) and not isinstance(every, bool) else EVERY_MINUTES
        self.every_minutes = max(1.0, min(1440.0, every))
        # a run sooner than this after the last one reads nothing (cron every 5 minutes: every third)
        self.min_seconds = float(self.config.get("min_seconds", self.every_minutes * 60 - 90))
        secs = self.config.get("seconds", SECONDS)
        self.seconds = max(0.1, min(5.0, float(secs))) if isinstance(secs, (int, float)) else SECONDS
        self.max_seconds = float(self.config.get("max_seconds") or 60)
        self.timeout = int(self.config.get("timeout") or 20)
        self.keep_days = int(self.config.get("keep_days") or KEEP_DAYS)
        alerts = self.config.get("alert_rules")
        if alerts is None:
            alerts = ((getattr(registry, "_config", {}) or {}).get("alerts") or {}).get("clock")
        self.alert_rules = alerts if isinstance(alerts, dict) else {}

    def _read(self, host: str) -> str:
        script = remote_command(self.seconds)
        if self._local(host):
            argv = ["sh", "-c", script]
        else:
            argv = ["ssh", "-n", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={min(self.timeout, 10)}"]
            if self.ssh_user:
                argv += ["-l", str(self.ssh_user)]
            argv += ["--", host, f"echo {MARK}; exec sh -c {shlex.quote(script)}"]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout,
                               stdin=subprocess.DEVNULL, errors="replace")
        except subprocess.TimeoutExpired:
            raise CollectionError(f"{host}: no answer in {self.timeout} s") from None
        except OSError as e:
            raise CollectionError(f"{host}: {e}") from None
        out = r.stdout or ""
        if not self._local(host):
            _head, mark, rest = out.partition(MARK + "\n")
            if not mark:
                raise CollectionError(f"{host}: ssh said {(r.stderr or '').strip()[-200:] or f'exit {r.returncode}'}")
            out = rest
        return out

    def _recent(self) -> bool:
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            try:
                row = conn.execute("SELECT MAX(timestamp) FROM cpu_clock").fetchone()
            finally:
                conn.close()
            last = datetime.fromisoformat(row[0]) if row and row[0] else None
        except (sqlite3.Error, ValueError):
            return False
        return bool(last and 0 <= (datetime.now() - last).total_seconds() < self.min_seconds)

    def collect(self) -> list[dict[str, Any]]:
        if not self.hosts and not self.slurm_nodes:
            self.note = "no hosts listed ([collectors.clock] hosts, workstations, slurm_nodes)"
            return []
        if self.min_seconds > 0 and self._recent():
            self.note = f"read less than {self.min_seconds / 60:g} min ago"
            return []
        when = datetime.now().isoformat(timespec="seconds")
        targets = self._targets()
        import random
        random.shuffle(targets)                 # any host not reached in max_seconds: not always the same
        rows, failed = [], []
        from concurrent.futures import ThreadPoolExecutor, wait
        pool = ThreadPoolExecutor(max_workers=min(self.parallel, max(1, len(targets))))
        try:
            futures = {pool.submit(self._read, host): (name, host) for name, host in targets}
            done, late = wait(futures, timeout=self.max_seconds)
            for f in done:
                name, _host = futures[f]
                try:
                    rows.append(parse(name, f.result(), when))
                except CollectionError as e:
                    failed.append(str(e))
            for f in late:
                f.cancel()
            if late:
                failed.append(f"{len(late)} host(s) not read within {self.max_seconds:g} s")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        notes = []
        if failed:
            notes.append(f"{len(failed)} of {len(targets)} not read: " + "; ".join(sorted(failed)))
        counterless = sorted(r["node"] for r in rows if not r.get("mhz"))
        if counterless:
            notes.append(f"no cycle counter on {', '.join(counterless[:8])}"
                         + (f" and {len(counterless) - 8} more" if len(counterless) > 8 else "")
                         + " (judged by the sha256 rate)")
        self.note = "; ".join(notes)[:400] or None
        if not rows and failed:
            raise CollectionError(self.note)
        return rows

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS cpu_clock (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, node TEXT NOT NULL,
            model TEXT, cpus INTEGER, load1 REAL, mhz INTEGER, sha_mbps REAL, max_mhz INTEGER,
            os_mhz INTEGER, driver TEXT, governor TEXT, perf TEXT, low INTEGER, vm INTEGER, sha_ni INTEGER,
            pkgs INTEGER, mhz_pkgs TEXT);
        CREATE INDEX IF NOT EXISTS idx_cpu_clock_node ON cpu_clock(node, timestamp);
        CREATE INDEX IF NOT EXISTS idx_cpu_clock_time ON cpu_clock(timestamp);
        CREATE TABLE IF NOT EXISTS cpu_clock_hosts (
            node TEXT PRIMARY KEY, read_at TEXT, model TEXT, mhz INTEGER, sha_mbps REAL, os_mhz INTEGER,
            governor TEXT, reference REAL, basis TEXT, measure TEXT, ratio REAL, low_since TEXT,
            throttled INTEGER, severity TEXT, problem TEXT, every_minutes REAL);
    """

    def store(self, data: list[dict[str, Any]]) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        fresh = []
        try:
            conn.executescript(self.SCHEMA)
            since = (datetime.now() - timedelta(days=HISTORY_DAYS)).isoformat(timespec="seconds")
            recent = (datetime.now() - timedelta(hours=PEER_HOURS)).isoformat(timespec="seconds")
            # hosts with the same CPU: this read's, and others' latest within PEER_HOURS
            latest = {n: {"model": m, "mhz": z, "sha_mbps": s, "vm": v, "sha_ni": k} for n, m, z, s, v, k in
                      conn.execute("SELECT h.node, h.model, h.mhz, h.sha_mbps, c.vm, c.sha_ni FROM cpu_clock_hosts h "
                                   "LEFT JOIN cpu_clock c ON c.id = (SELECT MAX(id) FROM cpu_clock WHERE node = h.node) "
                                   "WHERE h.read_at >= ? AND h.low_since IS NULL", (recent,))}
            latest.update({d["node"]: d for d in data})
            for d in data:
                rule = rules(self.alert_rules, d["node"])
                own = [{"mhz": z, "sha_mbps": s} for z, s in conn.execute(
                    "SELECT mhz, sha_mbps FROM cpu_clock WHERE node = ? AND timestamp >= ? AND COALESCE(low, 0) = 0",
                    (d["node"], since))]
                peers = [v for n, v in latest.items() if n != d["node"] and alike(d, v)]
                j = judge(d, own, peers, rule)
                d["low"] = 1 if j["low"] else 0
                conn.execute(
                    "INSERT INTO cpu_clock (timestamp, node, model, cpus, load1, mhz, sha_mbps, max_mhz, os_mhz, "
                    "driver, governor, perf, low, vm, sha_ni, pkgs, mhz_pkgs) VALUES (:timestamp, :node, :model, "
                    ":cpus, :load1, :mhz, :sha_mbps, :max_mhz, :os_mhz, :driver, :governor, :perf, :low, :vm, "
                    ":sha_ni, :pkgs, :mhz_pkgs)", d)
                # throttled: this and the readings before it low, `sustain` in a row
                last = [low for (low,) in conn.execute(
                    "SELECT low FROM cpu_clock WHERE node = ? ORDER BY id DESC LIMIT ?",
                    (d["node"], rule["sustain"]))]
                throttled = len(last) >= rule["sustain"] and all(last)
                prev = conn.execute("SELECT low_since, throttled FROM cpu_clock_hosts WHERE node = ?",
                                    (d["node"],)).fetchone()
                low_since = (prev[0] if prev and prev[0] else d["timestamp"]) if j["low"] else None
                host = {"node": d["node"], "read_at": d["timestamp"], "model": d.get("model"), "mhz": d.get("mhz"),
                        "sha_mbps": d.get("sha_mbps"), "os_mhz": d.get("os_mhz"), "governor": d.get("governor"),
                        "reference": j["reference"], "basis": j["basis"], "measure": j["measure"],
                        "ratio": j["ratio"], "low_since": low_since, "throttled": 1 if throttled else 0,
                        "severity": None, "problem": j["problem"], "every_minutes": self.every_minutes}
                if throttled and rule["enabled"]:
                    supplies, _unread = power_of(conn, d["node"])
                    host["severity"] = "critical" if supplies else "warning"
                conn.execute(
                    "INSERT OR REPLACE INTO cpu_clock_hosts (node, read_at, model, mhz, sha_mbps, os_mhz, governor, "
                    "reference, basis, measure, ratio, low_since, throttled, severity, problem, every_minutes) VALUES "
                    "(:node, :read_at, :model, :mhz, :sha_mbps, :os_mhz, :governor, :reference, :basis, :measure, "
                    ":ratio, :low_since, :throttled, :severity, :problem, :every_minutes)", host)
                if host["severity"]:
                    supplies, unread = power_of(conn, d["node"])
                    fresh.append(alert_item(d["node"], host, supplies, unread, every_minutes=self.every_minutes))
            cutoff = (datetime.now() - timedelta(days=self.keep_days)).isoformat(timespec="seconds")
            conn.execute("DELETE FROM cpu_clock WHERE timestamp < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()
        self._send(fresh)

    def _send(self, items: list[dict]) -> list[dict]:
        for it in items:
            try:
                from nomad.alerts import send_alert
                send_alert(severity=it["severity"], source="clock", host=it["host"], subject=it["subject"],
                           message=it["message"], details=it["details"])
            except Exception as e:                     # noqa: BLE001 -- never stops collection
                logger.warning(f"clock: alert not sent: {e}")
        return items


def hosts_now(conn: sqlite3.Connection, node: str | None = None, site: str | None = None) -> list[dict]:
    """Each host's last reading and judgment (cpu_clock_hosts), with its site on the hub."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cpu_clock_hosts)")}
    if not cols:
        return []
    names = ["node", "read_at", "model", "mhz", "sha_mbps", "os_mhz", "governor", "reference", "basis", "measure",
             "ratio", "low_since", "throttled", "severity", "problem"]
    names += [c for c in ("every_minutes", "source_site") if c in cols]
    where, args = [], []
    if node:
        where.append("node = ?")
        args.append(node)
    if site and "source_site" in cols:
        where.append("source_site = ?")
        args.append(site)
    rows = conn.execute(f"SELECT {', '.join(names)} FROM cpu_clock_hosts"
                        + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY node", args).fetchall()
    return [dict(zip(names, r)) for r in rows]


def fresh(h: dict, now: datetime | None = None, hours: float = PEER_HOURS) -> bool:
    """Whether a host's last reading still stands: read within ``hours``, or
    2.5 of its own intervals where it is read less often."""
    try:
        at = datetime.fromisoformat(str(h.get("read_at")))
    except (TypeError, ValueError):
        return False
    every = h.get("every_minutes") or EVERY_MINUTES
    return (now or datetime.now()) - at <= timedelta(hours=max(hours, 2.5 * float(every) / 60))


def throttled_nodes(conn: sqlite3.Connection, site: str | None = None, hours: float = PEER_HOURS,
                    alerts_clock: dict | None = None) -> dict:
    """{node: its row} for the hosts throttled now (fresh()), as alerted: not
    one the site's [alerts.clock] leaves out (no severity stored) nor one
    ``alerts_clock`` (the asker's own) does."""
    try:
        return {h["node"]: h for h in hosts_now(conn, site=site) if h.get("throttled") and h.get("severity")
                and fresh(h, hours=hours) and rules(alerts_clock, h["node"])["enabled"]}
    except sqlite3.Error:
        return {}


__all__ = ["ClockCollector", "PROBE", "remote_command", "parse", "rules", "alike", "reference", "judge", "power_of",
           "alert_item", "hosts_now", "fresh", "throttled_nodes"]
