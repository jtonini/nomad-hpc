"""1.25.4: switches written as text; nodes that fail every job at start;
when a job would start in each partition (Slurm's own estimate)."""
from click.testing import CliRunner

try:
    import tomllib
except ImportError:                     # Python 3.10
    import toml as tomllib

from nomad.config.problems import quoted_switches

# ── switches written as text ────────────────────────────────────────────────

TOML = '''[hub]
gate_key = "x"

[[hub.sites]]
name = "spydur"
submit = "gate"
edit = "true"

[[hub.sites]]
name = "arachne"
edit = "False"   # a comment

[collectors.nfs]
enabled = "false"

[console]
password = "true"
ok = true
label = "truly"
'''


def test_switches_written_as_text_are_said_with_the_fix():
    got = quoted_switches(tomllib.loads(TOML), TOML)
    assert got == [
        'line 7, [[hub.sites]] (spydur): edit = "true" is text, not a switch: write edit = true (no quotes)',
        'line 11, [[hub.sites]] (arachne): edit = "False" is text, not a switch: write edit = false (no quotes)',
        'line 14, [collectors.nfs]: enabled = "false" is text, not a switch: write enabled = false (no quotes)',
    ]                                                   # password: a secret's key, left alone
    assert quoted_switches(tomllib.loads(TOML)) and all(not g.startswith("line") for g in quoted_switches(
        tomllib.loads(TOML)))
    assert quoted_switches({"a": {"b": True, "c": "yes"}}) == []
    # the top level, a table in a table, one in a [[...]] entry, an inline table (review: named right)
    odd = 'debug = "true"\n[a.b.c]\nx = "false"\n[[hub.sites]]\nname = "s1"\n[hub.sites.opts]\ny = "TRUE"\n' \
          '[k]\nz = { w = "true" }\n'
    assert [g.split(" is text")[0] for g in quoted_switches(tomllib.loads(odd), odd)] == [
        'line 1, the top level: debug = "true"', 'line 3, [a.b.c]: x = "false"',
        'line 7, [hub.sites.opts] of [[hub.sites]] (s1): y = "TRUE"', '[k.z]: w = "true"']


def test_config_check_says_them(tmp_path):
    from nomad.cli import cli
    p = tmp_path / "nomad.toml"
    p.write_text(TOML)
    r = CliRunner().invoke(cli, ["-c", str(p), "config", "check"])
    assert r.exit_code == 0, r.output
    assert 'line 7, [[hub.sites]] (spydur): edit = "true" is text' in r.output


# ── a node that fails every job at start ────────────────────────────────────

import sqlite3  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

from nomad.alerts import node_jobs  # noqa: E402

NOW = datetime(2026, 10, 29, 16, 40)


def _jobs(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, user_name TEXT, node_list TEXT, start_time TEXT, "
                 "end_time TEXT, state TEXT, runtime_seconds INTEGER)")
    conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def _vadur():
    """29 Oct: node03 back after a Slurm reinstall fails 16 tasks in six
    minutes; the same array runs for hours on node04 and node06."""
    rows = []
    for i in range(16):
        t = datetime(2026, 10, 29, 16, 29, 12) + timedelta(seconds=22 * i)
        rows.append((f"12271_{56 + i}", "jgyasi", "node03", t.isoformat(), (t + timedelta(seconds=4)).isoformat(),
                     "FAILED", 4))
    rows.append(("12271_80", "jgyasi", "node04", "2026-10-29T16:05:00", None, "RUNNING", None))
    rows.append(("12271_81", "jgyasi", "node06", "2026-10-29 15:50:00", None, "COMPLETED", 2400))
    rows.append(("12270_1", "amy", "node[01-02]", "2026-10-29T16:30:00", None, "FAILED", 2))   # two nodes: not one's
    rows.append(("12000_1", "jgyasi", "node03", "2026-10-29T12:00:00", None, "FAILED", 3))     # before the window
    return rows


def test_a_node_failing_every_job_at_start_is_found():
    got = node_jobs.failing_nodes(_jobs(_vadur()), NOW)
    assert [(f["node"], f["failed"], f["started"], f["people"]) for f in got] == [("node03", 16, 16, 1)]
    f = got[0]
    assert f["first"] == datetime(2026, 10, 29, 16, 29, 12) and f["jobs"][0] == "12271_56"
    text = node_jobs.message(f, 60)
    assert "node03 failed 16 of the 16 jobs" in text and "between 16:29 and 16:34" in text
    assert "jgyasi" not in text and "scontrol update nodename=node03 state=drain" in text
    # (review 3) one work only: it may be those tasks (an array's last ones without input) -- look first
    assert "or those very tasks" in text and "Look at job 12271_56's output first" in text
    assert "likely the node" in node_jobs.message({**f, "works": 3}, 60)


def test_not_the_node_when_the_job_fails_everywhere_or_rarely():
    # the same people's jobs failed on every node too: it is the job
    rows = [r for r in _vadur() if r[0] not in ("12271_80", "12271_81")]
    assert node_jobs.failing_nodes(_jobs(rows), NOW) == []
    # a few fast failures among many that ran: not every job
    rows = _vadur()[:4] + [(f"9_{i}", "jgyasi", "node03", "2026-10-29T16:10:00", None, "COMPLETED", 900)
                          for i in range(10)] + _vadur()[16:18]
    assert node_jobs.failing_nodes(_jobs(rows), NOW) == []
    # drained already: someone is on it
    assert node_jobs.failing_nodes(_jobs(_vadur()), NOW, skip={"node03"}) == []
    # an old window, and a table without the columns
    assert node_jobs.failing_nodes(_jobs(_vadur()), NOW + timedelta(hours=3)) == []
    assert node_jobs.failing_nodes(sqlite3.connect(":memory:"), NOW) == []


