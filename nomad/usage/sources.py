# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What the report reads, with every name taken out.

Jobs come from nomad's database (a site's own, or the hub's combined.db for
one site) or from an ``sacct -P`` export; node samples, job metrics, GPU
samples, filesystems and Slurm's monthly totals from the database when there
is one. On the way in:

- people become numbers (after the excluded accounts are counted and left out);
- job names and working directories become application families, and are dropped;
- partitions become classes (a tier, overlay, condo, other);
- a user map (user -> department, school) becomes labels on the jobs.

The names seen on the way are kept apart, in ``Data.names``, only for the
privacy guard that checks the finished report for them.
"""
from __future__ import annotations

import csv
import re
import sqlite3
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from nomad.hostlist import expand_hostlist
from nomad.usage import calcs
from nomad.usage.config import ALL_NODES, CONDO, OTHER, OVERLAY, ClusterConfig

# A finished job whose start disagrees with end - elapsed by more than this
# has a wrong start (nomad stored squeue's estimate before September 2026).
START_TOLERANCE_S = 120
MIXED = "mixed"
OUTSIDE = "outside the tier map"
# What the report sets aside (its own words, which the privacy check knows).
NEVER_STARTED = "never started"
START_CORRECTED = "start corrected"
NO_NODES = "no node list"
NO_END = "no end recorded (running, or outcome unknown)"
UNREADABLE = "unreadable export records"
REASONS = (OUTSIDE, MIXED, NEVER_STARTED, START_CORRECTED, NO_NODES, NO_END, UNREADABLE)
# A job left waiting with no end counts in a period only when submitted at
# most this long before it (a stale row must not count for ever).
WAITING_AT_MOST = timedelta(days=30)
_LABEL_TOKEN = re.compile(r"[A-Za-z0-9_\-@+]+")
_ALNUM = re.compile(r"[A-Za-z0-9]+")
# Words of the report that this module writes (the privacy check knows them).
DB_LABEL = "NØMAÐ database"
JOBS_DB = "NØMAÐ job records"
JOBS_EXPORT = "Slurm job records (sacct export"
INVENTORY_SAMPLES = "NØMAÐ node samples (each node as last seen in the period)"
SOURCE_WORDS = (DB_LABEL, JOBS_DB, JOBS_EXPORT, INVENTORY_SAMPLES, "report.toml and", ", site")
_UTC_SPACE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

_WAITING = ("PENDING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "RESV_DEL_HOLD", "SUSPENDED", "CONFIGURING")
# Powered down (power saving) is not a failure: such a node is there when needed.
_DOWN = ("DOWN", "NOT_RESPONDING", "FAIL", "NO_RESPOND", "UNKNOWN")
_POWER = ("POWERED_DOWN", "POWER_DOWN", "POWERING_DOWN", "POWERING_UP")
_DRAIN = ("DRAIN",)


class SourceError(ValueError):
    """The data asked for isn't there (no such site, no jobs table...)."""


@dataclass(slots=True)
class Job:
    user: int
    pclasses: tuple[str, ...]
    nodes: tuple[str, ...]
    tier: str | None          # one tier, MIXED, OUTSIDE, or None (no nodes)
    cpus: int
    elapsed: float            # seconds
    submit: datetime | None
    start: datetime | None
    end: datetime | None      # the real end, or start + elapsed for a job without one
    ended: bool
    state: str
    req_mem_mb: float | None
    req_time_s: int | None
    time_limit_known: bool
    gpus: int
    gpu_request: bool
    family: str
    family_how: str | None
    gpu_family: str
    gpu_family_how: str | None
    work_root: str | None
    cpu_pct: float | None = None
    peak_mem_gb: float | None = None
    dept: str | None = None
    school: str | None = None

    @property
    def started(self) -> bool:
        return self.start is not None


@dataclass
class NodeInfo:
    cores: int | None = None
    gpus: int = 0
    memory_mb: int | None = None


@dataclass
class Names:
    """Names seen in the data, for the privacy guard only."""
    users: set[str] = field(default_factory=set)
    groups: set[str] = field(default_factory=set)
    partitions: set[str] = field(default_factory=set)
    job_names: set[str] = field(default_factory=set)
    paths: set[str] = field(default_factory=set)
    others: set[str] = field(default_factory=set)

    def all(self) -> set[str]:
        return self.users | self.groups | self.partitions | self.job_names | self.paths | self.others


