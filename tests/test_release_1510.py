# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.51.0, the weekly note: jobs that ran short (out of time or memory), jobs
that stopped within a minute, and people new to a cluster whose first jobs
mostly failed."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from nomad import notices as N
from nomad.edu.insights import Issue, UsageStats, UserInsights

NOW = datetime.now().replace(microsecond=0)
CONFIG = {"notices": {"review_to": ["rc@example.edu"], "mail_domain": "example.edu", "kinds": ["jobs"],
                      "send_from": "Research Computing <rc@example.edu>", "link": "https://nomad.example.edu",
                      "signature": ["Research Computing", "Example University"],
                      "contact": "Questions: rc@example.edu"}}


def world(tmp_path):
    db = tmp_path / "combined.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (job_id TEXT, source_site TEXT, user_name TEXT, state TEXT, runtime_seconds INTEGER, "
              "exit_code INTEGER, exit_signal INTEGER, submit_time TEXT, end_time TEXT)")

    def job(jid, site, user, state, ran, days_ago, code=1, sig=None, first=None):
        end = NOW - timedelta(days=days_ago)
        sub = first or end - timedelta(seconds=ran + 60)
        c.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (jid, site, user, state, ran, code, sig, sub.isoformat(sep=" "), end.isoformat()))
    # kim: an old hand, four jobs stopped within seconds this week, one killed for memory (not one of them)
    job("100", "hpc1", "kim", "COMPLETED", 3600, 300, code=0)
    for n in range(4):
        job(f"10{n + 1}", "hpc1", "kim", "FAILED", 8, 1 + n * 0.1)
    job("110", "hpc1", "kim", "FAILED", 20, 1, code=137)
    job("111", "hpc1", "kim", "FAILED", 30, 20)                       # three weeks ago: not this week's
    # pat: two quick ones only
    job("200", "hpc1", "pat", "COMPLETED", 3600, 300, code=0)
    job("201", "hpc1", "pat", "FAILED", 5, 1)
    job("202", "hpc1", "pat", "FAILED", 5, 2)
    # ann: new on hpc2 (first job ten days ago), four of five not completed; long-running, so not quick
    for n, st in enumerate(("FAILED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY", "COMPLETED")):
        job(f"30{n}", "hpc2", "ann", st, 900, 2 + n, code=0 if st == "COMPLETED" else 1)
    job("309", "hpc2", "ann", "CANCELLED", 10, 1)
    # olive: failing, but on hpc1 for a year
    job("400", "hpc1", "olive", "COMPLETED", 3600, 360, code=0)
    for n in range(3):
        job(f"40{n + 1}", "hpc1", "olive", "FAILED", 900, 1 + n)
    # sam: new, and most completed
    for n, st in enumerate(("COMPLETED", "COMPLETED", "FAILED")):
        job(f"50{n}", "hpc1", "sam", st, 900, 2 + n, code=0 if st == "COMPLETED" else 1)
    # lee: jobs this week (what ran short comes from his insights)
    job("600", "hpc1", "lee", "COMPLETED", 3600, 400, code=0)
    job("601", "hpc1", "lee", "TIMEOUT", 43200, 1, code=0)
    c.commit()
    c.close()
    return db


def short_issue(key, directive, suggested, current, stats, **kw):
    base = {"dimension": key, "dimension_key": key, "affected_jobs": 3, "total_applicable": 12, "avg_score": 40.0,
            "severity": "medium", "trajectory": "stable", "directive": directive, "suggested_value": 1,
            "suggested_display": suggested, "current_display": "x", "direction": "raise",
            "current_value_typical": current, "cluster": "hpc1", "partition": "basic", "usage_stats": stats}
    base.update(kw)
    return Issue(**base)


@pytest.fixture
def insights(monkeypatch):
    issues = {"lee": [short_issue("time", "time", "18:00:00", 12 * 3600, UsageStats(12 * 3600, 0, 0, 0, 0, "seconds"),
                                  jobs=[{"job_id": "601"}, {"job_id": "600"}]),
                      short_issue("memory", "mem", "12G", 8192, UsageStats(8000, 0, 0, 0, 0, "MB")),
                      short_issue("gpu", "gres", "2", 1, None),
                      short_issue("time", "time", "18:00:00", 12 * 3600, None, affected_jobs=1, partition="large")]}

    def fake(db_path, username, days=90, config=None, **k):
        return UserInsights(username=username, job_count=12, window_days=days, issues=issues.get(username, []))
    import nomad.edu.insights as I
    monkeypatch.setattr(I, "user_insights", fake)
    return issues


