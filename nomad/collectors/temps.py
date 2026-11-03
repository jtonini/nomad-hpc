# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Hardware temperatures (1.25.6): what the kernel's sensors read (hwmon),
on this host and on hosts reached over ssh.

A workstation whose CPU cooler's fan died (peterslabworkstation, Nov 2026)
runs on, passively cooled, until the CPU throttles at its limit; nothing
else nomad reads says so, and its fan speed can't be read (the board's
Super I/O chip has no driver in EL9). Its temperature can.

At each run, every host is asked for each temperature its sensors give
(/sys/class/hwmon: k10temp's Tctl, Tccd1..., coretemp's Package id N and
cores, nvme, amdgpu, ...), in one ssh call (BatchMode, a login with no
shell setup of its own needed: a POSIX sh line), ``parallel`` (8) at once,
for at most ``max_seconds`` (50) in all: the hosts in ``hosts``, this
collecting host's workstations (``workstations = true``: the workstation
collector's list) and, on a Slurm head node, every node Slurm lists that
isn't down, off or not responding (``slurm_nodes = true``). Each reading is
kept ``keep_days`` (30); each hour's lowest, mean and highest for good (day
and night, for as long as the history goes).

Alerts, on the CPU package's own sensor (k10temp Tctl, zenpower Tctl or
Tdie, coretemp Package id N): at or above ``warning`` (90 °C) or
``critical`` (95 °C) for ``sustain_minutes`` (3) -- every reading for that
long, so a moment's spike is not an alert -- through this host's [alerts]
(source ``temps``, the host as host): once, again when worse, then every
reminder. A CPU limit below 95 °C (a Ryzen X3D's 89, say) goes in a host's
own table.

    [collectors.temps]
    enabled = true
    hosts = ["peterslabworkstation"]   # over ssh; "localhost" (or this host's name): read here
    # workstations = true              # and every workstation the workstation collector reads
    # slurm_nodes = true               # and every node Slurm lists that answers (a head node)
    # parallel = 8
    # max_seconds = 50
    # ssh_user = "..."                 # default: as ssh logs in (its config)
    # timeout = 15                     # seconds a host has to answer
    # keep_days = 30                   # each reading; each hour's for good
    # min_seconds = 45                 # a run sooner than this after the last one reads nothing

    [alerts.temps]
    # warning = 90
    # critical = 95
    # sustain_minutes = 3
    [alerts.temps.peterslabworkstation]
    warning = 85
    critical = 89                      # its Ryzen 9 7900X3D's limit (Tjmax)

Run it every minute for a short sustain to mean minutes (its own timer:
``nomad collect -C temps --once``); with the rest of the collectors every 5
minutes, "3 minutes" is two readings in a row.

Tables: ``hw_temps`` (timestamp, node, chip, sensor, label, temp_c,
crit_c), ``hw_temps_hourly`` (hour, node, chip, sensor, label, min_c, avg_c,
max_c, readings).
"""
from __future__ import annotations

import logging
import re
import shlex
import socket
import sqlite3
import subprocess
from datetime import datetime, timedelta
from typing import Any

from .base import BaseCollector, CollectionError, registry

logger = logging.getLogger(__name__)

HWMON = "/sys/class/hwmon"
MARK = "__NOMAD_TEMPS__"
DEFAULTS = {"enabled": True, "warning": 90.0, "critical": 95.0, "sustain_minutes": 3.0}
KEEP_DAYS = 30
MIN_SECONDS = 45
GAP_MINUTES = 15          # readings further apart than this: not one stretch
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}")


def remote_command(root: str = HWMON) -> str:
    """One line of POSIX sh (any login shell runs it: no newlines, no !):
    a line ``T|chip|device|sensor|label|millidegrees|crit`` per sensor."""
    r = shlex.quote(root)
    # an older kernel's driver keeps its name and readings in device/ (spiderweb's hwmon2 has no name)
    return (f'for d in {r}/hwmon*; do n=$(cat "$d/name" 2>/dev/null || cat "$d/device/name" 2>/dev/null); '
            '[ -n "$n" ] || continue; v=$(readlink -f "$d/device" 2>/dev/null); v=${v##*/}; '
            'for t in "$d"/temp*_input "$d"/device/temp*_input; do [ -r "$t" ] || continue; b=${t%_input}; '
            'x=$(cat "$t" 2>/dev/null) || continue; l=$(cat "${b}_label" 2>/dev/null); '
            'c=$(cat "${b}_crit" 2>/dev/null); echo "T|$n|$v|${b##*/}|$l|$x|$c"; done; done')


def _celsius(text: str) -> float | None:
    try:
        v = int(str(text).strip()) / 1000.0
    except (TypeError, ValueError):
        return None
    # a sensor with nothing on it says 0, -128, 127 or 255 (a crit of 0 too): not a reading
    return round(v, 1) if -40.0 < v < 127.0 and v != 0 else None


def parse(node: str, output: str, when: str) -> list[dict[str, Any]]:
    """The readings in ``output`` (remote_command's lines). A chip name the
    host has twice (two NVMe drives) takes its device after an @."""
    rows, seen = [], set()
    for line in output.splitlines():
        bits = line.strip().split("|")
        if len(bits) != 7 or bits[0] != "T" or not bits[1]:
            continue
        _t, chip, device, sensor, label, milli, crit = bits
        if (chip, device, sensor) in seen:          # the same reading in hwmonN/ and its device/
            continue
        seen.add((chip, device, sensor))
        temp = _celsius(milli)
        if temp is None:
            continue
        rows.append({"timestamp": when, "node": node, "chip": chip.strip()[:64], "device": device.strip()[:64],
                     "sensor": sensor.strip()[:32], "label": label.strip()[:64] or None, "temp_c": temp,
                     "crit_c": _celsius(crit)})
    devices: dict = {}
    for r in rows:
        devices.setdefault(r["chip"], set()).add(r["device"])
    for r in rows:
        if len(devices[r["chip"]]) > 1:
            r["chip"] = f"{r['chip']}@{r['device'] or '?'}"
    return rows


def cpu_sensor(rows: list[dict]) -> list[dict]:
    """The CPU package's own readings among a host's (one per socket):
    k10temp Tctl (an old one's unlabelled temp1), zenpower Tctl or Tdie,
    coretemp Package id N (Physical id N)."""
    out = []
    by_chip: dict = {}
    for r in rows:
        by_chip.setdefault(r["chip"], []).append(r)
    for chip, rs in by_chip.items():
        base = chip.split("@")[0]
        labels = {(r["label"] or ""): r for r in rs}
        if base == "k10temp":
            pick = labels.get("Tctl") or labels.get("Tdie") or next((r for r in rs if r["sensor"] == "temp1"), None)
            out += [pick] if pick else []
        elif base == "zenpower":
            pick = labels.get("Tctl") or labels.get("Tdie")
            out += [pick] if pick else []
        elif base == "coretemp":
            out += [r for r in rs if (r["label"] or "").startswith(("Package id", "Physical id"))]
    return out


def rules(alerts_temps: dict | None, node: str) -> dict:
    """[alerts.temps] over the defaults, then the host's own table (its
    name, or its short name); a value of the wrong kind keeps the one
    before."""
    raw = alerts_temps if isinstance(alerts_temps, dict) else {}
    out = dict(DEFAULTS)
    short = node.split(".")[0]
    layers = [raw] + [raw[k] for k in (node, short) if isinstance(raw.get(k), dict)][:1]
    for layer in layers:
        if layer.get("enabled") is False:
            out["enabled"] = False
        elif layer.get("enabled") is True:
            out["enabled"] = True
        for key, low, high in (("warning", 0, 150), ("critical", 0, 150), ("sustain_minutes", 0, 120)):
            v = layer.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and low <= v <= high:
                out[key] = float(v)
    return out


def sustained(points: list[tuple[datetime, float]], threshold: float, minutes: float,
              now: datetime) -> tuple[bool, datetime | None, float | None, float | None]:
    """Whether the newest readings (``points``, newest first) are all at or
    above ``threshold`` for ``minutes``: (yes, since when, lowest, highest).
    The newest must be current (GAP_MINUTES); readings further apart than
    that end the stretch."""
    if not points or (now - points[0][0]) > timedelta(minutes=GAP_MINUTES) or points[0][1] < threshold:
        return False, None, None, None
    start, lo, hi, prev = points[0][0], points[0][1], points[0][1], points[0][0]
    for t, v in points[1:]:
        if v < threshold or (prev - t) > timedelta(minutes=GAP_MINUTES):
            break
        start, lo, hi, prev = t, min(lo, v), max(hi, v), t
    span = (points[0][0] - start).total_seconds()
    return span >= max(0.0, minutes * 60 - 30), start, lo, hi


class TempsCollector(BaseCollector):
    """Hardware temperatures (hwmon), here and over ssh."""

    name = "temps"
    description = "Hardware temperatures (hwmon: CPU, drives, ...), here and over ssh"
    default_interval = 60

    def __init__(self, config: dict[str, Any], db_path):
        super().__init__(config, db_path)
        hosts = self.config.get("hosts") or []
        listed = [str(h) for h in (hosts if isinstance(hosts, list) else [hosts])]
        if self.config.get("workstations") is True:
            listed += [str(h) for h in (self.config.get("workstation_hosts") or [])]
        self.hosts = list(dict.fromkeys(h for h in listed if _HOST.fullmatch(h)))
        self.slurm_nodes = self.config.get("slurm_nodes") is True
        self.parallel = max(1, min(32, int(self.config.get("parallel") or 8)))
        self.max_seconds = float(self.config.get("max_seconds") or 50)
        self.ssh_user = self.config.get("ssh_user") or None
        self.timeout = int(self.config.get("timeout") or 15)
        self.keep_days = int(self.config.get("keep_days") or KEEP_DAYS)
        self.min_seconds = float(self.config.get("min_seconds", MIN_SECONDS))
        self.root = str(self.config.get("hwmon_root") or HWMON)     # tests
        # no retry within a run: the next minute's run is the retry (a host that's off would take
        # three ssh timeouts, longer than a minute)
        self._max_retries = int(self.config.get("max_retries", 1))
        alerts = self.config.get("alert_rules")
        if alerts is None:                         # built outside the plan: the whole config, if set
            alerts = ((getattr(registry, "_config", {}) or {}).get("alerts") or {}).get("temps")
        self.alert_rules = alerts if isinstance(alerts, dict) else {}

    # ── reading ────────────────────────────────────────────────────────────
    def _local(self, host: str) -> bool:
        names = {"localhost", "127.0.0.1", socket.gethostname(), socket.gethostname().split(".")[0]}
        try:
            names.add(socket.getfqdn())
        except OSError:
            pass
        return host in names

    def _read(self, host: str) -> str:
        script = remote_command(self.root)
        if self._local(host):
            argv = ["sh", "-c", script]
        else:
            argv = ["ssh", "-n", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={min(self.timeout, 10)}"]
            if self.ssh_user:
                argv += ["-l", str(self.ssh_user)]
            argv += ["--", host, f"echo {MARK}; exec sh -c {shlex.quote(script)}"]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise CollectionError(f"{host}: no answer in {self.timeout} s") from None
        except OSError as e:
            raise CollectionError(f"{host}: {e}") from None
        out = r.stdout or ""
        if not self._local(host):
            head, mark, rest = out.partition(MARK + "\n")
            if not mark:
                raise CollectionError(f"{host}: ssh said {(r.stderr or '').strip()[-200:] or f'exit {r.returncode}'}")
            out = rest
        return out

    def _recent(self) -> bool:
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            try:
                row = conn.execute("SELECT MAX(timestamp) FROM hw_temps").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        try:
            last = datetime.fromisoformat(row[0]) if row and row[0] else None
        except ValueError:
            return False
        return bool(last and (datetime.now() - last).total_seconds() < self.min_seconds)

    def _targets(self) -> list[tuple[str, str]]:
        """(name kept, host asked): the hosts listed (this host's own name
        for one read here), then Slurm's nodes that answer."""
        out = []
        for h in self.hosts:
            out.append((socket.gethostname().split(".")[0] if self._local(h) else h, h))
        if self.slurm_nodes:
            from nomad.collectors.base import find_tool, tool_env
            from nomad.collectors.node_disks import nodes_from_sinfo
            sinfo = find_tool("sinfo")
            if not sinfo:
                raise CollectionError("slurm_nodes = true, but there's no sinfo here")
            r = subprocess.run([sinfo, "-a", "-N", "-h", "-o", "%N %n %t"], capture_output=True, text=True,
                               errors="replace", timeout=30, env=tool_env())
            if r.returncode != 0:
                raise CollectionError(f"sinfo: {r.stderr.strip()[:200]}")
            here = {name for name, _h in out}
            out += [(n, h) for n, h in nodes_from_sinfo(r.stdout) if n not in here]
        seen: set = set()
        return [t for t in out if not (t[0] in seen or seen.add(t[0]))]

    def collect(self) -> list[dict[str, Any]]:
        if not self.hosts and not self.slurm_nodes:
            self.note = "no hosts listed ([collectors.temps] hosts, workstations, slurm_nodes)"
            return []
        if self.min_seconds > 0 and self._recent():
            self.note = f"read less than {self.min_seconds:g} s ago"
            return []
        when = datetime.now().isoformat(timespec="seconds")
        targets = self._targets()
        rows, failed, bare = [], [], []
        from concurrent.futures import ThreadPoolExecutor, wait
        pool = ThreadPoolExecutor(max_workers=min(self.parallel, max(1, len(targets))))
        try:
            futures = {pool.submit(self._read, host): (name, host) for name, host in targets}
            done, late = wait(futures, timeout=self.max_seconds)
            for f in done:
                name, host = futures[f]
                try:
                    got = parse(name, f.result(), when)
                except CollectionError as e:
                    failed.append(str(e))
                    continue
                if not got:
                    bare.append(name)                   # a VM, say: nothing to read, not a failure
                rows += got
            for f in late:
                f.cancel()
            if late:
                failed.append(f"{len(late)} host(s) not read within {self.max_seconds:g} s")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        notes = []
        if failed:
            notes.append(f"{len(failed)} of {len(targets)} not read: " + "; ".join(sorted(failed)))
        if bare:
            notes.append(f"no temperature sensors on {', '.join(sorted(bare)[:10])}"
                         + (f" and {len(bare) - 10} more" if len(bare) > 10 else ""))
        self.note = "; ".join(notes)[:400] or None
        if not rows and failed:
            raise CollectionError(self.note)
        return rows

    # ── keeping ────────────────────────────────────────────────────────────
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS hw_temps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, node TEXT NOT NULL, chip TEXT NOT NULL, sensor TEXT NOT NULL,
            label TEXT, temp_c REAL, crit_c REAL);
        CREATE INDEX IF NOT EXISTS idx_hw_temps_node ON hw_temps(node, chip, sensor, timestamp);
        CREATE INDEX IF NOT EXISTS idx_hw_temps_time ON hw_temps(timestamp);
        CREATE TABLE IF NOT EXISTS hw_temps_hourly (
            hour TEXT NOT NULL, node TEXT NOT NULL, chip TEXT NOT NULL, sensor TEXT NOT NULL, label TEXT,
            min_c REAL, avg_c REAL, max_c REAL, readings INTEGER,
            PRIMARY KEY (node, chip, sensor, hour));
    """

    def store(self, data: list[dict[str, Any]]) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.executescript(self.SCHEMA)
            conn.executemany(
                "INSERT INTO hw_temps (timestamp, node, chip, sensor, label, temp_c, crit_c) "
                "VALUES (:timestamp, :node, :chip, :sensor, :label, :temp_c, :crit_c)", data)
            hours = sorted({d["timestamp"][:13] for d in data})
            since = (datetime.fromisoformat(hours[0] + ":00:00") - timedelta(hours=1)).isoformat(timespec="seconds")
            # this hour and the one before, from the readings themselves (one run late is fine)
            conn.execute(
                "INSERT OR REPLACE INTO hw_temps_hourly (hour, node, chip, sensor, label, min_c, avg_c, max_c, "
                "readings) SELECT substr(timestamp, 1, 13) || ':00:00', node, chip, sensor, MAX(label), "
                "MIN(temp_c), ROUND(AVG(temp_c), 2), MAX(temp_c), COUNT(*) FROM hw_temps WHERE timestamp >= ? "
                "GROUP BY substr(timestamp, 1, 13), node, chip, sensor", (since,))
            cutoff = (datetime.now() - timedelta(days=self.keep_days)).isoformat(timespec="seconds")
            conn.execute("DELETE FROM hw_temps WHERE timestamp < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()
        self._alerts(data)

    # ── alerting ───────────────────────────────────────────────────────────
    def _alerts(self, data: list[dict], now: datetime | None = None) -> list[dict]:
        """The CPU readings checked against [alerts.temps]; what was raised."""
        now = now or datetime.now()
        by_node: dict = {}
        for d in data:
            by_node.setdefault(d["node"], []).append(d)
        raised = []
        for node, rows in by_node.items():
            rule = rules(self.alert_rules, node)
            if not rule["enabled"]:
                continue
            for cpu in cpu_sensor(rows):
                found = self.check(node, cpu, rule, rows, now)
                if found:
                    raised.append(found)
                    try:
                        from nomad.alerts import send_alert
                        send_alert(severity=found["severity"], source="temps", host=node,
                                   subject=f"CPU temperature ({cpu['chip']} {cpu['label'] or cpu['sensor']})",
                                   message=found["message"], details=found["details"])
                    except Exception as e:                     # noqa: BLE001 -- never stops collection
                        logger.warning(f"temps: alert not sent: {e}")
        return raised

    def check(self, node: str, cpu: dict, rule: dict, rows: list[dict], now: datetime) -> dict | None:
        return check_cpu(self.db_path, node, cpu, rule, rows, now)


LOOK_BACK_MINUTES = 360   # how far back "for how long" is counted


def _for_how_long(minutes: int, whole: bool) -> str:
    if whole:
        return f"for more than {LOOK_BACK_MINUTES // 60} hours"
    if minutes < 120:
        return f"for {minutes} minute{'s' if minutes != 1 else ''}"
    return f"for {minutes // 60} h {minutes % 60:02d} min"


def check_cpu(db_path, node: str, cpu: dict, rule: dict, rows: list[dict], now: datetime) -> dict | None:
    """The alert for one CPU sensor (``cpu``: one of ``rows``, the host's
    readings now), from its stored readings, or None."""
    window = max(rule["sustain_minutes"] + GAP_MINUTES, LOOK_BACK_MINUTES)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            pts = conn.execute(
                "SELECT timestamp, temp_c FROM hw_temps WHERE node = ? AND chip = ? AND sensor = ? "
                "AND timestamp >= ? ORDER BY timestamp DESC",
                (node, cpu["chip"], cpu["sensor"],
                 (now - timedelta(minutes=window)).isoformat(timespec="seconds"))).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    points = []
    for t, v in pts:
        try:
            points.append((datetime.fromisoformat(t), float(v)))
        except (TypeError, ValueError):
            continue
    for severity in ("critical", "warning"):
        limit = rule[severity]
        yes, since, lo, hi = sustained(points, limit, rule["sustain_minutes"], now)
        if not yes:
            continue
        span = (points[0][0] - since).total_seconds()
        mins = round(span / 60)
        whole = since == points[-1][0] and (now - since) >= timedelta(minutes=window - GAP_MINUTES)
        name = f"{cpu['chip']} {cpu['label'] or cpu['sensor']}"
        others = ", ".join(f"{r['label'] or r['sensor']} {r['temp_c']:g} °C" for r in rows
                           if r["chip"] == cpu["chip"] and r is not cpu)
        temps = f"{lo:g} °C" if lo == hi else f"{lo:g}-{hi:g} °C"
        msg = ((f"{node}'s CPU is at {temps} (" if span < 60 else
                f"{node}'s CPU has been at {temps} {_for_how_long(mins, whole)} (") + f"{name}; "
               f"warning at {rule['warning']:g} °C, critical at {rule['critical']:g} °C)"
               + (f"; its other sensors now: {others}" if others else "")
               + (". At its limit the CPU slows itself down to cool: its jobs run slower. "
                  "Check its cooling (fans, airflow, the room)." if severity == "critical" else
                  ". Its cooling is not keeping up: check its fans and airflow."))
        return {"severity": severity, "message": msg,
                "details": {"node": node, "sensor": name, "temp_c": points[0][1], "lowest_c": lo,
                            "highest_c": hi, "since": since.isoformat(timespec="seconds"),
                            "minutes": mins, "warning_c": rule["warning"], "critical_c": rule["critical"],
                            "sustain_minutes": rule["sustain_minutes"]}}
    return None


def latest(conn: sqlite3.Connection, node: str | None = None) -> list[tuple]:
    """Each sensor's last reading: (node, chip, sensor, label, temp_c, timestamp)."""
    where, args = ("WHERE node = ?", [node]) if node else ("", [])
    try:
        return conn.execute(
            f"SELECT h.node, h.chip, h.sensor, h.label, h.temp_c, h.timestamp FROM hw_temps h JOIN ("
            f"SELECT node, chip, sensor, MAX(timestamp) AS t FROM hw_temps {where} GROUP BY node, chip, sensor) m "
            "ON h.node = m.node AND h.chip = m.chip AND h.sensor = m.sensor AND h.timestamp = m.t "
            "ORDER BY h.node, h.chip, h.sensor", args).fetchall()
    except sqlite3.Error:
        return []


def hourly(conn: sqlite3.Connection, node: str, chip: str, sensor: str, since: datetime) -> list[tuple]:
    """(hour, min_c, avg_c, max_c, readings) since ``since``."""
    try:
        return conn.execute(
            "SELECT hour, min_c, avg_c, max_c, readings FROM hw_temps_hourly WHERE node = ? AND chip = ? "
            "AND sensor = ? AND hour >= ? ORDER BY hour",
            (node, chip, sensor, since.strftime("%Y-%m-%dT%H:00:00"))).fetchall()
    except sqlite3.Error:
        return []


__all__ = ["TempsCollector", "remote_command", "parse", "cpu_sensor", "rules", "sustained", "check_cpu", "latest",
           "hourly", "DEFAULTS"]
