"""1.8.0: `nomad usage-report`, from a small invented site whose every figure
can be worked out by hand.

Site c1: basic = cn[01-04], gpu = g01 (4 cards), condo = lab01; 10 cores
each. Period: January and February 2026 (59 days, 1,416 hours).
"""
import csv
import json
import os
import sqlite3
import stat
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad.collectors.slurm_usage import SlurmUsageCollector
from nomad.db.jobkeys import ensure_job_columns
from nomad.db.migrations import ensure_database
from nomad.usage import config as ucfg, guard, render, run, sections, sources
from nomad.usage.sources import Names

T0, T1 = datetime(2026, 1, 1), datetime(2026, 3, 1)

CONFIG = """
[report]
exclude_users = ["root"]

[report.clusters.c1]
tiers = { basic = "cn[01-04]", gpu = "g01", condo = "lab01" }
institutional_tiers = ["basic", "gpu"]
gpu_accounting_start = "2026-02-01T00:00:00"
unlisted_partitions = "condo"

[report.clusters.c1.capacity]
practical = 0.75

[[report.clusters.c1.gpu_families]]
name = "molecular dynamics"
regex = "gmx"
"""

NODES = {"cn01": "short,all", "cn02": "short,all", "cn03": "short,all", "cn04": "short,all",
         "g01": "gpus,all", "lab01": "zlab,all"}


@pytest.fixture(autouse=True)
def eastern(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def J(c, job_id, user, part, nodes, cpus, submit, start, elapsed_h, state="COMPLETED", end=None,
      name="job", gpus=0, rt=86400, tres="auto", mem_mb=None, root=None, tail=None):
    s = datetime.fromisoformat(start) if start else None
    e = datetime.fromisoformat(end) if end else (s + timedelta(hours=elapsed_h) if s else None)
    if tres == "auto":
        tres = f"cpu={cpus}" + (f",gres/gpu={gpus}" if gpus else "")
    c.execute("INSERT INTO jobs (job_id, user_name, group_name, partition, node_list, job_name, submit_time, "
              "start_time, end_time, state, req_cpus, req_mem_mb, req_gpus, req_time_seconds, runtime_seconds, "
              "alloc_tres, alloc_gpus, work_root, work_tail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (job_id, user, "people", part, nodes, name, submit, start, e.isoformat() if e else None, state,
               cpus, mem_mb, gpus, rt, int(elapsed_h * 3600) if s else None, tres, gpus if tres else None,
               root, tail))


def make_site(path: Path) -> Path:
    ensure_database(path)
    c = sqlite3.connect(path)
    ensure_job_columns(c)
    SlurmUsageCollector.ensure_schema(c)
    t = datetime(2025, 12, 1)
    while t < datetime(2026, 3, 5):
        for n, parts in NODES.items():
            down = n == "cn04" and datetime(2026, 2, 1) <= t < datetime(2026, 2, 11)
            alloc, load = {"g": (10, 20.0), "l": (2, 1.0)}.get(n[0], (5, 2.5))
            c.execute("INSERT INTO node_state (timestamp, node_name, cluster, state, cpus_total, cpus_alloc, "
                      "cpu_load, memory_total_mb, partitions, gres) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (t.isoformat(), n, "c1", "down*" if down else "mixed", 10, alloc, load, 256 * 1024, parts,
                       "gpu:a40:4(S:0-1)" if n == "g01" else "(null)"))
        t += timedelta(days=1)
    J(c, "1", "alice", "short", "cn01", 10, "2026-01-05T08:00:00", "2026-01-05T08:00:00", 10,
      mem_mb=65536, root="/home", tail="p/r1")
    J(c, "2", "alice", "short", "cn[02-03]", 20, "2026-01-10T00:00:00", "2026-01-12T00:00:00", 5,
      state="TIMEOUT", rt=None, mem_mb=131072, root="/scratch", tail="p/r2")
    J(c, "3", "bob", "short", "cn04", 10, "2025-12-31T19:00:00", "2025-12-31T20:00:00", 8,
      mem_mb=32768, root="/home", tail="q/r3")
    J(c, "4", "bob", "gpus", "g01", 4, "2026-02-10T00:00:00", "2026-02-10T00:00:00", 10, name="gmx_run", gpus=2)
    J(c, "5", "carol", "all", "lab01", 10, "2026-02-01T00:00:00", "2026-02-01T00:00:00", 24, state="FAILED")
    J(c, "6", "root", "short", "cn01", 10, "2026-01-20T00:00:00", "2026-01-20T00:00:00", 1)
    J(c, "7", "dave", "short", None, 4, "2026-02-20T00:00:00", None, 0, state="PENDING")
    J(c, "8", "erin", "short", "cn01", 10, "2025-11-30T00:00:00", "2025-12-01T00:00:00", 1)
    # Left the queue long before the period, outcome unknown: not in it.
    J(c, "12", "gina", "short", None, 4, "2025-06-01T00:00:00", None, 0, state="UNKNOWN")
    J(c, "9", "frank", "all", "g01", 2, "2026-02-15T00:00:00", "2026-02-15T00:00:00", 2, name="job_frank",
      rt=None, tres=None)
    # A start Slurm can't have written: after the job's end.
    J(c, "10", "alice", "short", "cn01", 10, "2026-01-20T10:00:00", "2026-01-25T00:00:00", 1,
      end="2026-01-20T12:00:00", rt=3600)
    for jid, cpu, peak in (("1", 50.0, 4.0), ("2", 25.0, 8.0), ("3", 80.0, 2.0), ("5", 90.0, 900.0)):
        c.execute("INSERT INTO job_summary (job_id, avg_cpu_percent, peak_memory_gb) VALUES (?,?,?)", (jid, cpu, peak))
    for i, u in enumerate((0, 50, 100, 0)):
        c.execute("INSERT INTO gpu_stats (timestamp, node_name, gpu_index, gpu_util_percent) VALUES (?,?,?,?)",
                  ("2026-02-12T10:00:00", "g01", i, u))
    for m, used in (("2025-11", 300e12), ("2025-12", 230e12), ("2026-01", 240e12), ("2026-02", 250e12)):
        c.execute("INSERT INTO filesystems (path, total_bytes, used_bytes, available_bytes, used_percent, timestamp) "
                  "VALUES (?,?,?,?,?,?)", ("/scratch", 313e12, used, 313e12 - used, used / 313e12 * 100, f"{m}-15T12:00:00"))
    per_year = {2021: 100, 2022: 100, 2023: 125, 2024: 150, 2025: 195, 2026: 240}
    y, mo = 2021, 7
    while (y, mo) <= (2026, 2):
        a = per_year[y]
        c.execute("INSERT INTO cluster_usage VALUES ('c1', ?, 'cpu', ?, 10, 0, ?, 50, 1000, ?, '2026-03-01')",
                  (f"{y}-{mo:02d}", a, 1000 - a - 10 - 50, 0 if (y, mo) == (2026, 2) else 1))
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    c.commit()
    c.close()
    return path


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path / "c1.db")


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "report.toml"
    p.write_text(CONFIG)
    return ucfg.load(p, "c1")


