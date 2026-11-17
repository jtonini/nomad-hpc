# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What nomad publishes for Prometheus (1.52.0): ``nomad metrics``.

A small set of numbers, read from the hub's combined database (or one site's
own), in Prometheus's text format, for Prometheus to collect and Grafana to
draw beside whatever else a site collects:

- each site: when the hub last brought its data up to date, and when each
  of its collectors last ran and last succeeded;
- each node: its condition (up, draining, drained, down), its cores and the
  cores allocated, its CPU clock against what it should give (throttling)
  and the problems its board reports (power supplies, voltages, fans);
- each GPU: how busy it is (DCGM's measure only), its memory, temperature
  and power;
- each partition: its pending and running jobs, and the median wait of the
  jobs that started in it lately;
- each filesystem: its size, use and free space.

What it never publishes: anything about one person (no user, job or lab
label). Whoever opens a Grafana dashboard sees all of it, and a label per
person or job makes thousands of series; the Console stays the place where
each lab sees only its own.

Honest gaps: a reading counts until its next one was due (1.5 of its usual
interval, as its own readings show it) plus ``grace_minutes`` (the hub's
pull, 30 by default). After that it is left out, not repeated, so a
collector that stopped shows as a gap in Grafana instead of a flat line that
looks fine; ``nomad_readings_too_old`` counts what was left out, and the
collectors' last runs say why. A node missing from its site's latest reading
(Slurm no longer lists it) is left out too.

    [metrics]
    # enabled = false               # `nomad metrics serve` answers only when true
    # listen = "127.0.0.1:10062"    # another address needs token_file
    # token_file = "/home/nomad/.config/nomad/metrics_token"   # Prometheus sends it (bearer)
    # grace_minutes = 30            # default: [hub] sync_interval_minutes + 10
    # wait_hours = 24               # the median wait: jobs that started in this many hours
    # cache_seconds = 30            # asks within this get the same page
    # site = "mycluster"            # a site's own database: its name (default: the host's)
"""
from __future__ import annotations

import ipaddress
import socket
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from nomad.facts._common import columns, num, parse_time, quantile

DEFAULT_PORT = 10062
DEFAULT_LISTEN = f"127.0.0.1:{DEFAULT_PORT}"
LOOK_HOURS = 48           # readings older than this are not looked at (none would count)
SAME_RUN_SECONDS = 120    # readings this close to a site's newest are one collector run
USUAL_GAPS = 20           # the usual interval: from this many latest gaps...
USUAL_FACTOR = 1.5        # ...a reading counts this many of them (plus grace) after it
DEFAULT_USUAL_MINUTES = 60
COLLECTOR_DAYS = 7        # collectors that ran in this many days
WAIT_SUBMIT_DAYS = 60     # jobs that started lately were submitted within this (an index's range)
CONDITIONS = ("up", "draining", "drained", "down")
POWER_PARTS = (("power supply", "power_supply"), ("voltage", "voltage"), ("fans", "fans"))
POWER_FILE_MINUTES = 20   # the power collector's max_age_minutes: how old the helper's file may be
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# name: (help, labels). The page's order, and the catalog `nomad metrics --catalog` prints.
CATALOG = {
    "nomad_build_info": ("The nomad that answers: 1, with its version.", ("version",)),
    "nomad_site_last_pull_timestamp_seconds": (
        "When the hub last brought the site's data up to date (Unix time).", ("site",)),
    "nomad_collector_last_run_timestamp_seconds": (
        f"When the collector last ran at the site (Unix time; collectors that ran in the last {COLLECTOR_DAYS} "
        "days).", ("site", "collector")),
    "nomad_collector_last_success_timestamp_seconds": (
        "When the collector last ran without an error at the site (Unix time).", ("site", "collector")),
    "nomad_readings_too_old": (
        "Readings left out because their next one was due and didn't come (a gap, not old figures), by "
        "where they come from.", ("site", "source")),
    "nomad_node_condition": (
        "1 for the node's condition now (up, draining, drained or down), 0 for the others.",
        ("site", "node", "condition")),
    "nomad_node_cpus": ("Cores Slurm lists on the node.", ("site", "node")),
    "nomad_node_cpus_allocated": ("Cores allocated to jobs on the node.", ("site", "node")),
    "nomad_node_cpu_clock_ratio": (
        "The CPU's clock when busy, as a share of what it should give (1: as it should; far below: braked).",
        ("site", "node")),
    "nomad_node_cpu_throttled": (
        "1 when the CPU's clock has read low on reading after reading (throttled), else 0.", ("site", "node")),
    "nomad_node_power_watched": (
        "1 when the node's board was read; 0 when it can't be read from the system (no /dev/ipmi0, or "
        "ipmitool failing there): its power supplies aren't seen.", ("site", "node")),
    "nomad_node_power_problems": (
        "Problems the node's board reports now, by part (power_supply, voltage, fans).",
        ("site", "node", "part")),
    "nomad_gpu_busy_ratio": (
        "Share of the time the GPU was busy, as DCGM measures it (nvidia-smi's figure is not used).",
        ("site", "node", "gpu")),
    "nomad_gpu_memory_used_bytes": ("GPU memory in use.", ("site", "node", "gpu")),
    "nomad_gpu_memory_total_bytes": ("The GPU's memory.", ("site", "node", "gpu")),
    "nomad_gpu_temperature_celsius": ("The GPU's temperature.", ("site", "node", "gpu")),
    "nomad_gpu_power_watts": ("The GPU's power draw.", ("site", "node", "gpu")),
    "nomad_partition_jobs": ("Jobs in the partition now, by state (pending, running).",
                             ("site", "partition", "state")),
    "nomad_partition_wait_median_seconds": (
        "The median wait of the jobs that started in the partition in the last wait_hours ([metrics], 24), "
        "from when each could start (its eligible time).",
        ("site", "partition")),
    "nomad_partition_jobs_started": (
        "Jobs that started in the partition in the last wait_hours ([metrics], 24).", ("site", "partition")),
    "nomad_filesystem_size_bytes": ("The filesystem's size.", ("site", "path")),
    "nomad_filesystem_used_bytes": ("Space used on the filesystem.", ("site", "path")),
    "nomad_filesystem_avail_bytes": ("Space free on the filesystem.", ("site", "path")),
}


@dataclass
class Settings:
    enabled: bool = False
    listen: str = DEFAULT_LISTEN
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    token_file: Path | None = None
    grace_minutes: float = 30.0
    wait_hours: float = 24.0
    cache_seconds: float = 30.0
    site: str | None = None
    problems: list = field(default_factory=list)


def split_listen(text: str) -> tuple[str, int]:
    """("127.0.0.1", 10062) from "127.0.0.1:10062", "[::1]:10062" or ":10062"
    (every address); ValueError when it isn't one."""
    raw = str(text).strip()
    if raw.startswith("["):
        host, sep, port = raw[1:].partition("]:")
        if not sep:
            raise ValueError(raw)
    else:
        host, sep, port = raw.rpartition(":")
        if not sep or ":" in host:
            raise ValueError(raw)
    p = int(port)
    if not 1 <= p <= 65535:
        raise ValueError(raw)
    return host or "0.0.0.0", p


def loopback(host: str) -> bool:
    """Whether only this host can reach the address."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def settings(config: dict | None) -> Settings:
    """[metrics] in nomad.toml, each value checked: one that isn't what it
    should be keeps its default, and says so in ``problems``."""
    config = config or {}
    raw = config.get("metrics")
    raw = raw if isinstance(raw, dict) else {}
    s = Settings()
    try:
        from nomad.sync_timer import interval
        every = interval(config)
    except Exception:
        every = None
    if every:
        s.grace_minutes = float(every + 10)
    known = {"enabled", "listen", "token_file", "grace_minutes", "wait_hours", "cache_seconds", "site"}
    for key in sorted(set(raw) - known):
        s.problems.append(f"[metrics] {key}: not a setting (ignored)")
    if "enabled" in raw:
        if isinstance(raw["enabled"], bool):
            s.enabled = raw["enabled"]
        else:
            s.problems.append("[metrics] enabled: true or false (serving stays off)")
    if "listen" in raw:
        try:
            s.host, s.port = split_listen(raw["listen"])
            s.listen = str(raw["listen"]).strip()
        except (TypeError, ValueError):
            s.problems.append(f"[metrics] listen = {raw['listen']!r}: an address and port, like "
                              f"\"{DEFAULT_LISTEN}\" (kept {DEFAULT_LISTEN})")
    if raw.get("token_file"):
        s.token_file = Path(str(raw["token_file"])).expanduser()
    for key, lo, hi in (("grace_minutes", 0, 240), ("wait_hours", 1, 168), ("cache_seconds", 0, 600)):
        if key not in raw:
            continue
        v = raw[key]
        if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
            setattr(s, key, float(v))
        else:
            s.problems.append(f"[metrics] {key} = {v!r}: a number from {lo} to {hi} (kept {getattr(s, key):g})")
    if raw.get("site"):
        s.site = str(raw["site"]).strip()
    return s


@dataclass
class Page:
    """What a scrape gets: samples by name, and what was left out."""
    generated_at: datetime
    samples: dict = field(default_factory=dict)        # name -> [(labels dict, value)]
    too_old: dict = field(default_factory=dict)        # (site, source) -> readings left out
    sites: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def add(self, name: str, labels: dict, value) -> None:
        if value is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        if v != v or v in (float("inf"), float("-inf")):      # NaN, infinite: not a reading
            return
        self.samples.setdefault(name, []).append((labels, v))

    def left_out(self, site: str, source: str, n: int = 1) -> None:
        key = (site, source)
        self.too_old[key] = self.too_old.get(key, 0) + n

    def looked(self, site: str, source: str) -> None:
        self.too_old.setdefault((site, source), 0)


def _bound(t: datetime, like) -> str:
    """``t`` written as the stored times are (a space or a T between the
    day and the time), so a string comparison means what it says."""
    sep = " " if isinstance(like, str) and len(like) > 10 and like[10] == " " else "T"
    return t.isoformat(sep=sep, timespec="seconds")


def _unix(t: datetime) -> float:
    return t.timestamp()


class _Reader:
    """One read of the database into a Page."""

    def __init__(self, conn: sqlite3.Connection, now: datetime, s: Settings):
        self.conn, self.now, self.s = conn, now, s
        self.tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.page = Page(generated_at=now)
        self.own = s.site or socket.gethostname().split(".")[0]
        self.hub = "sync_sites" in self.tables
        self.sites = self._sites()
        self.page.sites = [self.label(x) for x in self.sites]
        self.current: set = set()      # sites whose node readings are fresh

    # -- sites, filters, freshness -----------------------------------------------------------

    def _sites(self) -> list:
        if self.hub:
            try:
                names = [r[0] for r in self.conn.execute("SELECT name FROM sync_sites ORDER BY name") if r[0]]
            except sqlite3.Error:
                names = []
            if names:
                return names
            if "node_state" in self.tables and "source_site" in columns(self.conn, "node_state"):
                return [r[0] for r in self.conn.execute(
                    "SELECT DISTINCT source_site FROM node_state WHERE source_site IS NOT NULL ORDER BY 1")]
        return [None]

    def label(self, site) -> str:
        return self.own if site is None else str(site)

    def where(self, cols: set, site) -> tuple[str, list]:
        """``source_site = ? AND `` for a site on the hub (its index leads with it)."""
        if site is None or "source_site" not in cols:
            return "", []
        return "source_site = ? AND ", [site]

    def rows(self, sql: str, args=()) -> list[dict]:
        cur = self.conn.execute(sql, tuple(args))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, tuple(r))) for r in cur.fetchall()]

    def newest(self, table: str, tcol: str, w: str, a: list):
        raw = self.conn.execute(f"SELECT MAX({tcol}) FROM {table} WHERE {w}{tcol} IS NOT NULL", a).fetchone()[0]
        return raw, parse_time(raw)

    def usual_minutes(self, table: str, tcol: str, w: str, a: list, newest: datetime, like) -> float | None:
        """The usual interval between the site's readings in ``table``: the
        90th percentile of its latest gaps (a minute or more apart)."""
        since = _bound(newest - timedelta(hours=24), like)
        times = sorted({t for (raw,) in self.conn.execute(
            f"SELECT DISTINCT substr({tcol}, 1, 16) FROM {table} WHERE {w}{tcol} >= ? "
            f"ORDER BY 1 DESC LIMIT {USUAL_GAPS + 1}", a + [since]) if (t := parse_time(raw)) is not None})
        gaps = [(b - x).total_seconds() / 60 for x, b in zip(times, times[1:])]
        gaps = [g for g in gaps if g >= 1]
        return quantile(gaps, 0.9) if len(gaps) >= 2 else None

    def log_usual(self, site, names: tuple) -> float | None:
        """A collector's usual interval at the site, from collection_log."""
        if "collection_log" not in self.tables:
            return None
        cols = columns(self.conn, "collection_log")
        if not {"collector", "started_at"} <= cols:
            return None
        w, a = self.where(cols, site)
        raw, _t = self.newest("collection_log", "started_at", w, a)
        since = _bound(self.now - timedelta(days=2), raw)
        marks = ",".join("?" for _ in names)
        times = sorted({t for (raw,) in self.conn.execute(
            f"SELECT started_at FROM collection_log WHERE {w}started_at >= ? AND collector IN ({marks}) "
            f"ORDER BY started_at DESC LIMIT {USUAL_GAPS + 1}", a + [since] + list(names))
            if (t := parse_time(raw)) is not None})
        gaps = [g for g in ((b - x).total_seconds() / 60 for x, b in zip(times, times[1:])) if g >= 1]
        return quantile(gaps, 0.9) if len(gaps) >= 2 else None

    def limit(self, usual: float | None) -> float:
        """Minutes a reading counts after it was taken."""
        return USUAL_FACTOR * (usual or DEFAULT_USUAL_MINUTES) + self.s.grace_minutes

    def age(self, t: datetime | None) -> float | None:
        return None if t is None else max(0.0, (self.now - t).total_seconds() / 60)

    # -- what is read -------------------------------------------------------------------------

    def read(self) -> Page:
        from nomad import __version__
        self.page.add("nomad_build_info", {"version": str(__version__)}, 1)
        for part in (self.pulls, self.collectors, self.nodes, self.clock, self.power, self.gpus,
                     self.partitions, self.waits, self.filesystems):
            try:
                part()
            except sqlite3.Error as e:
                self.page.notes.append(f"{part.__name__}: {e}")
        for (site, source), n in sorted(self.page.too_old.items()):
            self.page.add("nomad_readings_too_old", {"site": site, "source": source}, n)
        return self.page

    def pulls(self) -> None:
        if not self.hub or not {"name", "last_success"} <= columns(self.conn, "sync_sites"):
            return
        for r in self.rows("SELECT name, last_success FROM sync_sites ORDER BY name"):
            t = parse_time(r["last_success"])
            if r["name"] and t is not None:
                self.page.add("nomad_site_last_pull_timestamp_seconds", {"site": str(r["name"])}, _unix(t))

    def collectors(self) -> None:
        if "collection_log" not in self.tables:
            return
        cols = columns(self.conn, "collection_log")
        if not {"collector", "started_at"} <= cols:
            return
        ok = "MAX(CASE WHEN success THEN started_at END)" if "success" in cols else "NULL"
        for site in self.sites:
            w, a = self.where(cols, site)
            raw, _t = self.newest("collection_log", "started_at", w, a)
            since = _bound(self.now - timedelta(days=COLLECTOR_DAYS), raw)
            for r in self.rows(f"SELECT collector, MAX(started_at) AS ran, {ok} AS good FROM collection_log "
                               f"WHERE {w}started_at >= ? GROUP BY collector ORDER BY collector", a + [since]):
                if not r["collector"]:
                    continue
                labels = {"site": self.label(site), "collector": str(r["collector"])}
                for name, value in (("nomad_collector_last_run_timestamp_seconds", r["ran"]),
                                    ("nomad_collector_last_success_timestamp_seconds", r["good"])):
                    t = parse_time(value)
                    if t is not None:
                        self.page.add(name, labels, _unix(t))

    def _latest_run(self, table: str, key: str, want: tuple, source: str, site):
        """The site's latest run of ``table`` (rows within SAME_RUN_SECONDS of
        its newest), each ``key``'s latest row there; [] when that run is too
        old (its rows counted as left out)."""
        cols = columns(self.conn, table)
        if not {key, "timestamp"} <= cols:
            return None
        w, a = self.where(cols, site)
        raw, newest = self.newest(table, "timestamp", w, a)
        if newest is None:
            return None
        label = self.label(site)
        self.page.looked(label, source)
        run = _bound(newest - timedelta(seconds=SAME_RUN_SECONDS), raw)
        have = [c for c in want if c in cols]
        rows = self.rows(f"SELECT {key} AS k, MAX(timestamp) AS t{''.join(', ' + c for c in have)} FROM {table} "
                         f"WHERE {w}timestamp >= ? GROUP BY {key}", a + [run])
        age = self.age(newest)
        if age is None or age > self.limit(self.usual_minutes(table, "timestamp", w, a, newest, raw)):
            self.page.left_out(label, source, len(rows))
            return []
        return rows

    def nodes(self) -> None:
        if "node_state" not in self.tables:
            return
        from nomad.facts.node import _condition
        for site in self.sites:
            rows = self._latest_run("node_state", "node_name", ("state", "cpus_total", "cpus_alloc"),
                                    "node_state", site)
            if rows is None:
                continue
            label = self.label(site)
            if rows:
                self.current.add(site)
            for r in rows:
                if not r["k"]:
                    continue
                labels = {"site": label, "node": str(r["k"])}
                alloc = num(r.get("cpus_alloc"))
                cond = _condition(r.get("state"), alloc)
                for c in CONDITIONS:
                    self.page.add("nomad_node_condition", {**labels, "condition": c}, 1 if c == cond else 0)
                self.page.add("nomad_node_cpus", labels, num(r.get("cpus_total")))
                self.page.add("nomad_node_cpus_allocated", labels, alloc)

    def clock(self) -> None:
        """cpu_clock_hosts: each host's latest judged reading; it counts for
        1.5 of its own every_minutes plus grace."""
        if "cpu_clock_hosts" not in self.tables:
            return
        cols = columns(self.conn, "cpu_clock_hosts")
        if not {"node", "read_at"} <= cols:
            return
        want = [c for c in ("ratio", "throttled", "every_minutes") if c in cols]
        for site in self.sites:
            w, a = self.where(cols, site)
            label = self.label(site)
            rows = self.rows(f"SELECT node, read_at{''.join(', ' + c for c in want)} FROM cpu_clock_hosts "
                             f"WHERE {w}read_at IS NOT NULL ORDER BY node", a)
            if not rows:
                continue
            self.page.looked(label, "cpu_clock")
            for r in rows:
                age = self.age(parse_time(r["read_at"]))
                if age is None or age > self.limit(num(r.get("every_minutes")) or 15):
                    self.page.left_out(label, "cpu_clock")
                    continue
                labels = {"site": label, "node": str(r["node"])}
                self.page.add("nomad_node_cpu_clock_ratio", labels, num(r.get("ratio")))
                if r.get("throttled") is not None:
                    self.page.add("nomad_node_cpu_throttled", labels, 1 if num(r["throttled"]) else 0)

    def power(self) -> None:
        """power_hosts (each host's last read) and power_sensors (each sensor
        now): for each host read in time, whether its board could be read
        (watched) and, when it could, its problems by part. The root helper
        writes its file on its own cron and the collector reads it on its
        own, so a reading may be POWER_FILE_MINUTES old when stored (the
        collector's own max_age_minutes): it counts that much longer."""
        if "power_hosts" not in self.tables:
            return
        import json
        hcols = columns(self.conn, "power_hosts")
        if not {"node", "read_at"} <= hcols:
            return
        scols = columns(self.conn, "power_sensors") if "power_sensors" in self.tables else set()
        for site in self.sites:
            w, a = self.where(hcols, site)
            label = self.label(site)
            want = [c for c in ("error", "host_problems") if c in hcols]
            hosts = self.rows(f"SELECT node, read_at{''.join(', ' + c for c in want)} FROM power_hosts "
                              f"WHERE {w}read_at IS NOT NULL ORDER BY node", a)
            if not hosts:
                continue
            self.page.looked(label, "power")
            lim = self.limit(self.log_usual(site, ("power",))) + POWER_FILE_MINUTES
            counts: dict = {}
            if {"node", "subject", "problem"} <= scols:
                sw, sa = self.where(scols, site)
                for r in self.rows(f"SELECT node, subject, COUNT(*) AS n FROM power_sensors "
                                   f"WHERE {sw}problem IS NOT NULL GROUP BY node, subject", sa):
                    counts[(r["node"], r["subject"])] = int(r["n"] or 0)
            for h in hosts:
                age = self.age(parse_time(h["read_at"]))
                if age is None or age > lim:
                    self.page.left_out(label, "power")
                    continue
                node = {"site": label, "node": str(h["node"])}
                # its board can't be read (no /dev/ipmi0, ipmitool failing): not watched, which is not "fine"
                self.page.add("nomad_node_power_watched", node, 0 if h.get("error") else 1)
                if h.get("error"):
                    continue
                loose: dict = {}
                try:
                    for p in json.loads(h.get("host_problems") or "[]") or []:
                        if isinstance(p, dict):
                            loose[p.get("subject")] = loose.get(p.get("subject"), 0) + 1
                except (TypeError, ValueError):
                    pass
                for subject, part in POWER_PARTS:
                    self.page.add("nomad_node_power_problems", {**node, "part": part},
                                  counts.get((h["node"], subject), 0) + loose.get(subject, 0))

    def gpus(self) -> None:
        """gpu_stats: each GPU's latest reading, if in time; busy only from
        DCGM's readings."""
        if "gpu_stats" not in self.tables:
            return
        cols = columns(self.conn, "gpu_stats")
        if not {"timestamp", "node_name"} <= cols:
            return
        gi = "gpu_index" if "gpu_index" in cols else "0"
        for site in self.sites:
            w, a = self.where(cols, site)
            raw, newest = self.newest("gpu_stats", "timestamp", w, a)
            if newest is None:
                continue
            label = self.label(site)
            self.page.looked(label, "gpu_stats")
            lim = self.limit(self.usual_minutes("gpu_stats", "timestamp", w, a, newest, raw))
            since = _bound(self.now - timedelta(hours=LOOK_HOURS), raw)
            want = [c for c in ("memory_used_mb", "memory_total_mb", "temperature_c", "power_draw_w") if c in cols]
            for r in self.rows(f"SELECT node_name, {gi} AS g, MAX(timestamp) AS t"
                               f"{''.join(', ' + c for c in want)} FROM gpu_stats WHERE {w}timestamp >= ? "
                               f"GROUP BY node_name, {gi}", a + [since]):
                age = self.age(parse_time(r["t"]))
                if age is None or age > lim:
                    self.page.left_out(label, "gpu_stats")
                    continue
                labels = self._gpu_labels(label, r)
                used, total = num(r.get("memory_used_mb")), num(r.get("memory_total_mb"))
                self.page.add("nomad_gpu_memory_used_bytes", labels, None if used is None else used * 2 ** 20)
                self.page.add("nomad_gpu_memory_total_bytes", labels, None if total is None else total * 2 ** 20)
                self.page.add("nomad_gpu_temperature_celsius", labels, num(r.get("temperature_c")))
                self.page.add("nomad_gpu_power_watts", labels, num(r.get("power_draw_w")))
            if not {"data_source", "real_util_pct"} <= cols:
                continue
            for r in self.rows(f"SELECT node_name, {gi} AS g, MAX(timestamp) AS t, real_util_pct FROM gpu_stats "
                               f"WHERE {w}timestamp >= ? AND data_source = 'dcgm' AND real_util_pct IS NOT NULL "
                               f"GROUP BY node_name, {gi}", a + [since]):
                age = self.age(parse_time(r["t"]))
                if age is None or age > lim:
                    continue                 # counted above, with the GPU's other readings
                v = num(r.get("real_util_pct"))
                self.page.add("nomad_gpu_busy_ratio", self._gpu_labels(label, r),
                              None if v is None else max(0.0, min(100.0, v)) / 100)

    def _gpu_labels(self, label: str, r: dict) -> dict:
        g = num(r.get("g"))
        return {"site": label, "node": str(r.get("node_name") or label),
                "gpu": str(int(g)) if g is not None else "0"}

    def partitions(self) -> None:
        if "queue_state" not in self.tables:
            return
        for site in self.sites:
            rows = self._latest_run("queue_state", "partition", ("pending_jobs", "running_jobs"),
                                    "queue_state", site)
            for r in rows or []:
                if not r["k"]:
                    continue
                labels = {"site": self.label(site), "partition": str(r["k"])}
                self.page.add("nomad_partition_jobs", {**labels, "state": "pending"}, num(r.get("pending_jobs")))
                self.page.add("nomad_partition_jobs", {**labels, "state": "running"}, num(r.get("running_jobs")))

    def waits(self) -> None:
        """The jobs that started in the last wait_hours, by partition: how
        many, and their median wait. Only for a site whose node readings are
        in time (its jobs are as current as they are)."""
        if "jobs" not in self.tables:
            return
        cols = columns(self.conn, "jobs")
        if not {"partition", "start_time", "submit_time"} <= cols:
            return
        wait = "wait_time_seconds" if "wait_time_seconds" in cols else "NULL"
        elig = "eligible_time" if "eligible_time" in cols else "NULL"
        since = self.now - timedelta(hours=self.s.wait_hours)
        for site in self.sites:
            if "node_state" in self.tables and site not in self.current:
                continue
            w, a = self.where(cols, site)
            # how its times are written (the hub's index on submit_time finds the newest at once)
            got = self.conn.execute(f"SELECT submit_time FROM jobs WHERE {w}submit_time IS NOT NULL "
                                    "ORDER BY submit_time DESC LIMIT 1", a).fetchone()
            sample = got[0] if got else None
            if sample is None:
                continue
            by: dict = {}
            for r in self.rows(
                    f"SELECT partition, submit_time, start_time, {elig} AS eligible, {wait} AS wait FROM jobs WHERE {w}"
                    "submit_time >= ? AND start_time >= ? AND start_time <= ?",
                    a + [_bound(since - timedelta(days=WAIT_SUBMIT_DAYS), sample), _bound(since, sample),
                         _bound(self.now, sample)]):
                if not r["partition"]:
                    continue
                # from when it could start (its eligible time, as the condo figures count it): a dependency
                # or a start time asked for is not waiting; else Slurm's own wait, else from its submission
                s0, s1, e = parse_time(r["submit_time"]), parse_time(r["start_time"]), parse_time(r.get("eligible"))
                ref = e if e is not None and (s0 is None or e >= s0) else None
                v = (s1 - ref).total_seconds() if ref is not None and s1 is not None and s1 >= ref else None
                if v is None:
                    v = num(r.get("wait"))
                if v is None or v < 0:
                    v = (s1 - s0).total_seconds() if s0 and s1 and s1 >= s0 else None
                if v is not None:
                    by.setdefault(str(r["partition"]), []).append(v)
            label = self.label(site)
            for part, values in sorted(by.items()):
                labels = {"site": label, "partition": part}
                self.page.add("nomad_partition_jobs_started", labels, len(values))
                self.page.add("nomad_partition_wait_median_seconds", labels, statistics.median(values))

    def filesystems(self) -> None:
        if "filesystems" not in self.tables:
            return
        for site in self.sites:
            rows = self._latest_run("filesystems", "path", ("total_bytes", "used_bytes", "available_bytes"),
                                    "filesystems", site)
            for r in rows or []:
                if not r["k"]:
                    continue
                labels = {"site": self.label(site), "path": str(r["k"])}
                self.page.add("nomad_filesystem_size_bytes", labels, num(r.get("total_bytes")))
                self.page.add("nomad_filesystem_used_bytes", labels, num(r.get("used_bytes")))
                self.page.add("nomad_filesystem_avail_bytes", labels, num(r.get("available_bytes")))


def build(conn: sqlite3.Connection, now: datetime | None = None, config: dict | None = None,
          s: Settings | None = None) -> Page:
    """The page from ``conn`` (read-only is enough)."""
    return _Reader(conn, now or datetime.now(), s or settings(config)).read()


# -- the text format --------------------------------------------------------------------------

def _escape_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _escape_help(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("\n", "\\n")


def _value(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(round(v, 6))


def render(page: Page) -> str:
    """Prometheus's text format (0.0.4): each name's HELP and TYPE, then its
    samples; a name with no samples is left out."""
    out = []
    for name, (help_text, labels) in CATALOG.items():
        samples = page.samples.get(name)
        if not samples:
            continue
        out.append(f"# HELP {name} {_escape_help(help_text)}")
        out.append(f"# TYPE {name} gauge")
        for lab, v in samples:
            inner = ",".join(f'{k}="{_escape_label(lab[k])}"' for k in labels if k in lab)
            out.append(f"{name}{{{inner}}} {_value(v)}" if inner else f"{name} {_value(v)}")
    return "\n".join(out) + "\n"


def read_token(s: Settings) -> str | None:
    """The token Prometheus must send, from ``token_file``; None without
    one. ValueError when the file can't be used: missing, readable by
    others, or too short to be a secret."""
    if s.token_file is None:
        return None
    p = s.token_file
    try:
        mode = p.stat().st_mode
        text = p.read_text().strip()
    except OSError as e:
        raise ValueError(f"token_file {p}: {e.strerror or e}") from None
    if mode & 0o077:
        raise ValueError(f"token_file {p} can be read by others: chmod 600 {p}")
    if len(text) < 16 or any(ch.isspace() for ch in text):
        raise ValueError(f"token_file {p}: one line of at least 16 characters, no spaces "
                         "(`nomad metrics token --apply` writes one)")
    return text


def fingerprint(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def make_server(page_fn, host: str, port: int, token: str | None = None, cache_seconds: float = 30.0,
                log=None):
    """An HTTP server answering GET /metrics with ``page_fn()`` (the text),
    built again at most every ``cache_seconds``; with ``token``, only to an
    ``Authorization: Bearer`` that matches. ``serve_forever()`` runs it."""
    import hmac
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    lock = threading.Lock()
    cached: dict = {"at": 0.0, "text": None}
    want = None if token is None else f"Bearer {token}".encode()

    def page() -> str:
        with lock:
            t = time.monotonic()
            if cached["text"] is None or t - cached["at"] >= cache_seconds:
                cached["text"] = page_fn()
                cached["at"] = t
            return cached["text"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "nomad-metrics"
        timeout = 15

        def version_string(self):
            return self.server_version

        def log_message(self, fmt, *args):       # a line per scrape is noise in the journal
            pass

        def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(200, b"nomad metrics: GET /metrics\n")
            if path != "/metrics":
                return self._send(404, b"not found\n")
            if want is not None:
                got = (self.headers.get("Authorization") or "").strip().encode()
                if not hmac.compare_digest(got, want):
                    return self._send(401, b"nomad metrics: a bearer token is needed\n",
                                      extra={"WWW-Authenticate": 'Bearer realm="nomad metrics"'})
            try:
                text = page()
            except Exception as e:                    # the database busy, moved: said, and asked again next time
                if log:
                    log(f"nomad metrics: the page could not be built: {e}")
                return self._send(500, b"nomad metrics: the page could not be built (the service's log says why)\n")
            return self._send(200, text.encode(), CONTENT_TYPE)

        do_HEAD = do_GET

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        daemon_threads = True

    return Server((host, port), Handler)


def summary(page: Page) -> list[str]:
    """What the page holds, counts only (no node, partition or path names):
    each name's samples and sites, and what was left out as too old."""
    lines = []
    total = sum(len(v) for v in page.samples.values())
    lines.append(f"{total:,} samples under {len(page.samples)} names, for {len(page.sites)} "
                 f"site{'' if len(page.sites) == 1 else 's'} ({', '.join(page.sites) or 'none'}).")
    for name in CATALOG:
        samples = page.samples.get(name)
        if not samples:
            continue
        sites = {lab.get("site") for lab, _ in samples if lab.get("site")}
        lines.append(f"  {name}: {len(samples):,}" + (f" ({len(sites)} site{'' if len(sites) == 1 else 's'})"
                                                     if sites else ""))
    old = [(site, source, n) for (site, source), n in sorted(page.too_old.items()) if n]
    if old:
        lines.append("Left out as too old (their next reading was due and didn't come):")
        for site, source, n in old:
            lines.append(f"  {site}, {source}: {n:,}")
    else:
        lines.append("Nothing left out as too old.")
    for note in page.notes:
        lines.append(f"note: {note}")
    return lines
