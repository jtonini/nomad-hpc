# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The demo's cast (1.17.4): one person for each way the Console is seen.

`nomad demo --console DIR` makes a demo the Console can run on: the demo
cluster's data in DIR/nomad_demo.db, with these people in it, and the
nomad.toml (DIR/.config/nomad/nomad.toml) that makes each of them what they
are -- the PI's lab, the instructor's course, staff roles. Started with
HOME=DIR and NOMAD_CONSOLE_DEMO=1, the Console signs anyone in as one of
them from its sign-in page, with no password: for trying it, tutorials and
screenshots. Nothing here is a real person.

    student             bob     takes CS 101; his own jobs, some running now
    research assistant  eve     Grace's lab's research assistant: sees it as
                                its PI does, but for Grace's own work
    PI                  grace   leads the Grace Lab (group grace$): alice,
                                charlie and eve
    instructor          henry   teaches CS 101 this term
    operator            olive   runs the systems: sees everything, changes
                                no settings
    admin               adam    everything, settings included

The database says it is a demo (table demo_info, with the cast in it): the
Console offers its sign-in picker only on such a database.
"""
from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

DEMO_INFO = "demo_info"
LAB_GROUP = "grace$"
LAB_NAME = "Grace Lab"
LAB_MEMBERS = ("alice", "charlie", "eve", "grace")
COURSE = "cs101"
COURSE_NAME = "Introduction to Computing"
FACULTY_GROUP = "faculty"
FACULTY = ("grace", "henry")
SITE = "demo-cluster"
# The demo's nomad.toml begins so: the only one `nomad demo --console` replaces.
TOML_MARK = "# nomad.toml for the Console's demo (nomad demo --console): made-up people."


@dataclass(frozen=True)
class Person:
    user: str
    label: str          # what they are, as the sign-in picker says it
    about: str          # one line on what they see
    role: str           # the Console's role: viewer, operator or admin


CAST = (
    Person("bob", "Student", "Takes CS 101. Sees his own jobs, some running now.", "viewer"),
    Person("eve", "Research assistant",
           "The Grace Lab's research assistant: sees its members, one by one, but not Grace's own work.",
           "viewer"),
    Person("grace", "PI", "Leads the Grace Lab: sees its members, one by one, and its work.", "viewer"),
    Person("henry", "Instructor", "Teaches CS 101 this term: sees his class.", "viewer"),
    Person("olive", "Operator", "Runs the systems: sees everything, changes no settings.", "operator"),
    Person("adam", "Admin", "Everything, settings included.", "admin"),
)


def cast_dicts() -> list[dict]:
    return [asdict(p) for p in CAST]


def term_start(now: datetime) -> str:
    """This demo term's first day: three weeks ago."""
    return (now - timedelta(days=21)).strftime("%Y-%m-%d")


def demo_toml(now: datetime | None = None) -> str:
    """nomad.toml for the cast: staff roles, the PI's lab, the instructor's
    course (docs/config.md, Console access and Courses)."""
    now = now or datetime.now()
    admins = [p.user for p in CAST if p.role == "admin"]
    operators = [p.user for p in CAST if p.role == "operator"]
    return f"""{TOML_MARK}

[cluster]
name = "{SITE}"

[general]
cluster_name = "{SITE}"            # what the Console's pages call the demo cluster

[console.roles]
admin = {json.dumps(admins)}
operator = {json.dumps(operators)}

[console.labs]
group_pattern = "{{netid}}$"           # grace leads grace$

[console.labs.resources."{LAB_GROUP}"]
name = "{LAB_NAME}"
storage = ["{STORAGE_LAB[0]}"]                # the lab's own storage server

[console.storage."{STORAGE_HOME[0]}"]
note = "everyone's $HOME"

# What each machine is, shown beside its name (nomad 1.17.7)
[console.machines."{SITE}"]
note = "the demo's cluster"

[console.machines."{STORAGE_LAB[0]}"]
note = "the Grace Lab's NAS"

[console.machines."login-node-01"]
note = "a login node: for editing and submitting jobs, not for running them"

[console.labs.assistants]
"{LAB_GROUP}" = ["eve"]                # sees the lab as its PI does (nomad 1.17.5)

[console.courses]
sources = ["groups"]
pattern = "^[a-z]{{2,6}}[0-9]{{3}}[a-z]?$"
faculty_group = "{FACULTY_GROUP}"
term_start = "{term_start(now)}"
names = true

[console.courses.groups."{COURSE}"]
name = "{COURSE_NAME}"
instructors = ["henry"]
"""