def test_not_the_node_when_only_other_work_of_theirs_ran():
    # (1.25.4's review) a broken array that landed on the free node, while another job of the person's
    # ran elsewhere: the person's other work proves nothing about the node
    rows = [r for r in _vadur() if r[0] not in ("12271_80", "12271_81")]
    rows.append(("12300", "jgyasi", "node04", "2026-10-29T15:00:00", None, "RUNNING", None))
    assert node_jobs.failing_nodes(_jobs(rows), NOW) == []
    # the array's other task started elsewhere seconds ago: not yet "ran well"
    rows = [r for r in _vadur() if r[0] not in ("12271_80", "12271_81")]
    rows.append(("12271_90", "jgyasi", "node04", (NOW - timedelta(seconds=20)).isoformat(), None, "RUNNING", None))
    assert node_jobs.failing_nodes(_jobs(rows), NOW) == []
    # the array failing fast on other nodes too (more than a quarter): the array, not the node
    rows = _vadur() + [(f"12271_{100 + i}", "jgyasi", "node06", "2026-10-29T16:20:00", None, "FAILED", 3)
                       for i in range(3)]
    assert node_jobs.failing_nodes(_jobs(rows), NOW) == []


def test_jobs_of_one_name_count_as_the_same_work():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, user_name TEXT, node_list TEXT, start_time TEXT, "
                 "end_time TEXT, state TEXT, runtime_seconds INTEGER, job_name TEXT)")
    rows = [(str(500 + i), "amy", "node03", f"2026-10-29T16:{10 + i:02d}:00", None, "FAILED", 2, "relax")
            for i in range(6)]
    rows.append(("499", "amy", "node05", "2026-10-29T16:00:00", None, "RUNNING", None, "relax"))
    conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)", rows)
    got = node_jobs.failing_nodes(conn, NOW)
    assert [(f["node"], f["failed"], f["shown"]) for f in got] == [("node03", 6, 6)]
    conn.execute("UPDATE jobs SET job_name = 'other' WHERE job_id = '499'")       # other work: not shown
    assert node_jobs.failing_nodes(conn, NOW) == []


def test_the_jobs_it_reads_are_found_by_an_index(tmp_path):
    # (review 3) each minute, the last hour's starts out of a jobs table kept for years
    from nomad.db import ensure_database
    db = tmp_path / "site.db"
    ensure_database(db)
    conn = sqlite3.connect(db)
    plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN SELECT job_id FROM jobs WHERE start_time >= ?",
                                               ("2026-10-29",)))
    assert "idx_jobs_start" in plan
    from nomad.db.migrations import _jobs_start_index
    _jobs_start_index(sqlite3.connect(":memory:"))         # no jobs table: nothing, no error


def test_its_settings():
    assert node_jobs.settings({}) == node_jobs.DEFAULTS
    cfg = node_jobs.settings({"alerts": {"node_jobs": {"enabled": False, "min_jobs": 3, "share": 2,
                                                       "window_minutes": "30", "fail_fast_seconds": True}}})
    assert cfg == {**node_jobs.DEFAULTS, "enabled": False, "min_jobs": 3, "window_minutes": 30}


def test_the_collector_raises_it_once_per_node(tmp_path, monkeypatch):
    from nomad.collectors import node_state
    db = tmp_path / "site.db"
    conn = _jobs(_vadur())
    disk = sqlite3.connect(db)
    conn.backup(disk)
    disk.close()
    sent = []
    monkeypatch.setattr(node_state, "send_alert", lambda **k: sent.append(k) or {})
    c = node_state.NodeStateCollector({"cluster_name": "vadur"}, str(db))
    c._dispatch_job_alerts([{"node_name": "node03", "state": "IDLE"}], now=NOW)
    assert [(s["source"], s["host"], s["severity"]) for s in sent] == [("node_jobs", "node03", "warning")]
    assert sent[0]["details"]["failed"] == 16 and sent[0]["details"]["cluster"] == "vadur"
    sent.clear()
    c._dispatch_job_alerts([{"node_name": "node03", "state": "DRAINED"}], now=NOW)
    assert sent == []


# ── when a job would start, in each partition (sbatch --test-only) ──────────

import hashlib  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402

from tests.test_release_1182 import Sent, scratch  # noqa: F401,E402  (fixture)