def build(site, cfg, **kw):
    report, data = run.build(cfg, T0, T1, db=site, site=None, **kw)
    return report, data


def fact(report, fid):
    for f in report.facts():
        if f.id == fid:
            return f.value
    raise KeyError(fid)


def test_population_and_set_aside(site, cfg):
    report, data = build(site, cfg)
    assert fact(report, "s01.people") == 5                     # alice bob carol dave frank; not root, not erin
    assert data.excluded_accounts == 1 and data.excluded_jobs == 1
    assert data.set_aside["never started"] == 1
    assert data.set_aside["start corrected"] == 1
    aside = {a.what: a.count for a in report.set_aside}
    assert aside["accounts that are not people (report.toml exclude_users)"] == 1
    assert aside["memory peak larger than any node"] == 1     # 900 GB on 256 GB nodes


def test_core_hours_clipped_and_whole(site, cfg):
    report, _ = build(site, cfg)
    # 100 + 100 + 40 (bob's job 3, 4 of its 8 h in January) + 40 + 4 + 10.
    assert fact(report, "s01.institutional_core_hours") == pytest.approx(294)
    assert fact(report, "s01.institutional_core_hours_whole") == pytest.approx(334)
    assert fact(report, "s01.institutional_cores") == 50
    assert fact(report, "s01.institutional_utilization") == pytest.approx(294 / (50 * 1416))
    assert fact(report, "s01.condo_core_hours") == pytest.approx(240)
    assert fact(report, "s01.jobs_run") == 7
    # By month submitted: January alice alone; February bob, carol, dave, frank.
    assert fact(report, "s01.people_per_month_min") == 1 and fact(report, "s01.people_per_month_max") == 4


def test_waits_weighted_and_scoped(site, cfg):
    report, _ = build(site, cfg)
    # Basic nodes only (the institutional tiers without GPUs); by month submitted.
    assert fact(report, "s04.core_hours.2026-01") == pytest.approx(210)
    assert fact(report, "s04.waiting_core_hours.2026-01") == pytest.approx(100)
    assert fact(report, "s04.people.2026-01") == 1 and fact(report, "s04.people_waited.2026-01") == 1
    assert fact(report, "s04.core_hours.2025-12") == pytest.approx(80)     # bob's job, submitted in December
    md = render.markdown(report)
    assert "Dec 2025 (part)" in md


def test_partition_classes_from_nodes(site, cfg):
    report, _ = build(site, cfg)
    assert fact(report, "s03.people.basic") == 3          # alice, bob, dave (short = cn[01-04])
    assert fact(report, "s03.people.gpu") == 1            # bob (gpus = g01)
    assert fact(report, "s03.people.overlay") == 2        # carol, frank (all spans every tier)
    assert fact(report, "s03.people.gpu_partitions_or_requests") == 1
    assert fact(report, "s03.people_overlay_only") == 2
    assert fact(report, "s03.people_several_classes") == 1   # bob: basic and GPU


def test_held_vs_used(site, cfg):
    report, _ = build(site, cfg)
    assert fact(report, "s05.allocation.basic") == pytest.approx(0.5)
    assert fact(report, "s05.load.basic") == pytest.approx(0.25)
    assert fact(report, "s05.load.gpu") == pytest.approx(1.0)          # load capped at the cores
    # Jobs 1, 2 and 3: (0.5·10·10 + 0.25·20·5 + 0.8·10·8) / (100 + 100 + 80).
    assert fact(report, "s05.cpu_efficiency.basic") == pytest.approx(139 / 280)
    assert fact(report, "s05.allocation.basic.2026-01") == pytest.approx(0.5)


def test_memory(site, cfg):
    report, _ = build(site, cfg)
    assert fact(report, "s06.requested_gb.basic") == pytest.approx((64 + 128 + 32) / 3)
    assert fact(report, "s06.peak_gb.basic") == pytest.approx((4 + 8 + 2) / 3)
    assert fact(report, "s06.request_to_use.basic") == pytest.approx(16)
    assert fact(report, "s06.jobs_measured") == 3


def test_memory_by_partition_class(site, cfg):
    # Small jobs through the overlay partition land on the basic nodes: they
    # are not the basic partition's work, but the basic nodes held them.
    c = sqlite3.connect(site)
    for i in range(6):
        J(c, f"3{i}", "carol", "all", "cn02", 1, "2026-01-15T00:00:00", "2026-01-15T00:00:00", 1, mem_mb=1024)
        c.execute("INSERT INTO job_summary (job_id, avg_cpu_percent, peak_memory_gb) VALUES (?,?,?)",
                  (f"3{i}", 90.0, 0.5))
    J(c, "40", "alice", "short", "cn03", 10, "2026-01-16T00:00:00", "2026-01-16T00:00:00", 2,
      mem_mb=2 * 1024 * 1024)
    c.execute("INSERT INTO job_summary (job_id, avg_cpu_percent, peak_memory_gb) VALUES ('40', 10.0, 200.0)")
    c.commit()
    c.close()
    report, _ = build(site, cfg)
    assert fact(report, "s06.requested_gb.basic") == pytest.approx((64 + 128 + 32 + 2048) / 4)
    assert fact(report, "s06.share_requesting_over_1tb.basic") == pytest.approx(0.25)
    assert fact(report, "s06.requested_gb.overlay") == pytest.approx(1.0)
    assert fact(report, "s06.request_to_use.overlay") == pytest.approx(2.0)
    # The nodes' view: every job on the basic nodes.
    assert fact(report, "s06.node.peak_gb.basic") == pytest.approx((4 + 8 + 2 + 200 + 6 * 0.5) / 10)
    assert fact(report, "s06.node.largest_peak_gb.basic") == pytest.approx(200)
    sec = next(s for s in report.sections if s.number == 6)
    assert sec.finding.startswith("Jobs request far more memory than they use: in the basic partitions")
    assert "25% of the basic partitions' jobs requested more than 1 TB" in sec.finding
    md = render.markdown(report)
    assert "| basic | 10 | 256 GB |" in md
    assert "by partition class | basic 11×, overlay 2×" in md