def due(db, config=CONFIG, rec=None, now=None, **kw):
    s = N.settings(config)
    conn = sqlite3.connect(db)
    try:
        return N.plan(conn, db, config, s, rec, now or NOW, **kw)
    finally:
        conn.close()


def test_the_new_settings():
    s = N.settings(CONFIG)
    assert (s.short_min, s.quick_seconds, s.quick_min, s.new_days) == (2, 60, 3, 30) and not s.problems
    bad = N.settings({"notices": {**CONFIG["notices"], "jobs": {"quick_seconds": 1, "new_days": 999,
                                                               "quick_min_jobs": 0}}})
    assert bad.quick_seconds == 60 and bad.new_days == 30 and bad.quick_min == 0 and len(bad.problems) == 2


def test_jobs_that_stopped_within_a_minute(tmp_path, insights):
    db = world(tmp_path)
    got = {m["user"]: m for m in due(db)["mail"]}
    (e,) = [e for e in got["kim"]["entries"] if e.group == "jobs:quick"]
    assert e.title == "Some of your jobs stopped within a minute"
    assert e.lines == ["On hpc1, 4 of your jobs this week stopped with an error within a minute of starting."]
    assert e.jobs == "101, 102, 103 and 104"                                  # 1.62.0: a line of its own
    assert "slurm-JOBID.out" in " ".join(e.todo) and "module" in e.advice[0]
    assert not [e for e in got.get("pat", {"entries": []})["entries"] if e.group == "jobs:quick"]   # two: too few
    off = {"notices": {**CONFIG["notices"], "jobs": {"quick_min_jobs": 0}}}
    assert not [e for m in due(db, config=off)["mail"] for e in m["entries"] if e.group == "jobs:quick"]
    text = N.text_of(got["kim"], N.settings(CONFIG))
    assert ". Some of your jobs stopped within a minute\n" in text and "Jobs: 101, 102, 103 and 104" in text
    assert not any(c in text for c in ("n't", "'re", "'ll", "'ve", "'d "))


def test_people_new_to_a_cluster(tmp_path, insights):
    db = world(tmp_path)
    got = {m["user"]: m for m in due(db)["mail"]}
    (e,) = [e for e in got["ann"]["entries"] if e.group == "jobs:new"]
    assert e.title == "Getting started on hpc2"
    assert e.lines == ["You started running jobs on hpc2 recently, and 4 of your 5 jobs there so far have not "
                       "completed."]
    assert e.advice[0] == "That is common when starting out on a cluster, and it gets better quickly."
    assert e.todo[0].endswith("reply to this email, or write to the address below.")
    for who in ("olive", "sam", "kim"):                                  # a year on hpc1; most completed
        assert not [x for x in got.get(who, {"entries": []})["entries"] if x.group == "jobs:new"], who
    # told once a cluster
    assert not [x for x in N._new_entries(sqlite3.connect(db), "ann", N.settings(CONFIG), NOW,
                                          {"jobs:new:hpc2": NOW.isoformat()}, "source_site",
                                          {"submit_time"}) if x.group == "jobs:new"]
    nocontact = N.settings({"notices": {k: v for k, v in CONFIG["notices"].items() if k != "contact"}})
    (e2,) = N._new_entries(sqlite3.connect(db), "ann", nocontact, NOW, {}, "source_site", {"submit_time"})
    assert e2.todo[0].endswith("reply to this email.")


def test_jobs_that_ran_out_of_time_or_memory(tmp_path, insights):
    db = world(tmp_path)
    got = {m["user"]: m for m in due(db)["mail"]}
    short = [e for e in got["lee"]["entries"] if e.group.startswith("jobs:short")]
    assert [e.title for e in short] == ["Some of your jobs ran out of time", "Some of your jobs ran out of memory"]
    t, m = short
    assert t.lines[0] == ("On hpc1 (basic), 3 of your last 12 jobs reached their time limit "
                          "or came close to it (they typically asked for 12 h and ran about 12 h).")
    assert t.jobs == "601, 600 and 1 more" and t.change == ["#SBATCH --time=18:00:00"]      # 1.62.0: apart
    assert m.lines[0].endswith("ran out of memory or came close to it (they typically asked for 8 GB and used about "
                               "7.8 GB).")
    assert "loses the work" in t.advice[0] and t.todo[0].endswith("https://nomad.example.edu/submit")
    # a GPU "raise", and one of a single job (short_min_jobs = 2): not told
    assert len(short) == 2
    # told again only after repeat_days
    rec = {t.key: NOW.isoformat()}
    assert N._short_entry(insights["lee"][0], N.settings(CONFIG), NOW, rec) is None
    text = N.text_of(got["lee"], N.settings(CONFIG))
    assert "#SBATCH --time=18:00:00" in text
    assert not any(c in text for c in ("n't", "'re", "'ll", "'ve", "'d "))