def _columns(conn, table) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _after_readings(conn, tables: set, now: datetime) -> datetime:
    """The demo's "now": whole seconds, after every reading the generator
    and the stress scenarios wrote just before (so the cast's node reading
    is the latest one), and nothing after it. GPU samples, whose window ran
    to the generator's latest job end, stop at now."""
    if "gpu_stats" in tables and "timestamp" in _columns(conn, "gpu_stats"):
        conn.execute("DELETE FROM gpu_stats WHERE timestamp > ?", (now.isoformat(),))
    latest = now
    soon = now + timedelta(minutes=1)
    for table in sorted(tables):
        if "timestamp" not in _columns(conn, table):
            continue
        t = conn.execute(f'SELECT MAX(timestamp) FROM "{table}" WHERE timestamp <= ?',
                         (soon.isoformat(),)).fetchone()[0]
        try:
            t = datetime.fromisoformat(str(t)) if t else None
        except ValueError:
            continue
        if t and t.tzinfo is None and t > latest:
            latest = t
    whole = latest.replace(microsecond=0)
    return whole if whole >= latest else whole + timedelta(seconds=1)


def add_cast(db_path, now: datetime | None = None, seed: int | None = None, days: int = 7) -> dict:
    """Put the cast in a demo database: their groups (the lab, the faculty,
    CS 101), jobs of theirs running and waiting now (with others' running
    beside them), the nodes as those jobs hold them, and the demo_info
    table. Ended jobs that the generator let end after now are moved back
    within the last `days`. Returns what was done (counts)."""
    now = now or datetime.now()
    rnd = random.Random(seed)
    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        now = _after_readings(conn, tables, now)
        conn.execute("""CREATE TABLE IF NOT EXISTS group_membership (
            username TEXT, group_name TEXT, gid INTEGER, cluster TEXT,
            PRIMARY KEY (username, group_name, cluster))""")
        # One name for the demo cluster everywhere (its nodes and groups were "demo").
        for table in ("node_state", "nodes", "group_membership"):
            if table in tables | {"group_membership"} and "cluster" in _columns(conn, table):
                conn.execute(f"UPDATE OR REPLACE {table} SET cluster = ? WHERE cluster = 'demo'", (SITE,))
        groups = [(u, LAB_GROUP, 4001) for u in LAB_MEMBERS] + \
                 [(u, FACULTY_GROUP, 4002) for u in FACULTY] + [("henry", COURSE, 2001)]
        for user, group, gid in groups:
            conn.execute("INSERT OR REPLACE INTO group_membership (username, group_name, gid, cluster) "
                         "VALUES (?, ?, ?, ?)", (user, group, gid, SITE))

        # The demo's ended jobs end before now: the generator lets a long one
        # end in the future, which pages show as "ended 0 m ago". Each is
        # moved back whole to end at a moment anywhere in the window (one
        # longer than the window, just before now), its accounting with it.
        shifted = 0
        window = timedelta(days=days)
        acct = "job_accounting" in tables and {"job_id", "cluster", "submit_time"} <= _columns(conn, "job_accounting")
        for jid, site, sub, start, end in conn.execute(
                "SELECT job_id, cluster, submit_time, start_time, end_time FROM jobs WHERE end_time > ?",
                (now.isoformat(),)).fetchall():
            try:
                t_sub, t_end = datetime.fromisoformat(sub), datetime.fromisoformat(end)
            except (TypeError, ValueError):
                continue
            room = window - (t_end - t_sub) - timedelta(minutes=10)
            lands = now - timedelta(minutes=10) - (room * rnd.random() if room > timedelta(0) else timedelta(0))
            back = t_end - lands
            moved = [(datetime.fromisoformat(t) - back).isoformat() if t else t for t in (sub, start, end)]
            conn.execute("UPDATE jobs SET submit_time = ?, start_time = ?, end_time = ? "
                         "WHERE job_id = ? AND cluster = ?", (*moved, jid, site))
            if acct:
                conn.execute("UPDATE job_accounting SET submit_time = ? WHERE job_id = ? AND cluster = ?",
                             (moved[0], jid, site))
            shifted += 1

        # What squeue would show now: the waiting reason and nodes asked for.
        cols = _columns(conn, "jobs")
        for col, kind in (("pending_reason", "TEXT"), ("req_nodes", "INTEGER"), ("ready_since", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {kind}")

        def at(**kw):
            return (now - timedelta(**kw)).isoformat()

        # Numbers after the generated ones (and the stress scenarios' 7700000s).
        top = conn.execute("SELECT MAX(CAST(job_id AS INTEGER)) FROM jobs "
                           "WHERE job_id GLOB '[0-9]*' AND job_id NOT GLOB '*[^0-9]*'").fetchone()[0] or 0
        first = max(int(top) + 1, 9000001)
        # (user, partition, node, state, name, cpus, mem GB, gpus, limit h, submitted, started, reason)
        now_jobs = [
            ("bob", "compute", "node02", "RUNNING", "assignment3", 4, 16, 0, 4, at(hours=2, minutes=10),
             at(hours=2), None),
            ("bob", "compute", None, "PENDING", "assignment3-big", 32, 128, 0, 8, at(minutes=25), None, "Priority"),
            ("eve", "gpu", "gpu02", "RUNNING", "train-model", 8, 64, 2, 24, at(hours=6), at(hours=5), None),
            ("alice", "highmem", None, "PENDING", "genome_align", 32, 400, 0, 48, at(hours=1), None, "Resources"),
            ("grace", "compute", "node05", "RUNNING", "simulation", 16, 32, 0, 12, at(hours=3), at(hours=3), None),
            # Others' work, so the nodes are busy as a cluster is: alice's
            # 400 GB waits because the big-memory nodes are full.
            ("diana", "highmem", "node07", "RUNNING", "genome_align", 48, 420, 0, 72, at(hours=20),
             at(hours=19), None),
            ("frank", "highmem", "node08", "RUNNING", "md_sim", 40, 380, 0, 48, at(hours=9), at(hours=8), None),
            ("charlie", "compute", "node04", "RUNNING", "dft_calc", 24, 96, 0, 24, at(hours=4), at(hours=4), None),
            # One that will never start: the job it waits for failed (My
            # lab's Needs attention, 1.17.6).
            ("charlie", "compute", None, "PENDING", "post_process", 4, 16, 0, 4, at(days=2, hours=3), None,
             "DependencyNeverSatisfied"),
            ("frank", "gpu", "gpu01", "RUNNING", "inference", 8, 48, 1, 8, at(hours=1, minutes=30),
             at(hours=1, minutes=20), None),
        ]
        for n, (user, part, node, state, name, cpus, mem, gpus, hours, sub, start, reason) in enumerate(now_jobs):
            conn.execute(
                "INSERT INTO jobs (job_id, cluster, user_name, partition, node_list, job_name, state, "
                "submit_time, start_time, end_time, req_cpus, req_mem_mb, req_gpus, req_time_seconds, "
                "runtime_seconds, wait_time_seconds, pending_reason, req_nodes, ready_since) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, 1, ?)",
                (str(first + n), SITE, user, part, node, name, state, sub, start, cpus, mem * 1024, gpus,
                 hours * 3600,
                 int((datetime.fromisoformat(start) - datetime.fromisoformat(sub)).total_seconds())
                 if start else None,
                 reason, sub if reason in ("Priority", "Resources") else None))

        # The nodes as nomad last read them, under the jobs' site name: what
        # the running jobs hold, and nothing else.
        nodes = 0
        if "node_state" in tables:
            from nomad.demo import DEMO_CLUSTER
            held = {}
            for _u, _p, node, state, _n, cpus, mem, gpus, _h, _s, _st, _r in now_jobs:
                if node and state == "RUNNING":
                    c, m, g = held.get(node, (0, 0, 0))
                    held[node] = (c + cpus, m + mem * 1024, g + gpus)
            stamp = now.isoformat()
            ncols = _columns(conn, "node_state")
            for n in DEMO_CLUSTER["nodes"]:
                cpus, mem, _gpus = held.get(n["name"], (0, 0, 0))
                cpus, total = min(cpus, n["cores"]), n["memory_gb"] * 1024
                mem = min(mem, total)
                state = "allocated" if cpus >= n["cores"] else "mixed" if cpus else "idle"
                row = {"timestamp": stamp, "node_name": n["name"], "state": state, "cpus_total": n["cores"],
                       "cpus_alloc": cpus, "cpu_load": round(cpus * rnd.uniform(0.7, 1.0), 2),
                       "memory_total_mb": total, "memory_alloc_mb": mem, "memory_free_mb": total - mem,
                       "cpu_alloc_percent": round(100 * cpus / n["cores"], 1),
                       "memory_alloc_percent": round(100 * mem / total, 1), "cluster": SITE,
                       "partitions": n["partition"], "gres": f"gpu:{n['gpus']}" if n["gpus"] else "",
                       "is_healthy": 1}
                row = {k: v for k, v in row.items() if k in ncols}
                conn.execute(f"INSERT INTO node_state ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                             tuple(row.values()))
                nodes += 1

        # The queue as squeue would count it now, from the jobs held (the
        # generator's readings run to just before).
        if "queue_state" in tables and {"timestamp", "partition", "pending_jobs", "running_jobs",
                                        "total_jobs"} <= _columns(conn, "queue_state"):
            counts = {}
            for part, state, k in conn.execute(
                    "SELECT partition, state, COUNT(*) FROM jobs WHERE end_time IS NULL "
                    "AND state IN ('RUNNING', 'PENDING') GROUP BY partition, state"):
                counts.setdefault(part, {"RUNNING": 0, "PENDING": 0})[state] = k
            from nomad.demo import DEMO_CLUSTER
            for part in sorted(({n["partition"] for n in DEMO_CLUSTER["nodes"]} | set(counts)) - {None}):
                c = counts.get(part, {"RUNNING": 0, "PENDING": 0})
                conn.execute("INSERT INTO queue_state (timestamp, partition, pending_jobs, running_jobs, "
                             "total_jobs) VALUES (?, ?, ?, ?, ?)",
                             (now.isoformat(), part, c["PENDING"], c["RUNNING"], c["PENDING"] + c["RUNNING"]))

        owners = _storage_use(conn, now)

        conn.execute(f"CREATE TABLE IF NOT EXISTS {DEMO_INFO} (key TEXT PRIMARY KEY, value TEXT)")
        for key, value in (("made_by", "nomad demo --console"), ("made_at", now.isoformat()),
                           ("cast", json.dumps(cast_dicts()))):
            conn.execute(f"INSERT OR REPLACE INTO {DEMO_INFO} (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()
    return {"groups": len(groups), "jobs_now": len(now_jobs), "node_readings": nodes, "moved_back": shifted,
            "storage_owners": owners}


# Who holds what on the demo's storage servers (1.17.6), as the storage_use
# collector would have stored it: everyone's $HOME on nas-01, the Grace Lab's
# own data on nas-02. (user, nas-01 /home GB now, a week ago, unchanged for a
# year; nas-02 /data GB now, a week ago)
STORAGE_HOME = ("nas-01", "/home")
STORAGE_LAB = ("nas-02", "/data")
_HELD = (("alice", 1800, 1350, 1100, 900, 880), ("charlie", 420, 410, 40, 250, 240),
         ("eve", 160, 150, 5, 60, 20), ("grace", 2600, 2590, 2000, 300, 300),
         ("bob", 12, 9, 0, 0, 0), ("henry", 35, 35, 20, 0, 0), ("diana", 950, 940, 300, 0, 0),
         ("frank", 610, 400, 100, 0, 0))


def _storage_use(conn, now: datetime) -> int:
    from nomad.collectors.storage_use import SCHEMA
    conn.executescript(SCHEMA)
    gb = 10 ** 9
    stamp, before = now.isoformat(), (now - timedelta(days=8)).isoformat()
    for (server, path), total in ((STORAGE_HOME, 40_000), (STORAGE_LAB, 4_000)):
        i = 1 if path == "/home" else 4
        used = sum(h[i] for h in _HELD)
        # As big as the storage collector says the server is, when it does.
        try:
            row = conn.execute("SELECT total_bytes FROM storage_state WHERE hostname = ? "
                               "ORDER BY timestamp DESC LIMIT 1", (server,)).fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0] and row[0] // gb > used:
            total = row[0] // gb
        conn.execute("INSERT INTO storage_paths (timestamp, server, path, fs, total_bytes, used_bytes, "
                     "available_bytes, accounting) VALUES (?, ?, ?, 'zfs', ?, ?, ?, 1)",
                     (stamp, server, path, total * gb, used * gb, (total - used) * gb))
        conn.execute("INSERT INTO storage_reports (server, mode, started, finished, host, read_at, paths, owners) "
                     "VALUES (?, 'usage', ?, ?, ?, ?, 1, ?)", (server, stamp, stamp, server, stamp, len(_HELD)))
    n = 0
    for user, home, home_was, old, lab, lab_was in _HELD:
        for (server, path), now_gb, was_gb in ((STORAGE_HOME, home, home_was), (STORAGE_LAB, lab, lab_was)):
            if not now_gb:
                continue
            for when, size in ((stamp, now_gb), (before, was_gb)):
                conn.execute("INSERT INTO storage_owners (timestamp, server, path, kind, name, used_bytes) "
                             "VALUES (?, ?, ?, 'user', ?, ?)", (when, server, path, user, size * gb))
            n += 1
        conn.execute("INSERT INTO storage_tops (scanned_at, server, path, top, top_owner, bytes, files, "
                     "older1_bytes, older2_bytes, age1_days, age2_days, complete) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 180, 365, 1)",
                     ((now - timedelta(hours=7)).isoformat(), *STORAGE_HOME, user, user, home * gb,
                      home * 900, min(home, old * 1.2) * gb, old * gb))
    return n


def is_demo_db(db_path) -> bool:
    """Whether the database is one `nomad demo --console` made."""
    path = Path(db_path).expanduser()
    if not path.is_file():
        return False
    try:
        # As a file: URI of the resolved path, so '#', '?' or '%' in a folder
        # name are the name's, not the URI's.
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            # Only its own two columns: a copy a sync made has more (the
            # Console's demo check asks the same).
            if [r[1] for r in conn.execute(f"PRAGMA table_info({DEMO_INFO})")] != ["key", "value"]:
                return False
            row = conn.execute(f"SELECT value FROM {DEMO_INFO} WHERE key = 'made_by'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return bool(row and row[0] == "nomad demo --console")


__all__ = ["CAST", "COURSE", "DEMO_INFO", "TOML_MARK", "LAB_GROUP", "LAB_MEMBERS", "Person", "add_cast",
           "cast_dicts", "demo_toml", "is_demo_db", "term_start"]