def test_starts_asks_slurm_for_each_partition_and_submits_nothing(scratch, monkeypatch):  # noqa: F811
    import nomad.submit.cli as sc
    tmp, w, sent, slurm = scratch
    plans = {"basic": "2026-11-03T04:10:00", "medium": "2026-11-02T22:40:30", "parish": None}

    def sbatch(argv, cwd, data=None):
        sent.append(Sent(argv, data))
        part = next(a.split("=", 1)[1] for a in argv if a.startswith("--partition="))
        if plans[part] is None:
            return subprocess.CompletedProcess(argv, 1, "", "sbatch: error: Batch job submission failed: "
                                                            "Invalid account or account/partition combination")
        return subprocess.CompletedProcess(argv, 0, "", f"sbatch: Job 4243 to start at {plans[part]} using 4 "
                                                        f"processors on nodes spdr09 in partition {part}\n")
    monkeypatch.setattr(sc, "_test_only", sbatch)
    monkeypatch.setattr(sc, "_node_states", lambda names: {"medium": {"idle": 2, "mixed": 3}})
    sha = hashlib.sha256((w / "job.sh").read_bytes()).hexdigest()
    r = CliRunner().invoke(sc.submit_cmd, ["--via", "console", "--script-sha256", sha, "--starts",
                                           "basic,medium,parish", "--cpus", "2", "job.sh"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output.strip().splitlines()[-1])
    by = {s["partition"]: s for s in doc["starts"]}
    assert by["basic"]["start"] == "2026-11-03T04:10:00" and by["medium"]["nodes"] == "spdr09"
    assert "Invalid account" in by["parish"]["error"] and by["parish"]["start"] is None
    assert doc["nodes"] == {"medium": {"idle": 2, "mixed": 3}}
    assert len(sent) == 3
    for argv in sent:
        assert argv[1] == "--test-only" and "--parsable" not in argv
        assert not any(a.startswith(("--begin", "--extra")) for a in argv)          # no delay, no tag
        assert sum(a.startswith("--partition=") for a in argv) == 1
        assert "--cpus-per-task=2" in argv and argv.data == (w / "job.sh").read_bytes()
    assert not (tmp / "scratch" / "amy" / "jobs").exists()                           # nothing was submitted
    r = CliRunner().invoke(sc.submit_cmd, ["--via", "console", "--script-sha256", sha, "--starts", "a;b",
                                           "job.sh"])
    assert r.exit_code != 0 and "partition names" in r.output


def test_test_only_runs_with_iso_times_and_a_time_limit(scratch, monkeypatch):  # noqa: F811
    import time as _time
    import nomad.submit.cli as sc
    seen = []

    def fake_run(argv, **kw):
        seen.append((kw["env"].get("SLURM_TIME_FORMAT"), kw["timeout"], kw["input"]))
        return subprocess.CompletedProcess(argv, 0, b"", b"sbatch: Job 1 to start at 2026-11-02T22:41:00 ...")
    real = sc.subprocess.run
    monkeypatch.setenv("SLURM_TIME_FORMAT", "%d/%m %H:%M")                 # the person's own: not for nomad
    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    sc._test_only(["sbatch", "--test-only"], "/tmp", b"#!/bin/bash\n")
    assert seen == [("standard", sc.STARTS_TIMEOUT, b"#!/bin/bash\n")]
    monkeypatch.setattr(sc.subprocess, "run", real)
    # four at a time, in the order asked; one that never answers says so and holds up no other
    tmp, w, sent, slurm = scratch
    pl = sc.plan("job.sh")
    monkeypatch.setattr(sc, "_node_states", lambda names: {})
    busy, most = [], []

    def slow(argv, cwd, data=None):
        part = next(a.split("=", 1)[1] for a in argv if a.startswith("--partition="))
        busy.append(part)
        most.append(len(busy))
        _time.sleep(0.05)
        busy.remove(part)
        if part == "stuck":
            raise subprocess.TimeoutExpired(argv, sc.STARTS_TIMEOUT)
        return subprocess.CompletedProcess(argv, 0, "", f"sbatch: Job 1 to start at 2026-11-02T22:41:00 using 1 "
                                                        f"processors on nodes n1 in partition {part}")
    names = ["a", "stuck", "c", "d", "e", "f", "g", "h"]
    doc = sc.start_times(pl, [], ",".join(names), run=slow)
    assert [x["partition"] for x in doc["starts"]] == names and max(most) <= sc.STARTS_AT_ONCE
    assert "didn't answer in 60 s" in doc["starts"][1]["error"] and doc["starts"][2]["start"]


def test_starts_soon_is_within_two_minutes(scratch, monkeypatch):  # noqa: F811
    from datetime import datetime
    import nomad.submit.cli as sc
    tmp, w, sent, slurm = scratch
    now = datetime(2026, 11, 2, 22, 40)
    pl = sc.plan("job.sh")
    outs = iter(["sbatch: Job 1 to start at 2026-11-02T22:41:00 using 4 processors on nodes spdr01 in partition basic",
                 "sbatch: Job 1 to start at 2026-11-02T23:41:00 using 4 processors on nodes spdr09 in partition medium"])
    monkeypatch.setattr(sc, "_node_states", lambda names: {})
    doc = sc.start_times(pl, [], "basic,medium", now=now,
                         run=lambda argv, cwd, data=None: subprocess.CompletedProcess(argv, 0, "", next(outs)))
    assert [(s["partition"], s["soon"]) for s in doc["starts"]] == [("basic", True), ("medium", False)]
    # (review 3) the site's job_submit sent it to another partition: not an estimate for the one asked
    outs = iter(["sbatch: Job 1 to start at 2026-11-02T22:41:00 using 4 processors on nodes spdr01 in partition basic"])
    doc = sc.start_times(pl, [], "condos", now=now,
                         run=lambda argv, cwd, data=None: subprocess.CompletedProcess(argv, 0, "", next(outs)))
    assert doc["starts"][0]["error"] == "Slurm would run it in partition basic, not condos"
    assert doc["starts"][0]["start"] is None


# ── where a job starts soonest: spydur's layout ─────────────────────────────

from nomad.submit import where  # noqa: E402

SPYDUR = {"all": "spdr[01-15]", "all_15tb": "spdr[14-15]", "all_768": "spdr[09-13]", "basic": "spdr[01-08]",
          "bukach": "spdr50", "communitynodes": "spdr[01-15]", "condos": "spdr[50-61]", "cpunodes": "spdr[01-15]",
          "dias": "spdr[51-52]", "erickson": "spdr53", "gpunodes": "spdr[16-18]", "johnson": "spdr54",
          "large": "spdr[14-15]", "luckythirteen": "spdr13", "medium": "spdr[09-13]", "ml": "spdr16",
          "parish": "spdr[55-58]", "sci": "spdr[17-18]", "yang1": "spdr59", "yang2": "spdr60", "yangnolin": "spdr61"}
OWNERS = {"bukach": "cbukach", "dias": "mrebello", "erickson": "perickso", "johnson": "mj6im",
          "parish": "cparish", "yang1": "myang", "yang2": "myang", "yangnolin": "myang"}


def _rows():
    return [{"partition": p, "nodes": n, "is_default": p == "basic"} for p, n in SPYDUR.items()]


def _specs():
    from nomad.hostlist import expand_hostlist
    out = {}
    for n in expand_hostlist("spdr[01-08]"):
        out[n] = (384000, 0)
    for n in expand_hostlist("spdr[09-13]"):
        out[n] = (768000, 0)
    for n in expand_hostlist("spdr[14-15]"):
        out[n] = (1536000, 0)
    for n in expand_hostlist("spdr[16-18]"):
        out[n] = (384000, 4)
    for n in expand_hostlist("spdr[50-61]"):
        out[n] = (384000, 0)
    return out


def _condos(mine=()):
    from nomad.hostlist import expand_hostlist
    return [(c, expand_hostlist(SPYDUR[c]), c in mine) for c in OWNERS]


def test_spydurs_partitions_sorted():
    parts = {p.name: p for p in where.partitions(_rows(), _specs(), _condos())}
    kinds = {k: sorted(n for n, p in parts.items() if p.kind == k) for k in ("small", "big", "gpu", "condo", "mixed")}
    assert kinds["small"] == ["basic"]
    assert kinds["big"] == ["large", "medium"]                      # luckythirteen: inside medium
    assert kinds["gpu"] == ["gpunodes"]                             # ml, sci: inside it
    assert kinds["mixed"] == ["cpunodes"]                           # every shared CPU node: not suggested
    assert set(kinds["condo"]) == {"condos", *OWNERS}
    assert sorted(parts["cpunodes"].aliases) == ["all", "communitynodes"]
    assert parts["medium"].aliases == ["all_768"] and parts["large"].aliases == ["all_15tb"]
    assert parts["condos"].condos == sorted(OWNERS) and not parts["condos"].mine


def test_the_order_for_a_small_job_a_member_and_a_big_one():
    cfg = where.Settings()
    parts = where.partitions(_rows(), _specs(), _condos())
    order = lambda job, ps=parts: [(c["partition"], c["role"], c["allowed"]) for c in where.candidates(ps, job, cfg)]  # noqa: E731
    # a small job of 4 h: basic, then the condos as a guest, then the big nodes last
    assert order(where.Job(mem_mb=8000, time_s=4 * 3600)) == [
        ("basic", "shared", True), ("condos", "guest", True), ("medium", "overflow", True),
        ("large", "overflow", True)]
    # no time limit: neither a guest condo nor overflow, and why
    rows = where.candidates(parts, where.Job(mem_mb=8000), cfg)
    assert [(r["partition"], r["allowed"]) for r in rows] == [("basic", True), ("condos", False), ("medium", False),
                                                             ("large", False)]
    assert "time limit" in rows[1]["why"] and "24 hours" in rows[2]["why"]
    # 3 days: a guest condo yes (10 days), the big nodes no (24 h)
    assert order(where.Job(mem_mb=8000, time_s=3 * 86400))[1:] == [
        ("condos", "guest", True), ("medium", "overflow", False), ("large", "overflow", False)]
    # a Parish lab member: their condo first
    mine = where.partitions(_rows(), _specs(), _condos(mine={"parish"}))
    assert order(where.Job(mem_mb=8000, time_s=3600), mine)[:2] == [("parish", "yours", True), ("basic", "shared", True)]
    # a job that needs 700 GB: the big nodes are its own, not overflow
    assert order(where.Job(mem_mb=700000, time_s=5 * 86400)) == [("medium", "shared", True), ("large", "shared", True)]
    # GPUs: the GPU nodes
    assert order(where.Job(mem_mb=8000, time_s=3600, gpus=1)) == [("gpunodes", "shared", True)]
    # a site that keeps its condos for their labs
    assert [r["partition"] for r in where.candidates(parts, where.Job(mem_mb=8000, time_s=3600),
                                                     where.Settings(guests=False))] == ["basic", "medium", "large"]


def test_ranked_with_slurms_estimates_and_the_reserve():
    cfg = where.Settings()
    parts = where.partitions(_rows(), _specs(), _condos())
    cands = where.candidates(parts, where.Job(mem_mb=8000, time_s=4 * 3600), cfg)
    starts = {"ok": True, "starts": [
        {"partition": "basic", "start": "2026-11-03T04:00:00", "soon": False},
        {"partition": "condos", "start": "2026-11-02T22:41:00", "soon": True},
        {"partition": "medium", "start": "2026-11-02T22:41:00", "soon": True},
        {"partition": "large", "start": "2026-11-02T22:41:00", "soon": True}],
        "nodes": {"medium": {"idle": 3, "mixed": 2}, "large": {"idle": 1, "allocated": 1}}}
    out = where.rank(cands, starts, cfg, {"basic": 7200})
    by = {r["partition"]: r for r in out["rows"]}
    assert out["recommended"] == "condos" and by["condos"]["recommended"]
    assert out["fastest"] == "medium"                               # not a guest condo by itself
    assert not by["large"]["allowed"] and "1 stays free" in by["large"]["why"]   # the reserve
    assert by["basic"]["usual_wait_s"] == 7200
    # a site that lets "Fastest start" take a condo too
    assert where.rank(cands, starts, where.Settings(fastest_guests=True))["fastest"] == "condos"
    # nothing starts soon: the earliest; Slurm refusing one: not suggested, and why
    late = {"ok": True, "starts": [{"partition": "basic", "start": "2026-11-03T04:00:00"},
                                   {"partition": "condos", "error": "Invalid account"},
                                   {"partition": "medium", "start": "2026-11-03T02:00:00"}],
            "nodes": {"medium": {"idle": 2}}}
    out = where.rank(cands, late, cfg)
    assert out["recommended"] == "medium" and {r["partition"]: r["why"] for r in out["rows"]}["condos"] == \
        "Slurm: Invalid account"
    # the big nodes' states unknown (sinfo said nothing): no overflow; none idle (no "idle" count): none either
    by = {r["partition"]: r for r in where.rank(cands, {**late, "nodes": {}}, cfg)["rows"]}
    assert not by["medium"]["allowed"] and "can't tell how many" in by["medium"]["why"]
    by = {r["partition"]: r for r in where.rank(cands, {**late, "nodes": {"medium": {"mixed": 5}}}, cfg)["rows"]}
    assert not by["medium"]["allowed"] and "only 0 of its nodes are idle" in by["medium"]["why"]


def test_a_start_a_year_away_is_later_not_a_date_to_rank_by():
    # where jobs have no time limits Slurm puts the start where it can't see: a year or so on
    cfg = where.Settings()
    parts = where.partitions(_rows(), _specs(), _condos())
    cands = where.candidates(parts, where.Job(mem_mb=8000, time_s=4 * 3600), cfg)
    starts = {"ok": True, "now": "2026-11-02T22:40:00", "nodes": {"medium": {"idle": 0}, "large": {"idle": 0}},
              "starts": [{"partition": "basic", "start": "2027-11-02T22:40:00"},
                         {"partition": "condos", "start": "2027-10-30T09:00:00"},
                         {"partition": "medium", "start": "2026-11-03T01:00:00"}]}
    out = where.rank(cands, starts, cfg, {"condos": 600, "basic": 3600})
    by = {r["partition"]: r for r in out["rows"]}
    assert by["basic"]["later"] and by["condos"]["later"] and not by["medium"]["later"]
    # medium is refused (no idle node), so nothing known: the shortest usual wait
    assert out["recommended"] == "condos" and out["fastest"] == "basic"     # Fastest: never a guest by itself
    # nothing known at all: the site's first, and "Fastest start" takes nothing
    out = where.rank(cands, starts, cfg)
    assert out["recommended"] == "basic" and out["fastest"] is None


def test_settings_from_the_hub():
    cfg = where.settings({"submit": {"where": {"overflow_max_hours": 12, "reserve_nodes": 2, "guests": "yes",
                                               "spydur": {"fastest_guests": True, "reserve_nodes": -1}}}},
                         "spydur", policy_guest_days=7)
    assert (cfg.overflow_max_hours, cfg.reserve_nodes, cfg.guests, cfg.fastest_guests, cfg.guest_max_days) == \
        (12.0, 2, True, True, 7.0)


def test_whose_condo(tmp_path):
    from nomad.condo import config as ccfg
    report = ccfg.from_dict({"report": {"clusters": {"spydur": {"condos": {
        "parish": {"owner": "cparish", "partition": "parish"},
        "yangnolin": {"owner": ["myang", "knolin"], "partition": "yangnolin", "members": "group:nolinlab"},
        "draft": {"owner": "x", "partition": "dias", "confirmed": False}}}}}})
    people = {"parish": {"amy", "cparish"}, "yangnolin": {"bo", "myang", "knolin"}, "draft": {"x"}}
    got = where.my_condos(report, "spydur", "amy", _rows(), lambda c: people[c.name])
    # one not confirmed yet: still a condo's nodes (never shared), never anyone's own
    assert [(c, len(n), m) for c, n, m in got] == [("parish", 4, True), ("yangnolin", 1, False), ("draft", 2, False)]
    got = where.my_condos(report, "spydur", "BO", _rows(), lambda c: people[c.name])
    assert [m for _c, _n, m in got] == [False, True, False]
    assert where.my_condos(report, "spydur", "x", _rows(), lambda c: people[c.name])[2][2] is False
    assert where.my_condos(report, "spydur", "cparish", _rows(), lambda c: set())[0][2] is True   # an owner


def test_whose_condo_is_the_condo_reports_reading_of_members():
    # (review 3) members = a list, "account:X", "group:X", else the owners' lab groups: as the reports read them
    from nomad.condo import config as ccfg
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (job_id TEXT, user_name TEXT, account TEXT, end_time TEXT, source_site TEXT)")
    conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?)", [("1", "eve", "yanglab", "2026-10-30", "spydur"),
                                                             ("2", "dan", "other", "2026-10-30", "spydur")])
    report = ccfg.from_dict({"report": {"clusters": {"spydur": {"condos": {
        "parish": {"owner": "cparish", "partition": "parish", "members": ["amy", "bo"]},
        "yang1": {"owner": "myang", "partition": "yang1", "members": "account:yanglab"}}}}}})
    people_of = where.people_lookup(conn, {"console": {"labs": {"group_pattern": "{netid}$"}}}, "spydur",
                                    now=datetime(2026, 11, 2))
    mine = lambda who: {c: m for c, _n, m in where.my_condos(report, "spydur", who, _rows(), people_of)}  # noqa: E731
    assert mine("amy") == {"parish": True, "yang1": False} and mine("bo")["parish"]
    assert mine("carl") == {"parish": False, "yang1": False}       # not listed: a guest, whatever group he is in
    assert mine("eve")["yang1"] and not mine("dan")["yang1"]       # the account's users this past year
    # a condo whose partition Slurm no longer has, and no nodes given: its nodes can't be told from shared ones
    moved = ccfg.from_dict({"report": {"clusters": {"spydur": {"condos": {
        "parish": {"owner": "cparish", "partition": "parishlab"}}}}}})
    assert where.unplaced_condos(moved, "spydur", _rows()) == ["parish"]