@dataclass
class Data:
    cfg: ClusterConfig
    t0: datetime
    t1: datetime
    source: str                         # where the jobs came from, in words
    db_label: str | None                # the database, in words, or None
    site: str | None
    jobs: list[Job] = field(default_factory=list)
    names: Names = field(default_factory=Names)
    nodes: dict[str, NodeInfo] = field(default_factory=dict)
    inventory_source: str = ""
    set_aside: Counter = field(default_factory=Counter)
    excluded_accounts: int = 0
    excluded_jobs: int = 0
    outside_map_core_hours: float = 0.0
    jobs_by_month: Counter = field(default_factory=Counter)       # every job record, by month submitted
    first_job: datetime | None = None
    # node_state: (node, month) -> [samples, sum alloc share, sum load share]
    node_months: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    node_state_span: tuple[datetime, datetime] | None = None
    # (node, month) -> Counter of state class -> samples; node -> hours per sample
    node_state_classes: dict[tuple[str, str], Counter] = field(default_factory=dict)
    node_sample_hours: dict[str, float] = field(default_factory=dict)
    outages: list[tuple[datetime, datetime]] = field(default_factory=list)
    # gpu_stats: month -> [samples, active samples, sum util]
    gpu_months: dict[str, list[float]] = field(default_factory=dict)
    gpu_stats_span: tuple[datetime, datetime] | None = None
    # filesystems: path -> [(month, max used, max total, last reading)]
    filesystems: dict[str, list[tuple[str, float, float, datetime]]] = field(default_factory=dict)
    usage_rows: list[dict] = field(default_factory=list)          # cluster_usage
    teaching: dict | None = None
    teaching_site: str | None = None
    job_summary_count: int = 0
    user_map_given: bool = False
    unmapped_people: int = 0
    node_memory_mb_max: float | None = None
    seen_nodes: set[str] = field(default_factory=set)
    fs_labels: dict[str, str] = field(default_factory=dict)   # filesystem -> label shown
    # filesystem -> (used, total, when) of its last reading before the period's end
    fs_latest: dict[str, tuple[float, float, datetime | None]] = field(default_factory=dict)
    # node -> (first sample, last sample) in the period
    node_spans: dict[str, tuple[datetime, datetime]] = field(default_factory=dict)
    people_names: list[str] | None = None     # only for `nomad usage-report people`
    # Jobs submitted in the period that started after it: they count in the
    # waits of the month they were submitted, nowhere else.
    late_jobs: list[Job] = field(default_factory=list)
    gpu_first_alloc: datetime | None = None   # the first job with GPUs in its allocation, any time
    gpus_from_request: int = 0                # jobs whose GPUs come from the request (no allocation recorded)
    usage_clusters: int = 0                   # Slurm clusters in the monthly totals, when added together
    pending_ranges: int = 0                   # pending array ranges in an export (one waiting job each)

    def safe_label(self, text) -> str:
        """A label from the data (a filesystem, a session type) with any
        username, group, account or partition in it replaced by *, whole
        ('/mnt/zlab' -> '/mnt/*') or joined to other words by - or _
        ('/home/alice_old' -> '/home/*_old')."""
        if text is None:
            return ""
        bad = {n.lower() for n in self.names.users | self.names.groups | self.names.partitions
               if len(n) >= 2 and not n.isdigit()}
        if not bad:
            return str(text)
        out = _LABEL_TOKEN.sub(lambda m: "*" if m.group(0).lower() in bad else m.group(0), str(text))
        return _ALNUM.sub(lambda m: "*" if m.group(0).lower() in bad else m.group(0), out)

    def labels(self, raw) -> dict[str, str]:
        """Display labels for raw labels from the data: safe_label, numbered
        where two become the same ('/mnt/* (1)', '/mnt/* (2)')."""
        shown = {r: self.safe_label(r) for r in sorted(raw)}
        count = Counter(shown.values())
        seen: Counter = Counter()
        for r in sorted(raw):
            v = shown[r]
            if count[v] > 1:
                seen[v] += 1
                shown[r] = f"{v} ({seen[v]})"
        return shown

    @property
    def hours(self) -> float:
        return calcs.period_hours(self.t0, self.t1)

    def cores(self, node: str) -> int | None:
        info = self.nodes.get(node)
        return info.cores if info else None

    def tier_nodes(self, tiers) -> list[str]:
        tiers = set(tiers)
        if not self.cfg.has_tier_map:
            return sorted(set(self.nodes) | self.seen_nodes) if ALL_NODES in tiers else []
        return sorted(n for t in tiers for n in self.cfg.tiers.get(t, []))

    def gpu_nodes(self) -> list[str]:
        return sorted(n for n, i in self.nodes.items() if i.gpus)

    def cards(self) -> int:
        return sum(i.gpus for i in self.nodes.values())


# --- small helpers -------------------------------------------------------------