def test_short_outage_and_large_counts(site, cfg):
    c = sqlite3.connect(site)
    for ts, st in (("2026-01-15T00:10:00", "down*"), ("2026-01-15T00:15:00", "mixed")):
        for n, parts in NODES.items():
            c.execute("INSERT INTO node_state (timestamp, node_name, cluster, state, cpus_total, cpus_alloc, "
                      "cpu_load, memory_total_mb, partitions, gres) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (ts, n, "c1", st, 10, 0, 0.0, 256 * 1024, parts, "(null)"))
    for i in range(1200):
        J(c, f"9{i:04d}", "alice", "short", "cn01", 1, "2026-01-21T00:00:00", "2026-01-21T00:00:00", 0.01,
          rt=60, tres=None, root="/scratch" if i % 2 else None)
    c.commit()
    c.close()
    report, _ = build(site, cfg)
    assert fact(report, "s10.outages") == 1
    sec = next(s for s in report.sections if s.number == 10)
    assert "1 whole-cluster outage in the node samples (Jan 2026), under an hour in all." in sec.notes[0]
    md = render.markdown(report)
    assert "outage(s)" not in md
    assert "Working directories are known for 603 of 1,207 jobs" in md


def test_gpus(site, cfg):
    report, _ = build(site, cfg)
    assert fact(report, "s07.cards") == 4
    assert fact(report, "s07.gpu_hours") == pytest.approx(20)
    assert fact(report, "s07.allocation_share") == pytest.approx(20 / (4 * 28 * 24))
    assert fact(report, "s07.gpu_hours_by_family.molecular dynamics") == pytest.approx(20)
    assert fact(report, "s07.people_with_gpu_hours") == 1
    assert fact(report, "s07.gpu_people") == 1
    assert fact(report, "s07.gpu_nodes_without_gpu_use") == 1          # frank, through "all"
    assert fact(report, "s07.card_active_share.2026-02") == pytest.approx(0.5)
    assert fact(report, "s11.cpu_only_on_gpu_nodes_jobs") == 1
    assert fact(report, "s11.cpu_only_on_gpu_nodes_core_hours") == pytest.approx(4 / 44)


def test_gpu_accounting_start_detected(site, tmp_path):
    p = tmp_path / "r2.toml"
    p.write_text(CONFIG.replace('gpu_accounting_start = "2026-02-01T00:00:00"\n', ""))
    report, _ = build(site, ucfg.load(p, "c1"))
    # The first job with GPUs started on 10 Feb: from there to 1 March.
    assert fact(report, "s07.allocation_share") == pytest.approx(20 / (4 * 19 * 24))
    assert any("first job with GPUs" in a for a in report.assumptions)


def test_multi_year_load(site, cfg):
    report, _ = build(site, cfg)
    assert fact(report, "s02.growth.2023") == pytest.approx(0.25)
    assert fact(report, "s02.growth.2025") == pytest.approx(0.30)
    assert fact(report, "s02.cagr") == pytest.approx((2340 / 1200) ** (1 / 3) - 1)
    assert fact(report, "s02.growth_low") == pytest.approx(0.20)
    # February 2026 isn't settled: 2026 is January alone, against January 2025.
    assert fact(report, "s02.growth_same_months.2026") == pytest.approx(240 / 195 - 1)
    assert fact(report, "s02.annual.2026.allocated_h") == pytest.approx(240)
    assert fact(report, "s01.growth_same_months_last_year") == pytest.approx(240 / 195 - 1)
    sec = next(s for s in report.sections if s.number == 2)
    assert any("Feb 2026 left out" in n for n in sec.notes)


def test_storage_and_reliability(site, cfg):
    report, _ = build(site, cfg)
    assert fact(report, "s09.growth_per_month./scratch") == pytest.approx(10e12)
    assert fact(report, "s09.cleanup_month./scratch") == "2025-12"
    assert fact(report, "s09.months_to_full./scratch") == pytest.approx(6.3)
    assert fact(report, "s10.node_days_down.basic") == pytest.approx(10, abs=0.01)
    assert fact(report, "s10.nodes_out_over_7_days.basic") == 1
    assert fact(report, "s10.outages") == 0
    assert fact(report, "s10.jobs_share.TIMEOUT") == pytest.approx(1 / 7)


def test_policy(site, cfg):
    report, _ = build(site, cfg)
    assert fact(report, "s11.no_time_limit_jobs") == pytest.approx(1 / 6)   # job 9's limit is unknown
    assert fact(report, "s11.from_home_jobs") == pytest.approx(2 / 3)


def test_capacity(site, cfg, tmp_path):
    report, _ = build(site, cfg)
    base = 294 * 365 / 59
    assert fact(report, "s14.base") == pytest.approx(base)
    assert fact(report, "s14.growth.central") == pytest.approx((2340 / 1200) ** (1 / 3) - 1)
    assert fact(report, "s14.crossed.todays_institutional_nodes.central") is None    # far below capacity
    p = tmp_path / "r3.toml"
    p.write_text(CONFIG + "growth = { low = 1.0, central = 2.0, high = 3.0 }\n"
                 "planned = [ { label = \"new nodes\", cores = 100, weight = 1.5 } ]\n"
                 .replace("growth", "growth", 1))
    text = CONFIG.replace("[report.clusters.c1.capacity]\npractical = 0.75",
                          "[report.clusters.c1.capacity]\npractical = 0.75\n"
                          "growth = { low = 1.0, central = 2.0, high = 3.0 }\n"
                          "planned = [ { label = \"new nodes\", cores = 100, weight = 1.5 } ]")
    p.write_text(text)
    report, _ = build(site, ucfg.load(p, "c1"))
    today = 50 * 8760 * 0.75
    k = next(k for k in range(20) if base * 3 ** k > today)
    assert fact(report, "s14.crossed.todays_institutional_nodes.central") == 2026 + k
    assert fact(report, "s14.capacity.new_nodes") == pytest.approx(100 * 1.5 * 8760 * 0.75)
    assert fact(report, "s14.floor_new_cores") == 35       # ceil(50 / 1.45)


def test_not_measured_without_data(tmp_path, cfg):
    db = tmp_path / "empty.db"
    ensure_database(db)
    c = sqlite3.connect(db)
    ensure_job_columns(c)
    J(c, "1", "alice", "short", "cn01", 10, "2026-01-05T08:00:00", "2026-01-05T08:00:00", 10)
    c.commit()
    c.close()
    report, _ = build(db, cfg)
    status = {s.number: s.status for s in report.sections}
    assert status[2] == "not measured" and status[9] == "not measured" and status[12] == "not measured"
    assert status[13] == "not measured"
    md = render.markdown(report)
    assert "Not measured: Slurm's monthly totals" in md
    # Nothing reads as a measured zero.
    sec9 = next(s for s in report.sections if s.number == 9)
    assert not sec9.facts and not sec9.tables


def test_min_cell_hides_small_counts(site, cfg):
    report, _ = build(site, cfg, min_cell=3)
    f = next(f for f in report.facts() if f.id == "s07.gpu_people")
    assert f.suppressed and "fewer than 3" in f.note
    d = report.to_dict()
    assert next(x for x in d["facts"] if x["id"] == "s07.gpu_people")["value"] is None
    md = render.markdown(report)
    assert "fewer than 3" in md
    sec4 = next(s for s in report.sections if s.number == 4)
    assert "fewer than 3 of fewer than 3 people" in sec4.finding
    # Counts at or above the floor stay.
    assert fact(report, "s01.people") == 5


def test_guard_refuses_a_name(site, cfg, tmp_path):
    report, data = build(site, cfg)
    run.write(report, data, tmp_path / "ok", ["md", "json"])          # clean as built
    next(s for s in report.sections if s.number == 3).notes.append("The busiest was frank.")
    with pytest.raises(guard.GuardError) as e:
        run.write(report, data, tmp_path / "bad", ["md"])
    assert "1 usernames" in str(e.value) and "frank" not in str(e.value)
    assert not (tmp_path / "bad").exists() or not list((tmp_path / "bad").iterdir())
    run.guard_details(e.value, tmp_path / "found.txt")
    assert stat.S_IMODE(os.stat(tmp_path / "found.txt").st_mode) == 0o600
    assert "frank" in (tmp_path / "found.txt").read_text()


def test_guard_words_of_the_report_itself():
    names = Names(users={"basic", "zz9q"}, job_names={"zebra crossing", "my run"}, partitions={"labxyz"})
    guard.check(["The basic tier."], names, {"basic"})                 # a tier name, not a leak
    with pytest.raises(guard.GuardError):
        guard.check(["seen zz9q here"], names, set())
    with pytest.raises(guard.GuardError):
        guard.check(["a zebra crossing"], names, set())
    with pytest.raises(guard.GuardError):
        guard.check(["in labxyz."], names, set())
    guard.check(["zz9qa and labxyz2"], names, set())                   # other words that contain a name


def test_user_map(site, cfg, tmp_path):
    m = tmp_path / "map.csv"
    m.write_text("user,department,school,pi\nalice,Chemistry,Arts and Sciences,zpi1\nbob,Physics,Arts and Sciences,zpi2\n")
    report, data = build(site, cfg, user_map=m)
    assert fact(report, "s12.department_people.Chemistry") == 1
    assert fact(report, "s12.school_core_hours.Arts and Sciences") == pytest.approx(210 + 80)
    assert fact(report, "s12.unmapped_people") == 3
    assert "zpi1" in data.names.others                                  # the guard knows the PI column
    run.write(report, data, tmp_path / "out", ["md"])


def test_teaching_server(site, cfg):
    c = sqlite3.connect(site)
    c.execute("CREATE TABLE IF NOT EXISTS interactive_sessions (id INTEGER PRIMARY KEY, timestamp TEXT, server_id TEXT, "
              "user TEXT, session_type TEXT, pid INTEGER, cpu_percent REAL, mem_percent REAL, mem_mb REAL, "
              "mem_virtual_mb REAL, start_time TEXT, age_hours REAL, is_idle INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS interactive_summary (id INTEGER PRIMARY KEY, timestamp TEXT, server_id TEXT, "
              "total_sessions INTEGER, idle_sessions INTEGER, total_memory_mb REAL, unique_users INTEGER, "
              "rstudio_sessions INTEGER, jupyter_python_sessions INTEGER, jupyter_r_sessions INTEGER, "
              "stale_sessions INTEGER, memory_hog_sessions INTEGER)")
    for u, typ, idle in (("s1", "RStudio", 0), ("s2", "RStudio", 1), ("s3", "Jupyter (Python)", 0)):
        c.execute("INSERT INTO interactive_sessions (timestamp, server_id, user, session_type, cpu_percent, mem_mb, "
                  "is_idle) VALUES (?,?,?,?,?,?,?)", ("2026-01-15T10:00:00", "teach", u, typ, 20.0, 2048, idle))
    c.execute("INSERT INTO interactive_summary (timestamp, server_id, total_sessions, total_memory_mb) "
              "VALUES ('2026-01-15T10:00:00', 'teach', 3, 6144)")
    c.commit()
    c.close()
    report, data = build(site, cfg, teaching_site="teach")
    assert fact(report, "s13.people") == 3 and fact(report, "s13.peak_sessions") == 3
    assert fact(report, "s13.idle_share.RStudio") == pytest.approx(0.5)
    run.write(report, data, Path(site).parent / "t", ["md"])        # session users are guarded too


def write_export(path: Path):
    header = ("JobID|User|Group|Account|Partition|JobName|State|NodeList|AllocCPUS|AllocTRES|ReqMem|ReqTRES|"
              "Timelimit|Elapsed|Submit|Start|End|ExitCode|WorkDir")
    rows = [
        ("1", "alice", "short", "job", "COMPLETED", "cn01", 10, "cpu=10", "1-00:00:00", "10:00:00",
         "2026-01-05T08:00:00", "2026-01-05T08:00:00", "2026-01-05T18:00:00"),
        ("3", "bob", "short", "job", "COMPLETED", "cn04", 10, "cpu=10", "1-00:00:00", "08:00:00",
         "2025-12-31T19:00:00", "2025-12-31T20:00:00", "2026-01-01T04:00:00"),
        ("4", "bob", "gpus", "gmx_run", "COMPLETED", "g01", 4, "cpu=4,gres/gpu=2", "1-00:00:00", "10:00:00",
         "2026-02-10T00:00:00", "2026-02-10T00:00:00", "2026-02-10T10:00:00"),
        ("6", "root", "short", "job", "COMPLETED", "cn01", 10, "cpu=10", "1-00:00:00", "01:00:00",
         "2026-01-20T00:00:00", "2026-01-20T00:00:00", "2026-01-20T01:00:00"),
        ("7", "dave", "short", "job", "PENDING", "None assigned", 4, "", "1-00:00:00", "00:00:00",
         "2026-02-20T00:00:00", "Unknown", "Unknown"),
        ("11_[1-5]", "dave", "short", "arr", "PENDING", "None assigned", 1, "", "1-00:00:00", "00:00:00",
         "2026-02-21T00:00:00", "Unknown", "Unknown"),
    ]
    lines = [header]
    for jid, user, part, name, state, nodes, cpus, tres, tl, el, sub, st, en in rows:
        lines.append("|".join([jid, user, "people", "lab", part, name, state, nodes, str(cpus), tres, "4G", "",
                               tl, el, sub, st, en, "0:0", f"/home/{user}/p/r"]))
        if not jid.startswith("11"):
            lines.append("|".join([f"{jid}.batch", "", "", "", "", "batch", state, nodes, str(cpus), "", "", "",
                                   "", el, sub, st, en, "0:0", ""]))
    path.write_text("\n".join(lines) + "\n")
    return path


def test_sacct_export_reads_like_the_database(tmp_path, cfg):
    exp = write_export(tmp_path / "export.psv")
    cfg.cores_per_node = dict.fromkeys(("cn01", "cn02", "cn03", "cn04", "g01", "lab01"), 10)
    cfg.gpus_per_node = {"g01": 4}
    report, data = run.build(cfg, T0, T1, db=None, site=None, sacct=exp)
    assert fact(report, "s01.people") == 3                         # alice, bob, and dave, who waited
    assert fact(report, "s01.institutional_core_hours") == pytest.approx(100 + 40 + 40)
    assert fact(report, "s07.gpu_hours") == pytest.approx(20)
    assert data.excluded_jobs == 1
    # Dave's pending array range is one waiting job.
    assert data.pending_ranges == 1 and data.set_aside["never started"] == 2
    status = {s.number: s.status for s in report.sections}
    assert status[5] == "not measured" and status[2] == "not measured"   # no database: no samples, no totals
    run.write(report, data, tmp_path / "o", ["md", "json"])


def combined(tmp_path, site):
    """A hub's database: the site's tables tagged c1, plus a second site."""
    db = tmp_path / "combined.db"
    src = sqlite3.connect(site)
    src.backup(sqlite3.connect(db))
    src.close()
    c = sqlite3.connect(db)
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        cols = {r[1] for r in c.execute(f'PRAGMA table_info("{t}")')}
        if "source_site" not in cols:
            c.execute(f'ALTER TABLE "{t}" ADD COLUMN source_site TEXT')
        c.execute(f'UPDATE "{t}" SET source_site = ?', ("c1",))
    c.execute("CREATE TABLE jobs2 AS SELECT * FROM jobs WHERE job_id = '1'")
    c.execute("UPDATE jobs2 SET source_site = 'c2', job_id = '501', user_name = 'zed'")
    c.execute("INSERT INTO jobs SELECT * FROM jobs2")
    c.commit()
    c.close()
    return db


def test_hub_database_one_site(tmp_path, site, cfg):
    db = combined(tmp_path, site)
    with pytest.raises(sources.SourceError):
        run.build(cfg, T0, T1, db=db, site=None)
    report, _ = run.build(cfg, T0, T1, db=db, site="c1")
    assert fact(report, "s01.people") == 5
    assert fact(report, "s01.institutional_core_hours") == pytest.approx(294)


def cli(*args):
    from nomad.cli import cli as main
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def test_cli_report(site, tmp_path):
    (tmp_path / "report.toml").write_text(CONFIG)
    out = tmp_path / "out"
    r = cli("usage-report", "--db", str(site), "--config", str(tmp_path / "report.toml"), "--cluster", "c1",
            "--from", "2026-01-01", "--to", "2026-03-01", "--out", str(out))
    assert r.exit_code == 0, r.output
    assert "1. Headline" in r.output and "alice" not in r.output
    md = out / "usage-c1-2026-01-01-to-2026-03-01.md"
    js = out / "usage-c1-2026-01-01-to-2026-03-01.json"
    assert md.exists() and js.exists()
    data = json.loads(js.read_text())
    assert {f["id"]: f["value"] for f in data["facts"]}["s01.people"] == 5
    assert len(data["sections"]) == 14 and data["coverage"] and data["assumptions"]
    for name in ("alice", "bob", "frank", "gmx_run", "zlab", "job_frank"):
        assert name not in md.read_text() and name not in js.read_text()


def test_cli_errors(site, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('[report.clusters.c1]\ntiers = { a = "n[01-02]", b = "n02" }\n')
    r = CliRunner().invoke(__import__("nomad.cli", fromlist=["cli"]).cli,
                           ["usage-report", "--db", str(site), "--config", str(bad), "--out", str(tmp_path)])
    assert r.exit_code != 0 and "report.toml" in r.output
    r = CliRunner().invoke(__import__("nomad.cli", fromlist=["cli"]).cli,
                           ["usage-report", "--db", str(site), "--from", "2026-03-01", "--to", "2026-01-01"])
    assert r.exit_code != 0


def test_cli_init_and_people(site, tmp_path):
    target = tmp_path / "cfg" / "report.toml"
    r = cli("usage-report", "init", "--db", str(site), "--out", str(target))
    assert r.exit_code == 0, r.output
    text = target.read_text()
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert 'basic = "cn[01-04],lab01"' in text and 'gpu = "g01"' in text
    cfg = ucfg.load(target, "c1")                                     # the draft reads
    assert cfg.has_tier_map
    r = cli("usage-report", "init", "--db", str(site), "--out", str(target))
    assert r.exit_code != 0                                            # no overwrite without --force
    csvp = tmp_path / "people.csv"
    r = cli("usage-report", "people", "--db", str(site), "--from", "2026-01-01", "--to", "2026-03-01",
            "--out", str(csvp))
    assert r.exit_code == 0 and "alice" not in r.output
    rows = list(csv.reader(csvp.open()))
    assert rows[0] == ["user", "department", "school"]
    assert sorted(r[0] for r in rows[1:]) == ["alice", "bob", "carol", "dave", "frank", "root"] or \
        sorted(r[0] for r in rows[1:]) == ["alice", "bob", "carol", "dave", "frank"]
    assert stat.S_IMODE(os.stat(csvp).st_mode) == 0o600


def test_compress_hostlist():
    from nomad.usage.draft import compress_hostlist
    assert compress_hostlist(["n01", "n02", "n03", "n05", "g1"]) == "g1,n[01-03,05]"
    assert compress_hostlist(["login"]) == "login"
    assert compress_hostlist(["a9", "a10"]) == "a10,a9"               # different widths stay apart


# --- after the first review -------------------------------------------------------------

def add_job(site, *args, **kw):
    c = sqlite3.connect(site)
    J(c, *args, **kw)
    c.commit()
    c.close()


def test_guard_ignores_ids_dates_and_allows_words(site, cfg, tmp_path):
    # A job named like a fact id, a directory named like a month: not names.
    add_job(site, "20", "alice", "short", "cn01", 1, "2026-01-08T00:00:00", "2026-01-08T00:00:00", 1,
            name="s01", tail="runs/2026-02")
    report, data = build(site, cfg)
    run.write(report, data, tmp_path / "a", ["md", "json"])
    add_job(site, "21", "alice", "short", "cn01", 1, "2026-01-08T00:00:00", "2026-01-08T00:00:00", 1,
            name="rescale")
    report, data = build(site, cfg)
    next(s for s in report.sections if s.number == 8).notes.append("Mostly rescale work.")
    with pytest.raises(guard.GuardError):
        run.write(report, data, tmp_path / "b", ["md"])
    run.write(report, data, tmp_path / "b", ["md"], allow={"rescale"})


def test_filesystem_labels_lose_names(site, cfg, tmp_path):
    c = sqlite3.connect(site)
    for m, used in (("2026-01", 10e12), ("2026-02", 11e12)):
        for path in ("/mnt/zlab", "/home/alice"):
            c.execute("INSERT INTO filesystems (path, total_bytes, used_bytes, available_bytes, used_percent, "
                      "timestamp) VALUES (?,?,?,?,?,?)", (path, 20e12, used, 20e12 - used, 50, f"{m}-15T12:00:00"))
    c.commit()
    c.close()
    report, data = build(site, cfg)
    paths = run.write(report, data, tmp_path / "o", ["md", "json"])
    text = paths[0].read_text() + paths[1].read_text()
    assert "zlab" not in text and "alice" not in text
    assert "/mnt/*" in text and "/home/*" in text and "/scratch" in text


def test_gpu_people_ignore_a_labs_gpus(site, cfg):
    c = sqlite3.connect(site)
    c.execute("UPDATE node_state SET gres = 'gpu:2' WHERE node_name = 'lab01'")
    J(c, "30", "hal", "zlab", "lab01", 4, "2026-01-08T00:00:00", "2026-01-08T00:00:00", 1)
    J(c, "31", "ivy", "zlab", "lab01", 4, "2026-01-08T00:00:00", "2026-01-08T00:00:00", 1)
    c.commit()
    c.close()
    report, _ = build(site, cfg)
    assert fact(report, "s07.gpu_people") == 1 and fact(report, "s07.cards") == 4
    assert fact(report, "s03.people.gpu_partitions_or_requests") == 1


def test_gpu_people_without_a_tier_map(site, tmp_path):
    p = tmp_path / "plain.toml"
    p.write_text("[report]\nexclude_users = [\"root\"]\n")
    report, _ = build(site, ucfg.load(p, "c1"))
    assert fact(report, "s07.gpu_people") == 1                   # bob asked; nobody else did
    assert fact(report, "s07.cards") == 4


def test_gpu_start_detected_from_all_records(site, tmp_path):
    add_job(site, "40", "bob", "gpus", "g01", 4, "2025-11-01T00:00:00", "2025-11-01T00:00:00", 1, gpus=1)
    p = tmp_path / "r.toml"
    p.write_text(CONFIG.replace('gpu_accounting_start = "2026-02-01T00:00:00"\n', ""))
    report, _ = build(site, ucfg.load(p, "c1"))
    # Accounting began before the period: the whole period counts.
    assert fact(report, "s07.allocation_share") == pytest.approx(20 / (4 * 1416))
    sec = next(s for s in report.sections if s.number == 7)
    assert not any("exist only from" in n for n in sec.notes)


def test_gpus_from_the_request_without_an_allocation(site, cfg):
    add_job(site, "41", "bob", "gpus", "g01", 4, "2026-02-20T00:00:00", "2026-02-20T00:00:00", 5, gpus=2,
            tres=None)
    c = sqlite3.connect(site)
    c.execute("UPDATE jobs SET alloc_gpus = NULL WHERE job_id = '41'")
    c.commit()
    c.close()
    report, data = build(site, cfg)
    assert data.gpus_from_request == 1
    assert fact(report, "s07.gpu_hours") == pytest.approx(20 + 10)


def test_multi_year_load_without_consecutive_full_years(site, cfg):
    c = sqlite3.connect(site)
    c.execute("DELETE FROM cluster_usage WHERE month IN ('2023-03', '2025-03')")
    c.commit()
    c.close()
    report, _ = build(site, cfg)
    assert fact(report, "s02.cagr") == pytest.approx((1800 / 1200) ** (1 / 2) - 1)
    assert fact(report, "s14.growth.low") == fact(report, "s14.growth.central")


def test_inventory_leaves_out_retired_nodes(site, tmp_path):
    c = sqlite3.connect(site)
    c.execute("INSERT INTO node_state (timestamp, node_name, cluster, state, cpus_total, cpus_alloc, cpu_load, "
              "memory_total_mb, partitions, gres) VALUES ('2025-06-01T00:00:00', 'g99', 'c1', 'idle', 64, 0, 0, "
              "1024, 'gpus', 'gpu:4')")
    c.commit()
    c.close()
    p = tmp_path / "plain.toml"
    p.write_text("[report]\nexclude_users = [\"root\"]\n")
    report, _ = build(site, ucfg.load(p, "c1"))
    assert fact(report, "s07.cards") == 4
    assert fact(report, "s01.institutional_cores") == 60          # six nodes of 10 cores


def test_assumed_end_in_utc_is_not_an_end(site, cfg):
    c = sqlite3.connect(site)
    c.execute("INSERT INTO jobs (job_id, user_name, partition, node_list, job_name, submit_time, start_time, "
              "end_time, state, req_cpus, runtime_seconds, req_time_seconds) VALUES ('60', 'alice', 'short', "
              "'cn01', 'x', '2026-01-07T07:59:00', '2026-01-07T08:00:00', '2026-01-07 14:00:00', 'COMPLETED', "
              "10, 3300, 86400)")
    c.commit()
    c.close()
    _, data = build(site, cfg)
    j = next(j for j in data.jobs if j.submit == datetime(2026, 1, 7, 7, 59))
    assert j.start == datetime(2026, 1, 7, 8, 0) and j.end == datetime(2026, 1, 7, 8, 55) and not j.ended
    assert data.set_aside["start corrected"] == 1                # job 10 only


def test_daylight_saving_and_unknown_ends(site, cfg):
    add_job(site, "70", "alice", "short", "cn01", 1, "2025-11-01T23:00:00", "2025-11-01T23:30:00", 5,
            end="2025-11-02T03:30:00")                           # 5 hours across the change of hour
    add_job(site, "71", "alice", "short", "cn01", 1, "2026-01-09T00:00:00", "2026-01-09T00:00:00", 1,
            state="UNKNOWN", end="2026-01-12T00:00:00")           # noticed gone three days later
    report, data = run.build(cfg, datetime(2025, 11, 1), T1, db=site, site=None)
    j70 = next(j for j in data.jobs if j.submit == datetime(2025, 11, 1, 23))
    assert j70.start == datetime(2025, 11, 1, 23, 30)
    j71 = next(j for j in data.jobs if j.submit == datetime(2026, 1, 9))
    assert j71.end == datetime(2026, 1, 9, 1) and not j71.ended
    assert data.set_aside["start corrected"] == 1


def test_stale_waiting_rows_and_late_starters(site, cfg):
    add_job(site, "80", "gina", "short", None, 4, "2025-06-01T00:00:00", None, 0, state="PENDING")
    # Submitted in February, started after the period: it waited in February.
    add_job(site, "81", "hank", "short", "cn01", 10, "2026-02-20T00:00:00", "2026-03-05T00:00:00", 2)
    report, data = build(site, cfg)
    assert fact(report, "s01.people") == 5                       # neither gina nor hank
    assert fact(report, "s04.people_waited.2026-02") == 1 and fact(report, "s04.people.2026-02") == 1
    assert fact(report, "s04.waiting_core_hours.2026-02") == pytest.approx(20)


def test_user_map_with_a_byte_order_mark(site, cfg, tmp_path):
    m = tmp_path / "map.csv"
    m.write_bytes(b"\xef\xbb\xbfuser,department,school\nalice,Chemistry,Arts\n")
    report, _ = build(site, cfg, user_map=m)
    assert fact(report, "s12.department_people.Chemistry") == 1


def test_config_offset_times_and_held_vs_used_weighting(site, tmp_path):
    p = tmp_path / "r.toml"
    p.write_text(CONFIG.replace('"2026-02-01T00:00:00"', '"2026-02-01T05:00:00Z"'))
    cfg = ucfg.load(p, "c1")
    assert cfg.gpu_accounting_start == datetime(2026, 2, 1)       # 05:00 UTC is midnight in New York
    report, _ = build(site, cfg)
    # Four basic nodes at 50% and one GPU node at 100%, sample for sample.
    assert fact(report, "s05.allocation.institutional") == pytest.approx(0.6)


def test_cli_wrong_cluster_and_bad_export(site, tmp_path):
    (tmp_path / "report.toml").write_text(CONFIG)
    from nomad.cli import cli as main
    r = CliRunner().invoke(main, ["usage-report", "--db", str(site), "--config", str(tmp_path / "report.toml"),
                                  "--cluster", "c9", "--out", str(tmp_path)])
    assert r.exit_code != 0 and "has no [report.clusters.c9]" in r.output and "c1" in r.output
    bad = tmp_path / "bad.psv"
    bad.write_text("1|alice|short\n")
    r = CliRunner().invoke(main, ["usage-report", "--sacct", str(bad), "--db", str(site), "--config",
                                  str(tmp_path / "report.toml"), "--cluster", "c1", "--out", str(tmp_path)])
    assert r.exit_code == 1 and "header with JobID" in r.output
    assert isinstance(r.exception, SystemExit)                     # a message, not a traceback


def test_private_files(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep me\n")
    link = tmp_path / "link"
    link.symlink_to(victim)
    with pytest.raises(OSError):
        run.private_file(link, overwrite=True)
    with pytest.raises(FileExistsError):
        run.private_file(victim)
    assert victim.read_text() == "keep me\n"


def test_export_metrics_need_the_same_job(site, cfg, tmp_path):
    exp = write_export(tmp_path / "export.psv")
    c = sqlite3.connect(site)
    c.execute("UPDATE jobs SET submit_time = '2025-01-01T00:00:00' WHERE job_id = '3'")   # another job 3
    c.commit()
    c.close()
    report, data = run.build(cfg, T0, T1, db=site, site=None, sacct=exp)
    by_submit = {j.submit: j for j in data.jobs}
    assert by_submit[datetime(2026, 1, 5, 8)].cpu_pct == 50.0      # job 1: same number, same submission
    assert by_submit[datetime(2025, 12, 31, 19)].cpu_pct is None   # job 3: the database's is another job


# --- after the second review ------------------------------------------------------------

def test_redacted_labels_stay_apart(site, cfg, tmp_path):
    c = sqlite3.connect(site)
    J(c, "90", "ylab", "short", "cn01", 1, "2026-01-08T00:00:00", "2026-01-08T00:00:00", 1)   # a user 'ylab'
    for m, a, b in (("2026-01", 50e12, 10e12), ("2026-02", 52e12, 11e12)):
        for path, used in (("/mnt/zlab", a), ("/mnt/ylab", b), ("/mnt/zlab-nas", b), ("/home/alice_old", b)):
            c.execute("INSERT INTO filesystems (path, total_bytes, used_bytes, available_bytes, used_percent, "
                      "timestamp) VALUES (?,?,?,?,?,?)", (path, 100e12, used, 100e12 - used, 50, f"{m}-15T12:00:00"))
    c.commit()
    c.close()
    report, data = build(site, cfg)
    labels = set(data.fs_labels.values())
    assert {"/mnt/* (1)", "/mnt/* (2)", "/mnt/*-nas", "/home/*_old"} <= labels
    # Each keeps its own series: no cleanup appears from mixing them.
    grow = {f.id: f.value for f in report.facts() if f.id.startswith("s09.growth_per_month./mnt/* (")}
    assert sorted(grow.values()) == pytest.approx([1e12, 2e12])
    paths = run.write(report, data, tmp_path / "o", ["md", "json"])
    text = "".join(p.read_text() for p in paths)
    for name in ("zlab", "ylab", "alice"):
        assert name not in text


def test_guard_finds_names_joined_to_words():
    names = Names(users={"alice"}, partitions={"zlab"})
    with pytest.raises(guard.GuardError):
        guard.check(["kept in /home/alice_old"], names, set())
    with pytest.raises(guard.GuardError):
        guard.check(["the zlab-nas mount"], names, set())
    guard.check(["core-hours of all nodes"], Names(users={"core", "hours"}), set())   # the report's own words


def test_department_labels(site, cfg, tmp_path):
    m = tmp_path / "map.csv"
    # A Unix group is named like a department (common); a department is named like a user (not allowed).
    m.write_text("user,department,school\nalice,people,Sciences\nbob,Physics,Sciences\n")
    report, data = build(site, cfg, user_map=m)
    assert fact(report, "s12.department_people.people") == 1 and fact(report, "s12.department_people.Physics") == 1
    run.write(report, data, tmp_path / "ok", ["md", "json"])
    m.write_text("user,department,school\nalice,frank,Sciences\n")
    report, data = build(site, cfg, user_map=m)
    with pytest.raises(guard.GuardError):
        run.write(report, data, tmp_path / "no", ["md"])
    run.write(report, data, tmp_path / "no", ["md"], allow={"frank"})


def test_node_days_over_the_samples_only(site, cfg):
    # Samples begin on 1 Dec 2025: a report from October doesn't stretch them.
    report, _ = run.build(cfg, datetime(2025, 10, 1), T1, db=site, site=None)
    assert fact(report, "s10.node_days_down.basic") == pytest.approx(10, abs=1.05)
    sec = next(s for s in report.sections if s.number == 10)
    assert any("node samples cover" in t.note for t in sec.tables)


def test_storage_already_full(site, cfg):
    c = sqlite3.connect(site)
    for m, used in (("2026-01", 15.0e12), ("2026-02", 16.5e12)):
        c.execute("INSERT INTO filesystems (path, total_bytes, used_bytes, available_bytes, used_percent, timestamp) "
                  "VALUES (?,?,?,?,?,?)", ("/home", 16.5e12, used, 16.5e12 - used, 100, f"{m}-15T12:00:00"))
    c.commit()
    c.close()
    report, _ = build(site, cfg)
    sec = next(s for s in report.sections if s.number == 9)
    assert sec.finding.startswith("/home is 100% full (15 Feb 2026); /scratch has grown 10.0 TB a month")
    assert "0.0 months" not in render.markdown(report)


def test_storage_spike_then_cleanup(site, cfg):
    c = sqlite3.connect(site)
    for t, used in (("2026-01-15T12:00:00", 12.0e12), ("2026-02-10T12:00:00", 16.5e12),
                    ("2026-02-20T12:00:00", 12.7e12)):
        c.execute("INSERT INTO filesystems (path, total_bytes, used_bytes, available_bytes, used_percent, timestamp) "
                  "VALUES (?,?,?,?,?,?)", ("/home", 16.5e12, used, 16.5e12 - used, used / 16.5e12 * 100, t))
    c.commit()
    c.close()
    report, _ = build(site, cfg)
    sec = next(s for s in report.sections if s.number == 9)
    assert sec.finding.startswith("/home reached 100% full in Feb 2026 and was at 77% on 20 Feb 2026")
    assert fact(report, "s09.used_share./home") == pytest.approx(12.7 / 16.5)
    # Growth from the monthly highest (4.5 TB a month), the time to full from the last reading.
    assert fact(report, "s09.months_to_full./home") == pytest.approx((16.5 - 12.7) / 4.5)


def test_several_classes_are_node_classes(site, cfg):
    add_job(site, "95", "alice", "all", "cn01", 1, "2026-01-08T00:00:00", "2026-01-08T00:00:00", 1)
    report, _ = build(site, cfg)
    assert fact(report, "s03.people_several_classes") == 1        # bob (basic and GPU); alice's overlay doesn't count


def test_gpu_requests_before_accounting(site, cfg):
    add_job(site, "96", "ivy", "all", "cn01", 1, "2026-01-08T00:00:00", "2026-01-08T00:00:00", 1, gpus=0)
    c = sqlite3.connect(site)
    c.execute("UPDATE jobs SET req_gpus = 1 WHERE job_id = '96'")
    c.commit()
    c.close()
    report, _ = build(site, cfg)
    assert fact(report, "s07.gpu_people") == 2
    assert fact(report, "s07.gpu_people_asking_before_accounting") == 1
    sec = next(s for s in report.sections if s.number == 7)
    assert any("not in Slurm's accounting" in n for n in sec.notes)