# -- a job's first minutes, kept (job_live) ---------------------------------------------------------

def _row(job, elapsed, at, **kw):
    return {"job_id": job, "slurm_id": job, "user": "kim", "cpus": 4, "nodes": 1, "start": "2026-11-16T10:00:00",
            "sampled_at": at.isoformat(), "elapsed_s": elapsed, "measured": 1, "tasks": 1, "steps": "batch",
            "cpu_s": 4.0 * elapsed, "busy_avg": 4.0, "rss_now_mb": 1000.0 + elapsed, "peak_rss_mb": 1000.0 + elapsed,
            "peaks_sum_mb": 1000.0 + elapsed, **kw}


def test_a_jobs_first_minutes_are_kept(tmp_path):
    from nomad.collectors import job_live as J
    db = tmp_path / "s.db"
    c = J.JobLiveCollector({"sstat_as": "slurm"}, db)
    assert (c.early_minutes, c.early_keep_days) == (30, 90)
    t = datetime(2026, 11, 16, 10, 5)
    c.store([_row("7", 300, t), _row("8", 4000, t)])                 # 8 has run over an hour: not kept
    c.store([_row("7", 600, t + timedelta(minutes=5))])
    c.store([_row("7", 1800, t + timedelta(minutes=25)), _row("9", 60, t + timedelta(minutes=25))])
    c.store([_row("7", 2100, t + timedelta(minutes=30))])             # past 30 minutes: no more
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    got = [(r["job_id"], r["elapsed_s"], r["rss_now_mb"]) for r in conn.execute(
        "SELECT * FROM job_early ORDER BY id")]
    assert got == [("7", 300, 1300.0), ("7", 600, 1600.0), ("7", 1800, 2800.0), ("9", 60, 1060.0)]
    cols = {r[1] for r in conn.execute("PRAGMA table_info(job_early)")}
    assert "user" not in cols and {"held_most_mb", "busy_now", "start"} <= cols
    # kept early_keep_days; older ones go at the next reading
    conn.execute("INSERT INTO job_early (job_id, sampled_at) VALUES ('old', ?)",
                 ((datetime.now() - timedelta(days=91)).isoformat(),))
    conn.commit()
    c.store([])
    assert conn.execute("SELECT COUNT(*) FROM job_early WHERE job_id = 'old'").fetchone()[0] == 0
    # off: nothing kept, no table
    db2 = tmp_path / "t.db"
    J.JobLiveCollector({"sstat_as": "slurm", "early_minutes": 0}, db2).store([_row("7", 300, t)])
    assert sqlite3.connect(db2).execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'job_early'").fetchone()[0] == 0
    bad = J.JobLiveCollector({"sstat_as": "slurm", "early_minutes": "lots", "early_keep_days": 0}, db2)
    assert (bad.early_minutes, bad.early_keep_days) == (30, 90)


# -- a simple rule to beat: "their last job did it" ---------------------------------------------------

def _jobs_db(path):
    """Seven made-up people over ten weeks; each fails in runs (after a failure, mostly again)."""
    import numpy as np
    rng = np.random.default_rng(4)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE jobs (job_id TEXT, source_site TEXT, user_name TEXT, partition TEXT, req_cpus INTEGER, "
              "req_gpus INTEGER, req_mem_mb INTEGER, req_time_seconds INTEGER, runtime_seconds INTEGER, "
              "submit_time TEXT, end_time TEXT, state TEXT)")
    last = {}
    for i in range(1400):
        who = ("kim", "pat", "ann", "olive", "lee", "sam", "quinn")[i % 7]
        sub = NOW - timedelta(days=72) + timedelta(hours=i * 1.2)
        bad = rng.random() < (0.7 if last.get(who) else 0.15)
        last[who] = bad
        c.execute("INSERT INTO jobs VALUES (?, 's', ?, ?, ?, 0, 4096, 36000, 600, ?, ?, ?)",
                  (str(i), who, str(rng.choice(["a", "b"])), int(rng.choice([1, 4])), sub.isoformat(),
                   (sub + timedelta(minutes=10)).isoformat(), "FAILED" if bad else "COMPLETED"))
    c.commit()
    c.row_factory = sqlite3.Row
    return c