def test_the_gate_asks_nomad_as_the_person(tmp_path, monkeypatch, capsys):
    from nomad.submit import gate
    (tmp_path / "nomad").write_text("#!/bin/sh\n")
    (tmp_path / "nomad").chmod(0o755)
    monkeypatch.setattr(gate, "_uid", lambda: 5201)
    monkeypatch.setattr(gate, "_account", lambda: "amy")
    checked = []
    monkeypatch.setattr(gate, "pinned", lambda path, sha: checked.append((path, sha)) or b"#!/bin/bash\n")
    seen = []

    def login(folder, argv, timeout=None, run_=None):
        seen.append((folder, argv))
        return 0, 'Last login: today\n{"ok": true, "now": "2026-11-02T22:40:00", "starts": [], "nodes": {}}\n'
    monkeypatch.setattr(gate, "in_login", login)
    sha = "a" * 64
    code = gate.run(["starts", "--script", "/scratch/amy/r/job.sh", "--sha256", sha, "--partitions",
                     "basic,condos", "--cpus", "4", "--time", "4:00:00"], str(tmp_path / "nomad-gate"))
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and doc["ok"] and doc["starts"] == [] and checked == [("/scratch/amy/r/job.sh", sha)]
    folder, argv = seen[0]
    assert folder == "/scratch/amy/r" and argv[1:8] == ["submit", "--via", "console", "--script-sha256", sha,
                                                        "--starts", "basic,condos"]
    assert argv[-1] == "/scratch/amy/r/job.sh" and "--cpus" in argv and "--yes" not in argv
    # nomad saying nothing usable: a refusal, not a crash
    monkeypatch.setattr(gate, "in_login", lambda *a, **k: (1, "nomad: no such option"))
    code = gate.run(["starts", "--script", "/scratch/amy/r/job.sh", "--sha256", sha, "--partitions", "basic"],
                    str(tmp_path / "nomad-gate"))
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert not doc["ok"] and "didn't say when it would start" in doc["error"]
    # a job of theirs sent again (Run it again): nomad submit --again, from their home, the pin nomad's to check
    monkeypatch.setattr(gate, "in_login", login)
    monkeypatch.setattr(gate, "_home", lambda: "/home/amy")
    seen.clear()
    checked.clear()
    code = gate.run(["starts", "--job", "4815162", "--sha256", sha, "--partitions", "basic"],
                    str(tmp_path / "nomad-gate"))
    assert code == 0 and checked == []
    folder, argv = seen[0]
    assert folder == "/home/amy" and argv[1:] == ["submit", "--via", "console", "--again", "4815162",
                                                  "--script-sha256", sha, "--starts", "basic"]
    for words in (["starts", "--sha256", sha, "--partitions", "basic"],
                  ["starts", "--job", "1", "--script", "/x/j.sh", "--sha256", sha, "--partitions", "basic"]):
        gate.run(words, str(tmp_path / "nomad-gate"))
        assert "needs --script or --job" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error"]