def parse_time(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    text = str(value).strip()
    if not text or text in ("Unknown", "None", "N/A", "NONE"):
        return None
    try:
        return datetime.fromisoformat(text.replace(" ", "T")).replace(tzinfo=None, microsecond=0)
    except ValueError:
        return None


def _seconds(a: datetime, b: datetime) -> float:
    """Seconds from a to b, two local times: across a change to or from
    daylight saving time too."""
    try:
        return (b.astimezone() - a.astimezone()).total_seconds()
    except (OverflowError, OSError, ValueError):
        return (b - a).total_seconds()


def state_word(state) -> str:
    parts = str(state or "").split()
    return parts[0].rstrip("+").upper() if parts else ""


def node_state_class(state) -> str:
    s = str(state or "").upper()
    for k in _POWER:
        s = s.replace(k, "")
    if s.endswith("*") or any(k in s for k in _DOWN):
        return "down"
    if any(k in s for k in _DRAIN):
        return "drained"
    return "up"


def open_db(path: Path) -> sqlite3.Connection:
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.DatabaseError:
        return set()


def sites(conn) -> list[str]:
    """Sites in a hub's combined database ([] for a site's own database)."""
    if "source_site" not in _columns(conn, "jobs"):
        return []
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT source_site FROM jobs WHERE source_site IS NOT NULL ORDER BY 1")]


def cluster_name(conn) -> str | None:
    """The cluster a site's own database is about, from its node samples."""
    if "cluster" not in _columns(conn, "node_state"):
        return None
    names = [r[0] for r in conn.execute(
        "SELECT DISTINCT cluster FROM node_state WHERE cluster IS NOT NULL AND cluster NOT IN ('', 'default') "
        "LIMIT 2")]
    return names[0] if len(names) == 1 else None


def _site_filter(conn, table: str, site: str | None, alias: str = "") -> tuple[str, list]:
    if site and "source_site" in _columns(conn, table):
        return f" AND {alias}source_site = ?", [site]
    return "", []


def load_user_map(path: Path | str) -> tuple[dict[str, tuple[str | None, str | None]], set[str]]:
    """user -> (department, school) from a CSV with a header: user (or
    username, netid), department, school; other columns (a PI, say) are
    read only so the guard knows them."""
    out: dict[str, tuple[str | None, str | None]] = {}
    extra: set[str] = set()
    with open(Path(path).expanduser(), newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise SourceError(f"{path}: empty user map")
        fields = {h.lower().strip(): h for h in reader.fieldnames}
        ucol = next((fields[k] for k in ("user", "username", "netid", "login") if k in fields), None)
        if ucol is None:
            raise SourceError(f"{path}: the user map needs a 'user' column")
        dcol = fields.get("department") or fields.get("dept")
        scol = fields.get("school") or fields.get("college")
        for row in reader:
            u = (row.get(ucol) or "").strip()
            if not u:
                continue
            d = (row.get(dcol) or "").strip() or None if dcol else None
            s = (row.get(scol) or "").strip() or None if scol else None
            out[u] = (d, s)
            for k, v in row.items():
                if k not in (ucol, dcol, scol) and v and v.strip():
                    extra.add(v.strip())
    return out, extra


# --- partition classes -------------------------------------------------------------

def partition_classes_from_nodes(partition_nodes: dict[str, set[str]], cfg: ClusterConfig) -> dict[str, str]:
    """Class each partition by its nodes: one tier -> that tier; only condo
    nodes -> condo; several tiers -> overlay."""
    out = {}
    for p, nodes in partition_nodes.items():
        tiers = {cfg.tier_of(n) for n in nodes}
        tiers.discard(None)
        if not tiers:
            out[p] = OTHER
        elif len(tiers) == 1:
            out[p] = tiers.pop()
        else:
            out[p] = OVERLAY
    return out


class _Classes:
    def __init__(self, cfg: ClusterConfig, derived: dict[str, str]):
        self.cfg, self.derived = cfg, derived

    def __call__(self, partition: str | None) -> tuple[str, ...]:
        out = []
        for p in str(partition or "").split(","):
            p = p.strip()
            if not p:
                continue
            k = (self.cfg.partition_classes.get(p) or self.derived.get(p)
                 or self.cfg.unlisted_partitions)
            if not self.cfg.has_tier_map:
                k = ALL_NODES if k not in (OVERLAY, CONDO) else k
            if k not in out:
                out.append(k)
        return tuple(out)


# --- the job builder ------------------------------------------------------------------

class _Builder:
    """Turns job rows (dicts with nomad's jobs columns) into Jobs."""

    def __init__(self, data: Data, classes: _Classes, exclude: set[str],
                 user_map: dict | None, summaries: dict | None, from_sacct: bool = False):
        self.d, self.classes, self.exclude = data, classes, exclude
        self.from_sacct = from_sacct
        self.user_map, self.summaries = user_map, summaries or {}
        self.user_ids: dict[str, int] = {}
        self.excluded_seen: set[str] = set()
        self.unmapped: set[str] = set()

    def add(self, row: dict) -> None:
        d, cfg = self.d, self.d.cfg
        user = str(row.get("user_name") or "").strip()
        state = state_word(row.get("state"))
        submit = parse_time(row.get("submit_time"))
        start = parse_time(row.get("start_time"))
        end_text = row.get("end_time")
        assumed = False
        if not self.from_sacct and end_text and _UTC_SPACE.match(str(end_text)):
            # An outcome nomad assumed before 1.7.19 when the job left the
            # queue, timed by SQLite in UTC: when nomad noticed, not the end.
            from nomad.collectors.slurm import _utc_to_local
            end_text = _utc_to_local(end_text)
            assumed = True
        end = parse_time(end_text)
        if submit is not None:
            d.jobs_by_month[calcs.month_key(submit)] += 1
            if d.first_job is None or submit < d.first_job:
                d.first_job = submit
        for name, bucket in ((user, d.names.users), (row.get("group_name"), d.names.groups),
                             (row.get("account"), d.names.groups),
                             (row.get("job_name"), d.names.job_names)):
            if name and str(name).strip():
                bucket.add(str(name).strip())
        for p in str(row.get("partition") or "").split(","):
            if p.strip():
                d.names.partitions.add(p.strip())
        for part in str(row.get("work_tail") or "").split("/"):
            if part.strip():
                d.names.paths.add(part.strip())

        alloc = row.get("alloc_gpus")
        if alloc is None and row.get("alloc_tres"):
            from nomad.collectors.slurm import gpu_count
            alloc = gpu_count(row.get("alloc_tres"))
        req_gpus = int(row.get("req_gpus") or 0)
        gpus_from_request = alloc is None and not row.get("alloc_tres") and req_gpus > 0
        gpus = req_gpus if gpus_from_request else int(alloc or 0)
        if gpus and start is not None and not gpus_from_request:
            if d.gpu_first_alloc is None or start < d.gpu_first_alloc:
                d.gpu_first_alloc = start

        elapsed = row.get("runtime_seconds")
        try:
            elapsed = float(elapsed) if elapsed is not None else None
        except (TypeError, ValueError):
            elapsed = None
        known_end = end is not None and state != "UNKNOWN" and not assumed
        if start is not None and known_end:
            span = _seconds(start, end)
            if elapsed is None or elapsed < 0:
                elapsed = max(0.0, span)
            # A start Slurm can't have written: after the end, before the
            # submission, or off end - elapsed. Taken from end - elapsed.
            # (UNKNOWN jobs' ends are when nomad noticed them gone.)
            bad = (start > end or (submit is not None and start < submit)
                   or abs(span - elapsed) > START_TOLERANCE_S)
            if bad:
                start = end - timedelta(seconds=elapsed)
                d.set_aside[START_CORRECTED] += 1
        elapsed = max(0.0, elapsed or 0.0)
        ended = known_end
        if start is not None and not known_end:
            # Running, or UNKNOWN (its end is when nomad noticed it gone):
            # as long as it was seen to run.
            end = start + timedelta(seconds=elapsed)

        # In the period? A job that ran overlaps it; one that never ran was
        # waiting in it (submitted before its end, not ended before its start,
        # and not a row left waiting long before it). A job submitted in the
        # period that started after it counts in the waits only.
        late = False
        if start is not None:
            if not (start < d.t1 and end >= d.t0):
                if start >= d.t1 and submit is not None and d.t0 <= submit < d.t1:
                    late = True
                else:
                    return
        else:
            if submit is None or submit >= d.t1 or (ended and end < d.t0):
                return
            if not ended:
                earliest = d.t0 - WAITING_AT_MOST if state in _WAITING else d.t0
                if submit < earliest:
                    return

        if user in self.exclude:
            if not late:
                self.excluded_seen.add(user)
                d.excluded_jobs += 1
            return

        nodes = tuple(expand_hostlist(row.get("node_list"))) if start is not None else ()
        tiers = {cfg.tier_of(n) for n in nodes}
        if not nodes:
            tier = None
        elif None in tiers:
            tier = OUTSIDE
        elif len(tiers) == 1:
            tier = tiers.pop()
        else:
            tier = MIXED
        cpus = int(row.get("req_cpus") or 0)
        fam, how = calcs.classify(cfg.families, row.get("job_name"), row.get("work_tail"))
        gfam, ghow = calcs.classify(cfg.gpu_families, row.get("job_name"), row.get("work_tail"))
        rt = row.get("req_time_seconds")
        uid = self.user_ids.setdefault(user, len(self.user_ids)) if not late else -1
        job = Job(
            user=uid, pclasses=self.classes(row.get("partition")), nodes=nodes, tier=tier,
            cpus=cpus, elapsed=elapsed, submit=submit, start=start, end=end, ended=ended,
            state=state,
            req_mem_mb=float(row["req_mem_mb"]) if row.get("req_mem_mb") else None,
            req_time_s=int(rt) if rt not in (None, "") else None,
            # A time limit is known where the row was read from sacct's full
            # format (an export, or nomad 1.7.42 on): those carry the
            # allocation. Elsewhere "no limit" and "not recorded" look alike.
            time_limit_known=self.from_sacct or bool(row.get("alloc_tres")),
            gpus=gpus, gpu_request=gpus > 0 or req_gpus > 0,
            family=fam, family_how=how, gpu_family=gfam, gpu_family_how=ghow,
            work_root=row.get("work_root") or None,
        )
        if late:
            job.user = self.user_ids.setdefault(user, len(self.user_ids))
            d.late_jobs.append(job)
            return
        d.seen_nodes.update(nodes)
        if start is not None and not nodes:
            d.set_aside[NO_NODES] += 1
        if gpus_from_request:
            d.gpus_from_request += 1
        jid = str(row.get("job_id"))
        summ = self.summaries.get(jid)
        if self.from_sacct and submit is not None and (summ is None or summ[2] != submit):
            # The database's row of that number is another job: this one,
            # if nomad has it, is kept apart as NUMBER@SUBMIT.
            from nomad.db.jobkeys import aside_id
            summ = self.summaries.get(aside_id(jid, submit))
            summ = summ if summ is not None and summ[2] == submit else None
        if summ is not None:
            job.cpu_pct, job.peak_mem_gb = summ[0], summ[1]
        if self.user_map is not None:
            dept, school = self.user_map.get(user, (None, None))
            job.dept, job.school = dept, school
            if dept is None and school is None:
                self.unmapped.add(user)
        if tier == OUTSIDE:
            d.set_aside[OUTSIDE] += 1
            d.outside_map_core_hours += cpus * calcs.job_hours(job, d.t0, d.t1, True)
        if start is None:
            d.set_aside[NEVER_STARTED] += 1
        elif not ended:
            d.set_aside[NO_END] += 1
        d.jobs.append(job)

    def finish(self) -> None:
        self.d.excluded_accounts = len(self.excluded_seen)
        self.d.unmapped_people = len(self.unmapped)


# --- loading -------------------------------------------------------------------------

_JOB_COLS = ("job_id", "user_name", "group_name", "partition", "node_list", "job_name",
             "submit_time", "start_time", "end_time", "state", "req_cpus", "req_mem_mb",
             "req_gpus", "req_time_seconds", "runtime_seconds", "account", "alloc_tres",
             "alloc_gpus", "work_root", "work_tail")


def _summaries(conn, site, with_submit: bool = False) -> dict[str, tuple]:
    """job id -> (CPU use %, memory peak GB, the job's submit time or None).
    With ``with_submit`` (jobs from an export), the submit time tells whether
    the metrics are of that job or of an earlier one with the same number;
    it is read in one pass over the jobs, not looked up job by job (a hub's
    combined.db has no index on job ids)."""
    cols = _columns(conn, "job_summary")
    if not {"job_id", "avg_cpu_percent", "peak_memory_gb"} <= cols:
        return {}
    where, args = _site_filter(conn, "job_summary", site)
    out = {str(r[0]): (r[1], r[2], None) for r in conn.execute(
        f"SELECT job_id, avg_cpu_percent, peak_memory_gb FROM job_summary WHERE 1=1{where}", args)}
    if with_submit and out and {"job_id", "submit_time"} <= _columns(conn, "jobs"):
        w, a = _site_filter(conn, "jobs", site)
        for jid, sub in conn.execute(f"SELECT job_id, submit_time FROM jobs WHERE 1=1{w}", a):
            v = out.get(str(jid))
            if v is not None:
                out[str(jid)] = (v[0], v[1], parse_time(sub))
    return out


def _inventory(conn, data: Data) -> dict[str, set[str]]:
    """Cores, GPUs and memory per node: the latest sample of each node before
    the period's end; report.toml's figures win where given. Also the nodes
    of each partition (for the partition classes)."""
    cfg = data.cfg
    partition_nodes: dict[str, set[str]] = defaultdict(set)
    cols = _columns(conn, "node_state") if conn is not None else set()
    if {"node_name", "cpus_total", "timestamp"} <= cols:
        where, args = _site_filter(conn, "node_state", data.site)
        extra = ", ".join(c if c in cols else f"NULL AS {c}" for c in ("gres", "memory_total_mb", "partitions"))
        # The nodes sampled in the period, each as last seen in it (a node
        # retired before the period isn't part of it). A node of the tier
        # map with no sample in the period: as last seen at any time.
        rows = conn.execute(
            f"SELECT node_name, cpus_total, {extra}, MAX(timestamp) FROM node_state "
            f"WHERE timestamp >= ? AND timestamp < ?{where} GROUP BY node_name",
            [data.t0.isoformat(), data.t1.isoformat()] + args).fetchall()
        sampled = {r[0] for r in rows}
        missing = [n for nodes in cfg.tiers.values() for n in nodes if n not in sampled]
        for i in range(0, len(missing), 400):
            chunk = missing[i:i + 400]
            marks = ",".join("?" * len(chunk))
            rows += conn.execute(
                f"SELECT node_name, cpus_total, {extra}, MAX(timestamp) FROM node_state "
                f"WHERE node_name IN ({marks}){where} GROUP BY node_name", chunk + args).fetchall()
        for r in rows:
            node = r[0]
            data.nodes[node] = NodeInfo(cores=r[1], gpus=calcs.gres_gpus(r[2]), memory_mb=r[3])
            for p in str(r[4] or "").split(","):
                if p.strip():
                    partition_nodes[p.strip()].add(node)
                    data.names.partitions.add(p.strip())
        if data.nodes:
            data.inventory_source = INVENTORY_SAMPLES
    for t, nodes in cfg.tiers.items():
        for n in nodes:
            data.nodes.setdefault(n, NodeInfo())
    for n, c in cfg.cores_per_node.items():
        data.nodes.setdefault(n, NodeInfo()).cores = c
    for n, g in cfg.gpus_per_node.items():
        data.nodes.setdefault(n, NodeInfo()).gpus = g
    if cfg.cores_per_node or cfg.gpus_per_node:
        data.inventory_source = ("report.toml" + (" and " + data.inventory_source
                                                  if data.inventory_source else ""))
    mems = [i.memory_mb for i in data.nodes.values() if i.memory_mb]
    data.node_memory_mb_max = max(mems) if mems else None
    return partition_nodes


def load(cfg: ClusterConfig, t0: datetime, t1: datetime, *, db: Path | None = None,
         site: str | None = None, sacct: Path | None = None, exclude: set[str] | None = None,
         user_map: Path | None = None, teaching_site: str | None = None,
         keep_people: bool = False) -> Data:
    """Everything the sections need, names removed (``keep_people``: also the
    period's usernames, for the user-map template only)."""
    conn = open_db(db) if db is not None else None
    try:
        if conn is not None and sacct is None and not _columns(conn, "jobs"):
            raise SourceError(f"{db}: no jobs table")
        if conn is not None:
            known = sites(conn)
            if known:
                if site is None:
                    if len(known) > 1:
                        raise SourceError("this database holds several sites; choose one with "
                                          f"--cluster ({', '.join(known)})")
                    site = known[0]
                elif site not in known and sacct is None:
                    raise SourceError(f"no site {site!r} in this database ({', '.join(known)})")
        data = Data(cfg=cfg, t0=t0, t1=t1, site=site,
                    source=(f"{JOBS_EXPORT} {Path(sacct).name})" if sacct else JOBS_DB),
                    db_label=(f"{DB_LABEL} {Path(db).name}" + (f", site {site}" if site else ""))
                    if db is not None else None)
        partition_nodes = _inventory(conn, data) if conn is not None else _inventory(None, data)
        derived = partition_classes_from_nodes(partition_nodes, cfg) if cfg.has_tier_map else {}
        umap, extra = (load_user_map(user_map) if user_map else (None, set()))
        data.user_map_given = umap is not None
        data.names.others |= extra
        summaries = _summaries(conn, site, with_submit=sacct is not None) if conn is not None else {}
        builder = _Builder(data, _Classes(cfg, derived), set(cfg.exclude_users) | (exclude or set()),
                           umap, summaries, from_sacct=sacct is not None)
        if sacct is not None:
            _jobs_from_export(Path(sacct), builder)
        else:
            _jobs_from_db(conn, site, data, builder)
        builder.finish()
        if keep_people:
            data.people_names = sorted(builder.user_ids)
        data.job_summary_count = sum(1 for j in data.jobs if j.cpu_pct is not None or j.peak_mem_gb is not None)
        if conn is not None:
            _node_samples(conn, data)
            _gpu_samples(conn, data)
            _filesystems(conn, data)
            _usage(conn, data)
            if teaching_site:
                _teaching(conn, data, teaching_site)
        return data
    finally:
        if conn is not None:
            conn.close()


def _jobs_from_db(conn, site, data: Data, builder: _Builder) -> None:
    cols = _columns(conn, "jobs")
    sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in _JOB_COLS)
    where, args = _site_filter(conn, "jobs", site)
    # Wide on purpose (string times in two formats): the builder decides.
    lo = (data.t0 - timedelta(days=1)).isoformat()
    hi = (data.t1 + timedelta(days=1)).isoformat()
    sql = (f"SELECT {sel} FROM jobs WHERE (end_time IS NULL OR end_time >= ?) "
           f"AND (COALESCE(start_time, submit_time) < ? OR submit_time < ?){where}")
    for r in conn.execute(sql, [lo[:10], hi, hi] + args):
        builder.add(dict(r))
    if {"alloc_gpus", "start_time"} <= cols:
        w, a = _site_filter(conn, "jobs", site)
        row = conn.execute(f"SELECT MIN(start_time) FROM jobs WHERE alloc_gpus > 0{w}", a).fetchone()
        first = parse_time(row[0]) if row else None
        if first is not None and (data.gpu_first_alloc is None or first < data.gpu_first_alloc):
            data.gpu_first_alloc = first


def _jobs_from_export(path: Path, builder: _Builder) -> None:
    from nomad.collectors.sacct_import import open_export, quiet_collector, read_export
    parser = quiet_collector()
    for rec in read_export(open_export(str(path))):
        if rec is None:
            builder.d.set_aside[UNREADABLE] += 1
            continue
        jid = (rec.get("JobID") or "").strip()
        if "." in jid:
            continue
        try:
            job = parser.job_from_fields(rec)
        except Exception:
            job = None
        if job is None:
            builder.d.set_aside[UNREADABLE] += 1
            continue
        row = job.to_dict()
        if "[" in str(row.get("job_id")):
            # A pending array range: one row for tasks that haven't started.
            # It counts as one waiting job (its person was waiting).
            builder.d.pending_ranges += 1
        builder.add(row)


def _node_samples(conn, data: Data) -> None:
    cols = _columns(conn, "node_state")
    if not {"node_name", "cpus_total", "cpus_alloc", "cpu_load", "timestamp"} <= cols:
        return
    where, args = _site_filter(conn, "node_state", data.site)
    rng = [data.t0.isoformat(), data.t1.isoformat()] + args
    for r in conn.execute(
            "SELECT node_name, substr(timestamp, 1, 7), COUNT(*), "
            "SUM(1.0 * cpus_alloc / cpus_total), SUM(MIN(cpu_load, cpus_total) * 1.0 / cpus_total) "
            f"FROM node_state WHERE timestamp >= ? AND timestamp < ? AND cpus_total > 0{where} "
            "GROUP BY node_name, 2", rng):
        data.node_months[(r[0], r[1])] = [r[2], r[3] or 0.0, r[4] or 0.0]
    span = conn.execute(f"SELECT MIN(timestamp), MAX(timestamp) FROM node_state "
                        f"WHERE timestamp >= ? AND timestamp < ?{where}", rng).fetchone()
    if span and span[0]:
        data.node_state_span = (parse_time(span[0]), parse_time(span[1]))
    if "state" in cols:
        for r in conn.execute(
                "SELECT node_name, substr(timestamp, 1, 7), state, COUNT(*) FROM node_state "
                f"WHERE timestamp >= ? AND timestamp < ?{where} GROUP BY node_name, 2, state", rng):
            data.node_state_classes.setdefault((r[0], r[1]), Counter())[node_state_class(r[2])] += r[3]
        for r in conn.execute(
                "SELECT node_name, MIN(timestamp), MAX(timestamp), COUNT(*) FROM node_state "
                f"WHERE timestamp >= ? AND timestamp < ?{where} GROUP BY node_name", rng):
            a, b = parse_time(r[1]), parse_time(r[2])
            if a and b:
                data.node_spans[r[0]] = (a, b)
            if a and b and r[3] > 1:
                data.node_sample_hours[r[0]] = (b - a).total_seconds() / 3600.0 / (r[3] - 1)
        # Whole-cluster outages: runs in which nearly every node was down.
        down_sql = ("((upper(state) LIKE '%DOWN%' AND upper(state) NOT LIKE '%POWER%') "
                    "OR upper(state) LIKE '%NOT_RESPONDING%' OR state LIKE '%*' OR upper(state) LIKE '%FAIL%')")
        run_start = prev = None
        for r in conn.execute(
                f"SELECT timestamp, COUNT(*), SUM({down_sql}) FROM node_state "
                f"WHERE timestamp >= ? AND timestamp < ?{where} GROUP BY timestamp ORDER BY timestamp", rng):
            t = parse_time(r[0])
            out = r[1] >= 3 and (r[2] or 0) >= 0.9 * r[1]
            if out and run_start is None:
                run_start = t
            elif not out and run_start is not None:
                data.outages.append((run_start, t))
                run_start = None
            prev = t
        if run_start is not None and prev is not None:
            data.outages.append((run_start, prev))


def _gpu_samples(conn, data: Data) -> None:
    cols = _columns(conn, "gpu_stats")
    if not {"gpu_util_percent", "timestamp"} <= cols:
        return
    where, args = _site_filter(conn, "gpu_stats", data.site)
    rng = [data.t0.isoformat(), data.t1.isoformat()] + args
    for r in conn.execute(
            "SELECT substr(timestamp, 1, 7), COUNT(*), SUM(gpu_util_percent > 0), SUM(gpu_util_percent) "
            f"FROM gpu_stats WHERE gpu_util_percent IS NOT NULL AND timestamp >= ? AND timestamp < ?{where} "
            "GROUP BY 1", rng):
        data.gpu_months[r[0]] = [r[1], r[2] or 0, r[3] or 0.0]
    span = conn.execute(f"SELECT MIN(timestamp), MAX(timestamp) FROM gpu_stats "
                        f"WHERE timestamp >= ? AND timestamp < ?{where}", rng).fetchone()
    if span and span[0]:
        data.gpu_stats_span = (parse_time(span[0]), parse_time(span[1]))


def _filesystems(conn, data: Data) -> None:
    cols = _columns(conn, "filesystems")
    if not {"path", "used_bytes", "total_bytes", "timestamp"} <= cols:
        return
    where, args = _site_filter(conn, "filesystems", data.site)
    rows = conn.execute(
        "SELECT path, substr(timestamp, 1, 7), MAX(used_bytes), MAX(total_bytes), MAX(timestamp) "
        f"FROM filesystems WHERE timestamp < ?{where} GROUP BY path, 2 ORDER BY path, 2",
        [data.t1.isoformat()] + args).fetchall()
    wanted = set(data.cfg.storage_paths)
    for path, month, used, total, last in rows:
        if wanted and path not in wanted:
            continue
        data.filesystems.setdefault(path, []).append(
            (month, float(used or 0), float(total or 0), parse_time(last)))
    # Each filesystem's last reading before the period's end: a month's
    # highest reading can be a spike that was cleaned up days later.
    for path, used, total, last in conn.execute(
            "SELECT path, used_bytes, total_bytes, MAX(timestamp) FROM filesystems "
            f"WHERE timestamp < ?{where} GROUP BY path", [data.t1.isoformat()] + args):
        if path in data.filesystems:
            data.fs_latest[path] = (float(used or 0), float(total or 0), parse_time(last))
    data.fs_labels = data.labels(data.filesystems)


def _usage(conn, data: Data) -> None:
    cols = _columns(conn, "cluster_usage")
    if not {"month", "tres", "allocated_h", "reported_h"} <= cols:
        return
    where, args = _site_filter(conn, "cluster_usage", data.site)
    last = calcs.month_key(data.t1 - timedelta(seconds=1))
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM cluster_usage WHERE month <= ?{where} ORDER BY month", [last] + args)]
    # One Slurm cluster per site; if a site reports several, keep the one
    # named like the report's cluster, else all of them.
    names = {r.get("cluster") for r in rows}
    if len(names) > 1 and data.cfg.name in names:
        rows = [r for r in rows if r.get("cluster") == data.cfg.name]
    elif len(names) > 1:
        # Several Slurm clusters report here: their totals are added.
        acc: dict[tuple, dict] = {}
        for r in rows:
            k = (r["month"], r.get("tres", "cpu"))
            a = acc.setdefault(k, {"month": r["month"], "tres": r.get("tres", "cpu"), "settled": 1})
            for f in ("allocated_h", "down_h", "planned_down_h", "idle_h", "planned_h", "reported_h"):
                a[f] = a.get(f, 0.0) + float(r.get(f) or 0.0)
            a["settled"] = min(a["settled"], int(r.get("settled", 1) or 0))
        rows = [acc[k] for k in sorted(acc)]
        data.usage_clusters = len(names)
    data.usage_rows = rows


def _teaching(conn, data: Data, site: str) -> None:
    cols = _columns(conn, "interactive_sessions")
    if not {"timestamp", "user", "session_type"} <= cols:
        data.teaching_site = site
        return
    data.teaching_site = site
    where, args = _site_filter(conn, "interactive_sessions", site)
    rng = [data.t0.isoformat(), data.t1.isoformat()] + args
    by_type = []
    for r in conn.execute(
            "SELECT session_type, COUNT(DISTINCT user), COUNT(*), COUNT(DISTINCT substr(timestamp, 1, 16)), "
            "AVG(cpu_percent), AVG(mem_mb), MAX(mem_mb), AVG(CASE WHEN is_idle THEN 1.0 ELSE 0.0 END) "
            f"FROM interactive_sessions WHERE timestamp >= ? AND timestamp < ?{where} GROUP BY 1 ORDER BY 3 DESC",
            rng):
        by_type.append({"type": data.safe_label(r[0]), "people": r[1], "samples": r[2], "times": r[3],
                        "cpu_pct": r[4], "mem_mb": r[5], "max_mem_mb": r[6], "idle": r[7]})
    people = conn.execute(f"SELECT COUNT(DISTINCT user) FROM interactive_sessions "
                          f"WHERE timestamp >= ? AND timestamp < ?{where}", rng).fetchone()[0]
    for r in conn.execute(f"SELECT DISTINCT user FROM interactive_sessions "
                          f"WHERE timestamp >= ? AND timestamp < ?{where}", rng):
        data.names.users.add(str(r[0]))
    peak = None
    if {"total_sessions", "timestamp"} <= _columns(conn, "interactive_summary"):
        w2, a2 = _site_filter(conn, "interactive_summary", site)
        row = conn.execute(f"SELECT MAX(total_sessions), AVG(total_sessions), MAX(total_memory_mb) "
                           f"FROM interactive_summary WHERE timestamp >= ? AND timestamp < ?{w2}",
                           [data.t0.isoformat(), data.t1.isoformat()] + a2).fetchone()
        if row and row[0] is not None:
            peak = {"peak": row[0], "mean": row[1], "peak_memory_mb": row[2]}
    if by_type or peak:
        data.teaching = {"by_type": by_type, "people": people, "concurrency": peak}
