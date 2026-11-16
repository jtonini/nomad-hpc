"""nomad 1.48.0: each lab's term report (`nomad usage-report labs`)."""
import json
import os
import re
import sqlite3
from datetime import datetime

import pytest
from click.testing import CliRunner

from nomad.collectors.storage_use import SCHEMA as STORAGE_SCHEMA
from nomad.condo import config as ccfg, run, term

FALL = (datetime(2026, 8, 25), datetime(2027, 1, 10), "Fall")
NOW = datetime(2027, 1, 10, 7, 30)        # the timer, the morning the spring term begins
NOMAD_CONFIG = {"console": {"labs": {"group_pattern": "{netid}$"}}}

REPORT_TOML = """
[report.labs]
mail_domain = "example.edu"
send_from = "Research Computing <rc@example.edu>"
review_to = ["rc@example.edu"]
formats = ["html", "json", "md"]

[report.clusters.hpc1]
label = "Cluster One"

[report.clusters.hpc1.condos.lee]
owner = "lee"
confirmed = true
"""


def t(month, day, hour=0, year=2026):
    return datetime(year, month, day, hour).isoformat()


def build(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE node_state (id INTEGER PRIMARY KEY, timestamp TEXT, node_name TEXT, state TEXT,
        cpus_total INTEGER, cpus_alloc INTEGER, cpu_load REAL, memory_total_mb INTEGER,
        partitions TEXT, gres TEXT, cluster TEXT, source_site TEXT);
    CREATE TABLE jobs (job_id TEXT, user_name TEXT, partition TEXT, node_list TEXT, submit_time TEXT,
        eligible_time TEXT, start_time TEXT, end_time TEXT, state TEXT, req_cpus INTEGER, req_mem_mb INTEGER,
        req_gpus INTEGER, runtime_seconds INTEGER, account TEXT, alloc_tres TEXT, alloc_gpus INTEGER,
        source_site TEXT);
    CREATE TABLE job_summary (job_id TEXT, avg_cpu_percent REAL, peak_memory_gb REAL, source_site TEXT);
    CREATE TABLE group_membership (username TEXT, group_name TEXT, collected_at TEXT,
        has_account INTEGER, source_site TEXT, cluster TEXT);
    """)
    c.executescript(STORAGE_SCHEMA)
    for site, node, parts in (("hpc1", "n01", "basic"), ("hpc1", "c01", "lee,condos"), ("hpc2", "a01", "std")):
        c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, memory_total_mb, "
                  "partitions, cluster, source_site) VALUES (?, ?, 'IDLE', 32, 0, 256000, ?, ?, ?)",
                  (t(12, 1), node, parts, site, site))
    jobs = [
        # id, user, partition, node, submit, start, end, state, cpus, gpus, site
        ("1", "kim", "basic", "n01", t(9, 1), t(9, 1, 1), t(9, 1, 11), "COMPLETED", 8, 0, "hpc1"),
        ("2", "pat", "basic", "n01", t(10, 1), t(10, 1, 2), t(10, 1, 12), "FAILED", 16, 0, "hpc1"),
        ("3", "pat", "std", "a01", t(11, 1), t(11, 1), t(11, 1, 5), "OUT_OF_MEMORY", 4, 1, "hpc2"),
        # Began before the term: only its hours inside count; it ended in it.
        ("4", "ann", "basic", "n01", t(8, 24, 14), t(8, 24, 14), t(8, 25, 10), "COMPLETED", 10, 0, "hpc1"),
        # Ends after the term: its hours inside count, it isn't among the ended.
        ("5", "ann", "basic", "n01", t(1, 9, 20, 2027), t(1, 9, 20, 2027), t(1, 10, 4, 2027), "COMPLETED", 2, 0,
         "hpc1"),
        # The term before.
        ("6", "kim", "basic", "n01", t(6, 1), t(6, 1), t(6, 1, 10), "COMPLETED", 4, 0, "hpc1"),
        # Another lab's people.
        ("7", "olive", "basic", "n01", t(9, 2), t(9, 2), t(9, 2, 10), "COMPLETED", 8, 0, "hpc1"),
        ("8", "lee", "lee", "c01", t(9, 3), t(9, 3), t(9, 3, 10), "COMPLETED", 8, 0, "hpc1"),
    ]
    for (jid, user, part, node, sub, start, end, state, cpus, gpus, site) in jobs:
        c.execute("INSERT INTO jobs (job_id, user_name, partition, node_list, submit_time, start_time, end_time, "
                  "state, req_cpus, req_mem_mb, req_gpus, runtime_seconds, account, alloc_tres, source_site) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 8000, ?, 0, 'default', ?, ?)",
                  (jid, user, part, node, sub, start, end, state, cpus, gpus, f"cpu={cpus},mem=8000M,node=1", site))
    c.execute("INSERT INTO job_summary VALUES ('1', 50.0, 4.0, 'hpc1')")
    c.execute("INSERT INTO job_summary VALUES ('2', 10.0, 1.0, 'hpc1')")
    for user, group in (("kim", "kim$"), ("pat", "kim$"), ("ann", "kim$"), ("olive", "olive$"),
                        ("lee", "lee$"), ("sam", "lee$"), ("quinn", "quinn$")):
        c.execute("INSERT INTO group_membership VALUES (?, ?, ?, 1, 'hpc1', 'hpc1')", (user, group, NOW.isoformat()))
    # Storage: a reading before the term began and one before it ended; a
    # server holding copies of the first isn't counted.
    c.execute("INSERT INTO storage_reports (server, mode, started) VALUES ('fs1', 'usage', ?)", (t(12, 1),))
    c.execute("INSERT INTO storage_reports (server, mode, started, copy_of) VALUES ('bk1', 'usage', ?, 'fs1')",
              (t(12, 1),))
    for server in ("fs1", "bk1"):
        for when in (t(8, 20), t(12, 1)):
            c.execute("INSERT INTO storage_paths (timestamp, server, path, total_bytes, used_bytes) "
                      "VALUES (?, ?, '/data', 10000000000000, 1000)", (when, server))
    for server in ("fs1", "bk1"):
        for when, kim, pat, olive in ((t(8, 20), 1_000_000_000_000, 0, 5), (t(12, 1), 1_500_000_000_000,
                                                                             200_000_000_000, 5)):
            for name, b in (("kim", kim), ("pat", pat), ("olive", olive)):
                c.execute("INSERT INTO storage_owners (timestamp, server, path, kind, name, used_bytes) "
                          "VALUES (?, ?, '/data', 'user', ?, ?)", (when, server, name, b))
    c.commit()
    return c


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "combined.db"
    build(path).close()
    return path


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "report.toml"
    path.write_text(REPORT_TOML)
    return ccfg.load(path)


@pytest.fixture
def data(db, settings):
    conn = sqlite3.connect(db)
    try:
        return term.build(conn, settings, FALL[0], FALL[1], name=FALL[2], nomad_config=NOMAD_CONFIG, now=NOW)
    finally:
        conn.close()


def lab(data, group):
    return next(f for f in data["reports"] if f["lab"]["group"] == group)


# --- terms -----------------------------------------------------------------------

def test_the_term_that_ended_and_the_one_under_way():
    terms = ccfg.DEFAULT_TERMS
    assert term.last_term(terms, NOW) == FALL
    assert term.term_of(terms, NOW) == (datetime(2027, 1, 10), datetime(2027, 5, 10), "Spring")
    assert term.last_term(terms, datetime(2026, 11, 14)) == (datetime(2026, 5, 10), datetime(2026, 8, 25), "Summer")
    assert term.label(*FALL) == "Fall 2026"
    assert term.span(FALL[0], FALL[1]) == "25 August 2026 to 9 January 2027"
    assert term.key(FALL[0], FALL[1]) == "2026-08-25_2027-01-10"


def test_terms_in_report_toml():
    s = ccfg.from_dict({"report": {"labs": {"terms": {"09-01": "Autumn", "02-01": "Winter"}}}})
    assert s.delivery.terms == ((2, 1, "Winter"), (9, 1, "Autumn"))
    assert ccfg.from_dict({"report": {"labs": {"terms": ["09-01", "02-01"]}}}).delivery.terms[0] == (2, 1, None)
    for bad in (["09-01"], {"13-01": "x", "01-01": "y"}, {"02-29": "x", "01-01": "y"}, "09-01",
                {"09-01": "x", "9-1": "y"}):
        with pytest.raises(ccfg.CondoConfigError):
            ccfg.from_dict({"report": {"labs": {"terms": bad}}})
    with pytest.raises(ccfg.CondoConfigError, match="term_condo_owners"):
        ccfg.from_dict({"report": {"labs": {"term_condo_owners": "yes"}}})


# --- figures ---------------------------------------------------------------------

def test_the_labs_use_by_cluster(data):
    f = lab(data, "kim$")
    assert f["lab"]["leaders"] == ["kim"] and f["lab"]["members"] == 3
    total = f["total"]
    # 8x10 + 16x10 + 4x5 + 10x10 (inside the term) + 2x4 (before it ended)
    assert total["core_hours"] == 80 + 160 + 20 + 100 + 8
    assert total["gpu_hours"] == 5
    assert total["jobs_ended"] == 4 and total["outcomes"]["out_of_memory"] == 1
    assert total["failed_pct"] == 50.0
    assert total["people"] == 3 and total["clusters"] == 2
    one = next(c for c in f["clusters"] if c["cluster"] == "hpc1")
    assert one["label"] == "Cluster One" and one["core_hours"] == 80 + 160 + 100 + 8
    # CPU used of the cores held: 0.5 x 80 + 0.1 x 160 over 240
    assert one["cpu_used_pct"] == round(100 * (40 + 16) / 240, 1)
    # Waits of the jobs that started in the term: 60, 120 and 0 minutes (ann's
    # first job started before it).
    assert one["wait_median_minutes"] == 60.0 and one["waits"] == 3
    # Summer: kim's 4 x 10, and ann's 10 x 10 before the fall term began.
    assert f["previous"]["measured"] and f["previous"]["core_hours"] == 40 + 100


def test_members_by_name_and_nobody_elses(data, settings):
    f = lab(data, "kim$")
    each = {r["user"]: r for r in f["members"]["each"]}
    assert set(each) == {"kim", "pat", "ann"} and each["kim"]["pi"]
    assert each["pat"]["core_hours"] == 180 and each["pat"]["failed"] == 2
    assert each["kim"]["storage_bytes"] == 1_500_000_000_000
    labs, summary = term.build_all(data, settings, ["html"])
    text = next(b for b in labs if b.key == "kim$").text
    assert re.search(r"\bpat\b", text)
    for other in ("olive", "lee", "sam"):
        assert not re.search(rf"\b{other}\b", text)


def test_storage_over_the_term_without_copies(data):
    s = lab(data, "kim$")["storage"]
    assert s["measured"] and [p["server"] for p in s["paths"]] == ["fs1"]
    p = s["paths"][0]
    assert p["used_bytes"] == 1_700_000_000_000 and p["change_bytes"] == 700_000_000_000 and p["people"] == 2


def test_storage_before_any_reading_is_not_measured(db, settings):
    conn = sqlite3.connect(db)
    try:
        # No use that term either: asked for by name, it is written anyway.
        d = term.build(conn, settings, datetime(2026, 1, 10), datetime(2026, 5, 10), nomad_config=NOMAD_CONFIG,
                       now=NOW, only=["kim$"])
    finally:
        conn.close()
    f = lab(d, "kim$")
    assert not f["storage"]["measured"] and f["previous"]["measured"] is False


def test_a_lab_with_no_use_all_term_gets_no_report(db, settings, data):
    # quinn$: a group named for its one member, who ran nothing and holds nothing.
    assert {"lab": "quinn$", "name": "quinn$", "why": "quiet"} in data["left_out"]
    assert "quinn$" not in {f["lab"]["group"] for f in data["reports"]}
    labs, summary = term.build_all(data, settings, ["html"])
    assert "No report, as they ran no job all term and hold under 1 GB: quinn$." in summary.text
    conn = sqlite3.connect(db)
    try:
        asked = term.build(conn, settings, *FALL[:2], nomad_config=NOMAD_CONFIG, now=NOW, only=["quinn$"])
    finally:
        conn.close()
    assert [f["lab"]["group"] for f in asked["reports"]] == ["quinn$"]


def test_condo_owners_get_the_monthly_report_instead(db, settings, data):
    assert [x["lab"] for x in data["left_out"] if x["why"] == "condo"] == ["lee$"]
    assert {f["lab"]["group"] for f in data["reports"]} == {"kim$", "olive$"}
    conn = sqlite3.connect(db)
    try:
        d = term.build(conn, settings, *FALL[:2], nomad_config=NOMAD_CONFIG, now=NOW, condo_owners=True)
    finally:
        conn.close()
    assert "lee$" in {f["lab"]["group"] for f in d["reports"]}
    assert [x["why"] for x in d["left_out"]] == ["quiet"]


def test_members_unnamed_when_turned_off(db, tmp_path):
    path = tmp_path / "r.toml"
    path.write_text(REPORT_TOML.replace("formats", "name_members = false\nformats"))
    s = ccfg.load(path)
    conn = sqlite3.connect(db)
    try:
        d = term.build(conn, s, *FALL[:2], nomad_config=NOMAD_CONFIG, now=NOW)
    finally:
        conn.close()
    labs, _ = term.build_all(d, s, ["html"])
    text = next(b for b in labs if b.key == "kim$").text
    assert not re.search(r"\bpat\b", text) and "3 members" in text


def test_a_term_under_way_says_so(db, settings):
    conn = sqlite3.connect(db)
    try:
        d = term.build(conn, settings, *FALL[:2], name="Fall", nomad_config=NOMAD_CONFIG,
                       now=datetime(2026, 11, 14))
    finally:
        conn.close()
    assert d["period"]["so_far"]
    labs, summary = term.build_all(d, settings, ["html"])
    assert "Fall 2026 so far" in labs[0].text and "14 November 2026" in summary.text


# --- files and mail --------------------------------------------------------------

def test_files_are_private(tmp_path, data, settings):
    labs, summary = term.build_all(data, settings, ["html", "json"])
    out = tmp_path / "labs"
    paths = run.write(out, data["period"]["key"], labs, summary, ["html", "json"])
    assert (out / "kim" / "2026-08-25_2027-01-10.json") in paths
    assert all(os.stat(p).st_mode & 0o777 == 0o600 for p in paths)
    assert json.loads((out / "kim" / "2026-08-25_2027-01-10.json").read_text())["lab"]["name"] == "kim$"


def test_review_first_then_each_pi(tmp_path, data, settings):
    labs, summary = term.build_all(data, settings, ["html"])
    plan = term.plan_mail(labs, summary, settings, data)
    kim = next(p for p in plan if p["key"] == "kim$")
    assert kim["to"] == ["rc@example.edu"] and kim["review_of"] == "kim@example.edu"
    assert kim["subject"] == "[Review] kim$: research computing, Fall 2026"
    settings.delivery.review_to = []
    plan = term.plan_mail(labs, summary, settings, data)
    assert next(p for p in plan if p["key"] == "kim$")["to"] == ["kim@example.edu"]
    settings.delivery.mail_domain = None
    assert "mail_domain" in next(p for p in term.plan_mail(labs, summary, settings, data)
                                 if p["key"] == "kim$")["skip"]


def test_sending_marks_review_copies(monkeypatch, tmp_path, data, settings):
    from nomad import mail
    sent = []
    monkeypatch.setattr(mail, "send", lambda msg, cfg: sent.append(msg))
    labs, summary = term.build_all(data, settings, ["html"])
    plan = term.plan_mail(labs, summary, settings, data)
    res = run.send(labs, summary, settings, {}, data["period"]["key"], tmp_path, plan=plan)
    assert {r["status"] for r in res} == {"sent"} and len(sent) == 3
    assert all(m["To"] == "rc@example.edu" for m in sent)
    again = run.send(labs, summary, settings, {}, data["period"]["key"], tmp_path, plan=plan)
    assert {r["status"] for r in again} == {"already sent"}


# --- the command -----------------------------------------------------------------

class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def _cli(db, tmp_path, *args, monkeypatch=None):
    import nomad.cli
    from nomad.cli import cli
    if monkeypatch is not None:
        monkeypatch.setattr(nomad.cli, "datetime", _Frozen)
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n')
    rt = tmp_path / "report.toml"
    rt.write_text(REPORT_TOML)
    return CliRunner().invoke(cli, ["-c", str(cfg), "usage-report", "labs", "--db", str(db),
                                    "--config", str(rt), *args], catch_exceptions=False)


def test_the_command(db, tmp_path, monkeypatch):
    r = _cli(db, tmp_path, "--out", str(tmp_path / "o"), monkeypatch=monkeypatch)
    assert r.exit_code == 0, r.output
    assert "Fall 2026 (25 August 2026 to 9 January 2027): lab reports" in r.output
    assert "kim$ (kim): 3 of 3 ran jobs" in r.output and "holding condos: lee$" in r.output
    assert "no report: 1 lab with no job all term that holds under 1 GB: quinn$ (--lab writes one anyway)" in r.output
    assert (tmp_path / "o" / "kim" / "2026-08-25_2027-01-10.html").exists()
    masked = _cli(db, tmp_path, "--out", str(tmp_path / "m"), "--mask", monkeypatch=monkeypatch)
    for name in ("kim", "lee", "olive", "quinn"):
        assert name not in masked.output
    assert "lab 1:" in masked.output and "lab 2:" in masked.output
    so_far = _cli(db, tmp_path, "--term", "current", "--out", str(tmp_path / "c"), monkeypatch=monkeypatch)
    assert "Spring 2027 so far" in so_far.output and "not mailed: the term isn't over" in so_far.output


def test_a_term_not_over_isnt_sent(db, tmp_path):
    r = _cli(db, tmp_path, "--from", "2026-08-25", "--to", "2099-01-09", "--send")
    assert r.exit_code != 0 and "isn't over" in r.output


def test_the_timer_runs_as_each_term_begins(tmp_path):
    from nomad.cli import _term_timer_units
    service, timer = _term_timer_units("/opt/nomad/venv/bin/nomad", "/etc/nomad/nomad.toml", "nomad",
                                       ccfg.DEFAULT_TERMS, "07:30")
    assert "ExecStart=/opt/nomad/venv/bin/nomad -c /etc/nomad/nomad.toml usage-report labs --send" in service
    assert [x for x in timer.splitlines() if x.startswith("OnCalendar")] == [
        "OnCalendar=*-01-10 07:30", "OnCalendar=*-05-10 07:30", "OnCalendar=*-08-25 07:30"]
    assert "User=nomad" in service and "Persistent=true" in timer


def test_from_and_to_name_the_term_they_match(db, tmp_path, monkeypatch):
    r = _cli(db, tmp_path, "--from", "2026-08-25", "--to", "2027-01-09", "--out", str(tmp_path / "f"),
             monkeypatch=monkeypatch)
    assert r.output.startswith("Fall 2026 (25 August 2026 to 9 January 2027): lab reports")
    r = _cli(db, tmp_path, "--from", "2026-09-01", "--to", "2026-09-30", "--out", str(tmp_path / "s"),
             monkeypatch=monkeypatch)
    assert r.output.startswith("1 September 2026 to 30 September 2026: lab reports")
    bad = CliRunner().invoke(__import__("nomad.cli", fromlist=["cli"]).cli,
                             ["usage-report", "labs", "--from", "2026-09-01"])
    assert bad.exit_code != 0 and "go together" in bad.output