def test_starts_take_a_few_of_the_gates_places_at_most(tmp_path, monkeypatch):
    # (review 3) each takes minutes: never all the places sending and the rest need
    from nomad.submit import gate
    held = [gate._slot(str(tmp_path), kind="starts", n=gate.STARTS_HERE, wait=0) for _ in range(gate.STARTS_HERE)]
    assert all(h is not None for h in held)
    assert gate._slot(str(tmp_path), kind="starts", n=gate.STARTS_HERE, wait=0) is None
    other = gate._slot(str(tmp_path), wait=0)                 # the other requests' places: free
    assert other is not None and gate.STARTS_HERE < gate.AT_ONCE
    for fd in held + [other]:
        os.close(fd)
    monkeypatch.setattr(gate, "person", lambda name: type("P", (), {"pw_name": "jdoe"})())
    monkeypatch.setattr(gate, "_sudo", lambda: "/usr/bin/sudo")
    monkeypatch.setattr(gate, "_log", lambda m: None)
    monkeypatch.setattr(gate, "state_dir", lambda: str(tmp_path))
    busy = [gate._slot(str(tmp_path), kind="starts", n=gate.STARTS_HERE, wait=0) for _ in range(gate.STARTS_HERE)]
    env = {"SSH_ORIGINAL_COMMAND": f"nomad-gate starts --user jdoe --script /x/j.sh --sha256 {'a' * 64} "
                                   "--partitions basic"}
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    code = gate.serve("/g/nomad-gate", env=env, run_=lambda *a, **k: None, check_=lambda me: None,
                      stdin=io.BytesIO(b""))
    assert code == 4 and "asking where a job would start" in out.getvalue()
    for fd in busy:
        os.close(fd)