def test_each_job_carries_what_its_persons_last_job_did(tmp_path):
    from nomad.learn import dataset as D
    from nomad.learn.settings import Settings
    c = _jobs_db(tmp_path / "c.db")
    data, mapping = D.build(c, {**Settings(enabled=True, engine="baseline", predicts=("failure",)).plan()}, NOW)
    prev = data["previous"]["failure"]
    assert len(prev) == len(data["rows"]) and set(prev) <= {0, 1, None}
    by = {jid: i for i, jid, _ in mapping}
    users = {r["job_id"]: r["user_name"] for r in c.execute("SELECT job_id, user_name FROM jobs")}
    states = {r["job_id"]: r["state"] for r in c.execute("SELECT job_id, state FROM jobs")}
    # job 14 is kim's third: her second (7) had ended when it was sent
    assert prev[by["14"]] == (1 if states["7"] == "FAILED" else 0) and users["14"] == users["7"] == "kim"
    assert prev[by["0"]] is None                                     # her first: nothing before it
    assert "kim" not in str(data["previous"])                        # numbers only


def test_the_rule_beside_the_models(tmp_path):
    from nomad.learn import dataset as D
    from nomad.learn import train as T
    from nomad.learn.settings import Settings
    c = _jobs_db(tmp_path / "c.db")
    plan = {**Settings(enabled=True, engine="baseline", predicts=("failure",), bootstrap=0, rolling=2,
                       people_folds=2, compare=False).plan()}
    data, _ = D.build(c, plan, NOW)
    out = T.run(data, "cpu")["summary"]["targets"]["failure"]
    r = out["rule"]
    assert r["auc"] > 0.65 and r["right"] > 0.55 and r["caught"] > 0.5      # runs of failures: the rule sees them
    assert r["models"]["baseline"]["right"] < r["right"]                     # the request alone can't
    assert out["tenth"]["baseline"]["right"] is not None and out["tenth"]["tessera"] is None
    for test in (*out["rolling"]["periods"], *out["people"]["groups"]):
        if not test.get("skipped"):
            assert test["rule"]["auc"] is not None and "tenth" in test
    assert out["rolling"]["over"]["rule"]["auc"] > 0.6 and "tenth" in out["rolling"]["over"]
    # nothing known before: no rule
    data.pop("previous")
    assert "rule" not in T.run(data, "cpu")["summary"]["targets"]["failure"]


def test_the_cli_says_whether_it_is_good_enough_to_warn():
    from nomad.learn import cli as C
    x = {"train": 7015, "holdout": 1754, "rate_holdout": 0.226, "auc": {"tessera": 0.59, "baseline": 0.55},
         "top_tenth": {"tessera": 0.38, "baseline": 0.22},
         "tenth": {"tessera": {"right": 0.38, "caught": 0.17}, "baseline": {"right": 0.22, "caught": 0.1}},
         "rule": {"auc": 0.73, "flagged": 560, "of": 1754, "right": 0.63, "caught": 0.64,
                  "models": {"tessera": {"right": 0.41, "caught": 0.41}, "baseline": {"right": 0.3, "caught": 0.3}}}}
    text = "\n".join(C._target_lines({"targets": {"failure": x}}, {"warn_right": 0.8, "warn_caught": 0.5}))
    assert "TESSERA warning about the tenth it ranked first: right 3.8 in 10, would have caught 17% of the jobs " \
           "that failed" in text
    assert "the simple rule (warn when the person's last job failed): warns about 560 of 1,754, right 6.3 in 10, " \
           "would have caught 64%; TESSERA warning about as many: right 4.1 in 10, caught 41%" in text
    assert "good enough to warn people (right at least 8.0 in 10 and catching at least 50%): no" in text
    easy = {**x, "rule": {**x["rule"], "right": 0.85, "caught": 0.6}}
    assert "catching at least 50%): yes" in "\n".join(C._target_lines({"targets": {"failure": easy}}))