def test_a_guest_partition_report_toml_names_is_a_condo_for_guests():
    from nomad.hostlist import expand_hostlist
    only_parish = [("parish", expand_hostlist("spdr[55-58]"), False)]
    parts = {p.name: p for p in where.partitions(_rows(), _specs(), only_parish)}
    # named like "condo": its nodes are condos' (unlisted ones too: no one's own), offered to guests
    assert parts["condos"].kind == "condo" and parts["condos"].condos == ["parish"] and not parts["condos"].mine
    assert parts["johnson"].kind == "condo" and parts["johnson"].condos == [] and not parts["johnson"].mine
    parts = {p.name: p for p in where.partitions(_rows(), _specs(), only_parish, guest_parts=["condos", "basic"])}
    assert parts["condos"].kind == "condo" and parts["basic"].kind == "small"   # never the default
    cands = where.candidates(list(parts.values()), where.Job(mem_mb=8000, time_s=3600), where.Settings())
    assert [c["partition"] for c in cands if c["role"] == "guest"] == ["condos"]


def test_advise_settles_with_the_values_nomad_would_send(tmp_path):
    from nomad.condo import config as ccfg
    db = sqlite3.connect(tmp_path / "combined.db")
    db.execute("CREATE TABLE partition_settings (cluster TEXT, partition TEXT, nodes TEXT, is_default INTEGER, "
               "source_site TEXT, settings TEXT, gone_at TEXT)")
    for p, n in SPYDUR.items():
        db.execute("INSERT INTO partition_settings VALUES ('spydur', ?, ?, ?, 'spydur', '{}', NULL)",
                   (p, n, int(p == "basic")))
    db.execute("CREATE TABLE node_state (node_name TEXT, memory_total_mb INTEGER, gres TEXT, timestamp TEXT, "
               "source_site TEXT)")
    for node, (mem, gpus) in _specs().items():
        db.execute("INSERT INTO node_state VALUES (?, ?, ?, '2026-11-02T22:00:00', 'spydur')",
                   (node, mem, f"gpu:{gpus}" if gpus else ""))
    db.commit()
    report = ccfg.from_dict({"report": {"clusters": {"spydur": {"guest_partitions": ["condos"], "condos": {
        c: {"owner": o, "partition": c} for c, o in OWNERS.items()}}}}})
    asked = []

    def ask(names):
        asked.append(names)
        return {"ok": True, "now": "2026-11-02T22:40:00", "nodes": {"medium": {"idle": 4}},
                "job": {"mem_mb": 8000, "time_s": 3 * 86400, "gpus": 0},     # nomad's own: 3 days
                "starts": [{"partition": n, "start": "2026-11-02T22:40:30", "soon": True} for n in names]}
    out = where.advise(db, "spydur", "amy", where.Job(), config={}, report=report, ask_starts=ask)
    assert asked == [["basic", "condos", "medium", "large", "gpunodes"]]   # all it might suggest, once
    rows = {r["partition"]: r for r in out["rows"]}
    assert set(rows) == {"basic", "condos", "medium", "large"}               # no GPUs asked: not gpunodes
    assert rows["condos"]["allowed"] and not rows["medium"]["allowed"]       # 3 days: a condo yes, big no
    assert out["recommended"] == "basic" and out["job"]["time_s"] == 3 * 86400
    # the gate not answering: said, and nothing recommended
    out = where.advise(db, "spydur", "amy", where.Job(), config={}, report=report,
                       ask_starts=lambda names: {"ok": False, "error": "ssh: timed out"})
    assert out["ok"] and out["error"] == "ssh: timed out" and out["fastest"] is None
    assert out["recommended"] == "basic"                                     # the site's first: nothing timed
    assert not {r["partition"]: r for r in out["rows"]}["medium"]["allowed"]  # no node states: no overflow
    # a gate older than 1.25.4
    out = where.advise(db, "spydur", "amy", where.Job(), config={}, report=report, ask_starts=lambda names: {
        "ok": False, "error": "not something the gate does: starts"})
    assert "older than 1.25.4" in out["error"]
    # report.toml unreadable, or no condos table for the site: only the default partition, and why
    for rep_ in (None, ccfg.from_dict({"report": {"clusters": {"spydur": {"label": "Spydur"}}}})):
        asked.clear()
        out = where.advise(db, "spydur", "amy", where.Job(), config={}, report=rep_, ask_starts=ask)
        assert asked == [["basic"]] and [r["partition"] for r in out["rows"]] == ["basic"]
        assert "report.toml lists no condos for spydur" in out["note"]
    # a site with none says so with an empty table: then every partition is weighed
    asked.clear()
    none = ccfg.from_dict({"report": {"clusters": {"spydur": {"condos": {}}}}})
    where.advise(db, "spydur", "amy", where.Job(), config={}, report=none, ask_starts=ask)
    assert "medium" in asked[0] and "condos" in asked[0]          # named like condo: to guests, no one's own
    # a listed condo nomad can't find the nodes of: only the default partition, and why
    asked.clear()
    moved = ccfg.from_dict({"report": {"clusters": {"spydur": {"condos": {
        "parish": {"owner": "cparish", "partition": "parishlab"}}}}}})
    out = where.advise(db, "spydur", "amy", where.Job(), config={}, report=moved, ask_starts=ask)
    assert asked == [["basic"]] and "can't find the nodes of parish" in out["note"]


def test_partitions_sorted_shows_the_admin_what_the_console_will(tmp_path):
    from nomad.cli import cli
    db = sqlite3.connect(tmp_path / "combined.db")
    db.execute("CREATE TABLE partition_settings (cluster TEXT, partition TEXT, nodes TEXT, is_default INTEGER, "
               "source_site TEXT, settings TEXT, gone_at TEXT)")
    for p, n in SPYDUR.items():
        db.execute("INSERT INTO partition_settings VALUES ('spydur', ?, ?, ?, 'spydur', '{}', NULL)",
                   (p, n, int(p == "basic")))
    db.execute("CREATE TABLE node_state (node_name TEXT, memory_total_mb INTEGER, gres TEXT, timestamp TEXT, "
               "source_site TEXT)")
    for node, (mem, gpus) in _specs().items():
        db.execute("INSERT INTO node_state VALUES (?, ?, ?, '2026-11-02T22:00:00', 'spydur')",
                   (node, None if node == "spdr14" else mem, f"gpu:{gpus}" if gpus else ""))
    db.commit()
    db.close()
    (tmp_path / "nomad.toml").write_text("[general]\n")
    owners = "".join(f'[report.clusters.spydur.condos.{c}]\nowner = "{o}"\n' +
                     ('confirmed = false\n' if c == "johnson" else "") for c, o in OWNERS.items())
    (tmp_path / "report.toml").write_text('[report.clusters.spydur]\nguest_partitions = ["condos"]\n' + owners)
    r = CliRunner().invoke(cli, ["-c", str(tmp_path / "nomad.toml"), "partitions", "--db", str(tmp_path / "combined.db"),
                                 "--site", "spydur", "--sorted"])
    assert r.exit_code == 0, r.output
    lines = {ln.split()[0]: ln for ln in r.output.splitlines() if ln.startswith("  ") and "Note" not in ln}
    # spdr14 never read: the least memory might be lower still, so nothing is "small" for sure (basic: the default)
    assert "several kinds" in lines["basic"] and "the default: always offered" in lines["basic"]
    assert "big memory" in lines["medium"] and "several kinds" in lines["large"]
    assert "condo" in lines["johnson"] and "GPU nodes" in lines["gpunodes"]
    assert "not confirmed yet" in r.output and "johnson" in r.output and "no memory reading for spdr14" in r.output
    r = CliRunner().invoke(cli, ["-c", str(tmp_path / "nomad.toml"), "partitions", "--db", str(tmp_path / "combined.db"),
                                 "--sorted"])
    assert r.exit_code != 0 and "--sorted needs --site" in r.output


def test_it_fails_closed_on_what_it_doesnt_know():
    from nomad.hostlist import expand_hostlist
    specs = _specs()
    for n in expand_hostlist("spdr[09-15]"):
        specs[n] = (None, 0)                              # the big nodes' memory not read yet
    parts = {p.name: p for p in where.partitions(_rows(), specs, _condos())}
    assert parts["medium"].kind == "mixed" and parts["large"].kind == "mixed"       # not small: unknown
    assert parts["cpunodes"].kind == "mixed" and parts["basic"].kind == "mixed"     # nothing small for sure
    cands = where.candidates(list(parts.values()), where.Job(mem_mb=8000), where.Settings())
    assert [(c["partition"], c["role"]) for c in cands if c["allowed"]] == [("basic", "default")]  # still offered
    del specs["spdr16"]                                   # a GPU node never read
    assert {p.name: p for p in where.partitions(_rows(), specs, _condos())}["gpunodes"].kind == "mixed"
    # (review 3) one small node unread: the big ones must not become "small" (shared, no reserve, no limit)
    specs = _specs()
    specs["spdr03"] = (None, 0)
    parts = {p.name: p for p in where.partitions(_rows(), specs, _condos())}
    assert parts["basic"].kind == "mixed" and parts["medium"].kind == "big" and parts["large"].kind == "big"
    cands = {c["partition"]: c for c in where.candidates(list(parts.values()), where.Job(mem_mb=8000),
                                                         where.Settings())}
    assert cands["basic"]["role"] == "default" and cands["medium"]["role"] == "overflow"
    assert not cands["medium"]["allowed"]                 # no time limit: no overflow
    # nor a condo's or a GPU node in the small partition
    rows = _rows() + [{"partition": "odd", "nodes": "spdr[08,54]", "is_default": False}]
    parts = {p.name: p for p in where.partitions(rows, _specs(), _condos())}
    assert parts["medium"].kind == "big" and parts["odd"].kind == "mixed"
    # a condo report.toml doesn't list, inside the guest partition: a condo's nodes, no one's own
    some = [c for c in _condos() if c[0] != "johnson"]
    parts = {p.name: p for p in where.partitions(_rows(), _specs(), some)}
    assert parts["johnson"].kind == "condo" and not parts["johnson"].mine
    # the default partition is always offered, whatever its kind
    rows = [{"partition": "all", "nodes": "spdr[01-15]", "is_default": True},
            {"partition": "basic", "nodes": "spdr[01-08]", "is_default": False}]
    parts = where.partitions(rows, _specs(), [])
    assert [(c["partition"], c["role"]) for c in where.candidates(parts, where.Job(time_s=3600), where.Settings())] \
        == [("basic", "shared"), ("all", "default")]
    # at most 12 asked, a GPU partition kept among them
    many = [where.Part(name=f"p{i}", nodes=frozenset({f"n{i}"})) for i in range(14)] + \
        [where.Part(name="g", nodes=frozenset({"g1"}), gpus=2)]
    names = [p.name for p in many]
    assert where._at_most(names, many, 12)[-1] == "g" and len(where._at_most(names, many, 12)) == 12
    # (review 3) a GPU partition that isn't offered: no crash
    assert where._at_most(names[:14], many, 12) == names[:12]


# ── text files besides job scripts (read, save; only with serve --edit) ─────

import io  # noqa: E402
import sys  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402

import pytest  # noqa: E402

from nomad.submit import gate  # noqa: E402


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def me(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "_uid", lambda: os.getuid())
    monkeypatch.setattr(gate, "_home", lambda: str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    w = tmp_path / "run"
    w.mkdir()
    return w


def test_a_text_file_is_read_and_saved_like_a_script(me):
    p = me / "analysis.py"
    p.write_text("import numpy\nprint(1)\n")
    got = gate.read_text_file(str(p))
    assert got["text"] == "import numpy\nprint(1)\n" and got["sha256"] == _h(got["text"]) and got["utf8"]
    new = "import numpy\nprint(2)\n"
    d = gate.save(str(p), new, body_sha=_h(new), old_sha=got["sha256"])
    assert d["ok"] and p.read_text() == new and gate.recall(str(p), d["kept"]) == got["text"]
    # a new input file, by its extension; never over one there
    d = gate.save(str(me / "water.inp"), "%mem=8GB\n", body_sha=_h("%mem=8GB\n"), new=True)
    assert d["ok"] and stat.S_IMODE((me / "water.inp").stat().st_mode) == 0o600
    # the listing marks them; a job script stays a job script
    (me / "job.sh").write_text("#!/bin/bash\necho\n")
    (me / "data.bin").write_bytes(b"\x00\x01")
    rows = {r["name"]: r for r in gate.listing(str(me))["entries"]}
    assert rows["analysis.py"]["text"] and rows["water.inp"]["text"] and not rows["data.bin"]["text"]
    assert rows["job.sh"]["script"] and not rows["job.sh"]["text"]


def test_what_is_never_opened_or_saved_as_text(me, tmp_path):
    (me / "results.csv").write_text("a,b\n")
    (me / "big.txt").write_text("x" * (gate.MAX_SCRIPT + 1))
    (me / "nul.txt").write_bytes(b"a\x00b\n")
    hidden = tmp_path / "home" / ".config"
    hidden.mkdir()
    (hidden / "settings.toml").write_text("a = 1\n")
    os.symlink(hidden, me / "conf")
    for path, why in ((me / "results.csv", "nor a text file"), (me / "big.txt", "over 256 KB"),
                      (me / "nul.txt", "NUL"), (hidden / "settings.toml", "hidden"),
                      (me / "conf" / "settings.toml", "not a hidden folder")):
        with pytest.raises(gate.Refused, match=why):
            gate.read_text_file(str(path))
    with pytest.raises(gate.Refused, match="nor a text file"):
        gate.save(str(me / "new.csv"), "a\n", body_sha=_h("a\n"), new=True)
    if os.getuid() == 0:
        (me / "theirs.py").write_text("x = 1\n")
        os.chown(me / "theirs.py", 4321, -1)
        with pytest.raises(gate.Refused, match="isn't yours"):
            gate.read_text_file(str(me / "theirs.py"))
    assert gate.text_kind("A.PY") and gate.text_kind("in.R") and not gate.text_kind(".bashrc")
    # a hard link: another name for the file, maybe in a hidden folder (review): never read nor saved through it
    (hidden / "keys.txt").write_text("k\n")
    os.link(hidden / "keys.txt", me / "notes.txt")
    with pytest.raises(gate.Refused, match="hard link"):
        gate.read_text_file(str(me / "notes.txt"))
    with pytest.raises(gate.Refused, match="hard link"):
        gate.save(str(me / "notes.txt"), "x\n", body_sha=_h("x\n"), old_sha=_h("k\n"))
    assert (hidden / "keys.txt").read_text() == "k\n"


def test_reading_text_files_is_the_clusters_say_too(monkeypatch):
    monkeypatch.setattr(gate, "person", lambda name: type("P", (), {"pw_name": "jdoe"})())
    monkeypatch.setattr(gate, "_sudo", lambda: "/usr/bin/sudo")
    monkeypatch.setattr(gate, "_log", lambda m: None)
    calls = []

    def run_(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}\n', "")
    env = {"SSH_ORIGINAL_COMMAND": "nomad-gate read --user jdoe --path /x/a.py"}
    code = gate.serve("/g/nomad-gate", env=env, run_=run_, slot_=lambda: os.open(os.devnull, os.O_RDONLY),
                      check_=lambda me: None, stdin=io.BytesIO(b""))
    assert code == 2 and calls == []
    code = gate.serve("/g/nomad-gate", env=env, run_=run_, slot_=lambda: os.open(os.devnull, os.O_RDONLY),
                      check_=lambda me: None, stdin=io.BytesIO(b""), edit=True)
    assert code == 0 and calls[-1][-3:] == ["read", "--path", "/x/a.py"]
    # the earlier texts saving kept: the same switch
    for asked in ("nomad-gate versions --user jdoe --script /x/a.py",
                  "nomad-gate recall --user jdoe --script /x/a.py --id 20261102T224000-0123456789ab"):
        calls.clear()
        code = gate.serve("/g/nomad-gate", env={"SSH_ORIGINAL_COMMAND": asked}, run_=run_,
                          slot_=lambda: os.open(os.devnull, os.O_RDONLY), check_=lambda me: None,
                          stdin=io.BytesIO(b""))
        assert code == 2 and calls == []
