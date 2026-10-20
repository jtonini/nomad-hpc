"""nomad 1.15.0: monthly condo reports (`nomad usage-report condos`)."""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad.condo import config as ccfg, content, discover, figures, render, run

NOW = datetime(2026, 10, 1, 7, 0)       # the morning of the 1st
MONTH = "2026-09"
NOMAD_CONFIG = {"console": {"labs": {"group_pattern": "{netid}$"}}}

REPORT_TOML = """
[report.labs]
mail_domain = "example.edu"
send_from = "Research Computing <rc@example.edu>"
review_to = ["rc@example.edu"]
formats = ["html", "json", "md"]

[report.policy]
owner_start_minutes = 5
guest_max_days = 10

[report.clusters.hpc1]
label = "Cluster One"

[report.clusters.hpc1.condos.smith]
owner = "jsmith"
cc = ["manager@example.edu"]

[report.clusters.hpc1.condos.jones_lab]
owner = "rjones"
confirmed = false
"""


def t(day, hour=0, minute=0, month=9):
    return datetime(2026, month, day, hour, minute).isoformat()


def build(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE node_state (id INTEGER PRIMARY KEY, timestamp TEXT, node_name TEXT, state TEXT,
        cpus_total INTEGER, cpus_alloc INTEGER, cpu_load REAL, memory_total_mb INTEGER,
        partitions TEXT, gres TEXT, cluster TEXT, source_site TEXT);
    CREATE TABLE jobs (job_id TEXT, user_name TEXT, group_name TEXT, partition TEXT, node_list TEXT,
        job_name TEXT, submit_time TEXT, eligible_time TEXT, start_time TEXT, end_time TEXT, state TEXT,
        req_cpus INTEGER, req_mem_mb INTEGER, req_gpus INTEGER, runtime_seconds INTEGER,
        wait_time_seconds INTEGER, account TEXT, alloc_tres TEXT, alloc_gpus INTEGER, source_site TEXT);
    CREATE TABLE job_summary (job_id TEXT, avg_cpu_percent REAL, peak_memory_gb REAL, source_site TEXT);
    CREATE TABLE group_membership (username TEXT, group_name TEXT, collected_at TEXT,
        has_account INTEGER, source_site TEXT, cluster TEXT);
    """)
    layout = {"c01": "smith,condos", "c02": "smith,condos", "c03": "jones_lab,condos",
              "c04": "jones_lab,condos", "n01": "basic", "n02": "basic"}
    ts = datetime(2026, 8, 1)
    while ts < datetime(2026, 10, 1):
        for node, parts in layout.items():
            down = node == "c02" and datetime(2026, 9, 10) <= ts < datetime(2026, 9, 11)
            c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, cpu_load, "
                      "memory_total_mb, partitions, gres, cluster, source_site) "
                      "VALUES (?, ?, ?, 32, 0, 0, 256000, ?, NULL, 'hpc1', 'hpc1')",
                      (ts.isoformat(), node, "DOWN*" if down else "IDLE", parts))
        for node in ("a01", "a02"):
            c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, cpu_load, "
                      "memory_total_mb, partitions, gres, cluster, source_site) "
                      "VALUES (?, ?, 'IDLE', 16, 0, 0, 64000, 'standard', NULL, 'hpc2', 'hpc2')",
                      (ts.isoformat(), node))
        ts += timedelta(hours=1)
    jobs = [
        # id, user, partition, nodes, submit, eligible, start, end, state, cpus, runtime_s, site
        ("1", "jsmith", "smith", "c01", t(1, 23, 58, 9 - 0), None, t(2), t(2, 10), "COMPLETED", 16, 36000, "hpc1"),
        ("2", "s1", "smith", "c[01-02]", t(4, 22), t(4, 23), t(5), t(5, 5), "FAILED", 64, 18000, "hpc1"),
        ("3", "g1", "condos", "c02", t(4, 19), None, t(4, 20), t(5), "PREEMPTED", 32, 14400, "hpc1"),
        ("4", "s1", "condos", "c03", t(12), None, t(12), t(12, 10), "COMPLETED", 8, 36000, "hpc1"),
        ("5", "s1", "basic", "n01", t(13), None, t(13), t(13, 20), "COMPLETED", 8, 72000, "hpc1"),
        ("6", "s1", "standard", "a01", t(14), None, t(14), t(14, 10), "COMPLETED", 4, 36000, "hpc2"),
        ("7", "x9", "smith", "c02", t(15), None, t(15), t(15, 5), "COMPLETED", 8, 18000, "hpc1"),
        ("8", "s1", "smith", None, t(29, 12), None, None, None, "PENDING", 4, 0, "hpc1"),
        ("9", "g2", "condos", "c01", t(30, 19), None, t(30, 20), None, "RUNNING", 4, 39600, "hpc1"),
        ("10", "g3", "condos", "c02", t(24, 0, 0, 8), None, t(25, 0, 0, 8), t(6), "COMPLETED", 2, 12 * 86400, "hpc1"),
        ("11", "jsmith", "smith", "c01", t(10, 0, 0, 8), None, t(10, 0, 0, 8), t(10, 10, 0, 8), "COMPLETED",
         32, 36000, "hpc1"),
        # The lab's own jobs fill both nodes; the next one waits for them.
        ("12", "jsmith", "smith", "c[01-02]", t(22, 8), None, t(22, 8), t(22, 12), "COMPLETED", 64, 14400, "hpc1"),
        ("13", "s1", "smith", "c01", t(22, 9), None, t(22, 12), t(22, 13), "COMPLETED", 16, 3600, "hpc1"),
        # Waits with nothing on the nodes (a dependency, say).
        ("14", "jsmith", "smith", "c01", t(20, 10), None, t(20, 12), t(20, 14), "COMPLETED", 8, 7200, "hpc1"),
    ]
    for (jid, user, part, nodes, sub, elig, start, end, state, cpus, rt, site) in jobs:
        c.execute("INSERT INTO jobs (job_id, user_name, partition, node_list, submit_time, eligible_time, "
                  "start_time, end_time, state, req_cpus, req_mem_mb, req_gpus, runtime_seconds, account, "
                  "alloc_tres, source_site) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 8000, 0, ?, 'default', ?, ?)",
                  (jid, user, part, nodes, sub, elig, start, end, state, cpus, rt,
                   f"cpu={cpus},mem=8000M,node=1", site))
    c.execute("INSERT INTO job_summary VALUES ('5', 50.0, 4.0, 'hpc1')")
    c.execute("INSERT INTO job_summary VALUES ('1', 10.0, 2.0, 'hpc1')")
    for user, group in (("jsmith", "jsmith$"), ("s1", "jsmith$"), ("rjones", "rjones$")):
        c.execute("INSERT INTO group_membership VALUES (?, ?, ?, 1, 'hpc1', 'hpc1')",
                  (user, group, NOW.isoformat()))
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
        return figures.build(conn, settings, MONTH, nomad_config=NOMAD_CONFIG, now=NOW)
    finally:
        conn.close()


def report(data, name):
    return next(r for r in data["reports"] if r["condo"]["name"] == name)


# --- finding the condos ----------------------------------------------------------

def test_condos_are_partitions_inside_the_guest_partition(db):
    conn = sqlite3.connect(db)
    found = discover.find(discover.read_nodes(conn, "hpc1", until=NOW))
    assert found.guest_partitions == ["condos"]
    assert found.condos == {"jones_lab": {"c03", "c04"}, "smith": {"c01", "c02"}}
    assert discover.find(discover.read_nodes(conn, "hpc2", until=NOW)).condos == {}


def test_a_partition_over_every_condo_node_is_another_guest_partition():
    f = discover.Found(site="x", partitions={"condos": {"a", "b"}, "all_condos": {"a", "b"},
                                             "p1": {"a"}, "p2": {"b"}, "basic": {"z"}})
    discover.find(f)
    assert sorted(f.guest_partitions) == ["all_condos", "condos"] and sorted(f.condos) == ["p1", "p2"]
    one = discover.find(discover.Found(site="x", partitions={"condos": {"a"}, "solo": {"a"}}))
    assert one.condos == {"solo": {"a"}}


@pytest.mark.parametrize("partition, name", [("parish", "parish"), ("parishlab", "parish"),
                                             ("parish_lab", "parish"), ("yang2", "yang"),
                                             ("Parish-Lab", "parish"), ("x", None)])
def test_the_owner_name_in_a_partition_name(partition, name):
    assert discover.last_name(partition, ccfg.DEFAULT_PATTERNS) == name


def test_owner_candidates_prefer_a_lab_group(db):
    from nomad.config.access import access_from
    conn = sqlite3.connect(db)
    acc = access_from(NOMAD_CONFIG, log=False)
    assert discover.owner_candidates(conn, "smith", acc, "hpc1", "smith")[0] == ("jsmith", "lab group jsmith$")
    assert discover.owner_candidates(conn, "nobody", acc, "hpc1", "x") == []


def test_init_drafts_condos_and_keeps_what_is_there(db, tmp_path):
    from nomad.cli import cli
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n')
    rt = tmp_path / "report.toml"
    rt.write_text('[report.clusters.hpc1]\ntiers = { basic = "n[01-02]", condo = "c[01-04]" }\n')
    r = CliRunner().invoke(cli, ["-c", str(cfg), "usage-report", "condos", "init", "--db", str(db),
                                 "--config", str(rt)])
    assert r.exit_code == 0, r.output
    assert '[report.clusters.hpc1.condos.smith]' in r.output and 'owner = "jsmith"' in r.output
    assert "hpc2: no condos" in r.output and "not written" in r.output
    assert "[report.clusters.hpc1]\n" not in r.output.split("drafted")[1]   # already declared
    r = CliRunner().invoke(cli, ["-c", str(cfg), "usage-report", "condos", "init", "--db", str(db),
                                 "--config", str(rt), "--apply"])
    assert r.exit_code == 0, r.output
    s = ccfg.load(rt)
    assert s.clusters["hpc1"].condos["smith"].owner == "jsmith"
    assert s.clusters["hpc1"].condos["smith"].confirmed is False
    assert list(tmp_path.glob("report.toml.bak-*"))
    # The usage report still reads the file.
    from nomad.usage import config as ucfg
    assert ucfg.load(rt, "hpc1").tiers["condo"] == ["c01", "c02", "c03", "c04"]
    # Run again: nothing new to add.
    r = CliRunner().invoke(cli, ["-c", str(cfg), "usage-report", "condos", "init", "--db", str(db),
                                 "--config", str(rt), "--mask"])
    assert r.exit_code == 0 and "jsmith" not in r.output and "smith" not in r.output


# --- the figures -----------------------------------------------------------------

def test_node_hours_and_shares(data):
    s = report(data, "smith")
    assert s["nodes"]["down"] == 24.0 and s["nodes"]["in_service"] == 1416.0
    u = s["use"]
    assert u["capacity_core_hours"] == 32 * 720 + 32 * 696
    assert u["owner_core_hours"] == 160 + 320 + 40 + 256 + 16 + 16
    assert u["guest_core_hours"] == 128 + 16 + 240
    assert u["idle_core_hours"] == 45312 - 808 - 384
    assert u["owner_pct"] == 1.8 and u["guest_pct"] == 0.8 and u["idle_pct"] == 97.4


def test_guests_are_totals(data):
    g = report(data, "smith")["guests"]
    assert g == {"people": 3, "jobs": 3, "core_hours": 384.0, "gpu_hours": 0.0, "preempted": 1,
                 "over_time_limit": 1, "time_limit_days": 10.0}
    text = render.text(content.owner_doc(report(data, "smith"), ccfg.Delivery()))
    assert "g1" not in text and "g2" not in text and "g3" not in text


def test_owner_starts_and_why_they_waited(data):
    o = report(data, "smith")["owner_jobs"]
    # Six that started, and one still waiting 43 hours after it could start.
    assert o["tracked"] == 7 and o["within"] == 3 and o["within_pct"] == 42.9
    assert o["late"] == {"guests": 1, "own": 1, "other": 2}
    assert o["still_waiting"] == 1 and o["still_waiting_late"] == 1
    assert o["longest_wait_minutes"] == 43 * 60.0 and o["longest_reason"] == "other"
    assert o["longest_still_waiting"]
    st = o["summary"]
    assert st["finished"] == 6 and st["outcomes"]["failed"] == 1


def test_the_lab_elsewhere_and_its_members(data):
    s = report(data, "smith")
    where = {(e["cluster"], e["where"]): e["core_hours"] for e in s["elsewhere"]}
    assert where == {("hpc1", "other condos"): 80.0, ("hpc1", "shared nodes"): 160.0,
                     ("hpc2", "all nodes"): 40.0}
    each = {r["user"]: r for r in s["members"]["each"]}
    assert each["s1"]["condo_core_hours"] == 336 and each["s1"]["shared_core_hours"] == 160
    assert each["s1"]["other_condo_core_hours"] == 80
    assert each["s1"]["elsewhere_core_hours"] == 40 and each["s1"]["member"]
    assert each["x9"]["condo_core_hours"] == 40 and not each["x9"]["member"]
    assert each["jsmith"]["condo_core_hours"] == 432
    assert s["members"]["how"] == "lab group jsmith$"


def test_a_lab_on_another_condo_is_a_guest_there(data):
    j = report(data, "jones_lab")
    assert j["use"]["guest_core_hours"] == 80.0 and j["use"]["owner_core_hours"] == 0
    assert j["owner_jobs"]["tracked"] == 0


def test_the_trend_starts_with_the_readings(data):
    tr = report(data, "smith")["trend"]
    assert len(tr) == 12 and tr[-1]["month"] == MONTH
    assert [x["month"] for x in tr if x["measured"]] == ["2026-08", "2026-09"]
    aug = tr[-2]
    assert aug["owner_pct"] == round(100 * 320 / (64 * 744), 1)


def test_the_summary_names_every_cluster(data):
    names = [(c["name"], len(c["condos"])) for c in data["clusters"]]
    assert names == [("hpc1", 2), ("hpc2", 0)]
    hpc1 = data["clusters"][0]
    assert hpc1["label"] == "Cluster One"
    assert {"kind": "unconfirmed", "condos": ["jones_lab"]} in hpc1["flags"]
    text = render.text(content.summary_doc(data, ccfg.Delivery(), ccfg.Policy()))
    assert "## Cluster One" in text and "## hpc2" in text and "No condos." in text


def test_members_not_named_when_turned_off(db, settings):
    settings.delivery.name_members = False
    conn = sqlite3.connect(db)
    d = figures.build(conn, settings, MONTH, nomad_config=NOMAD_CONFIG, now=NOW)
    s = report(d, "smith")
    assert s["members"]["each"] == [] and s["members"]["count"] == 3
    assert "s1" not in render.text(content.owner_doc(s, settings.delivery))


def test_a_cluster_without_a_condos_table_reports_what_the_nodes_say(db):
    s = ccfg.from_dict({"report": {}})
    conn = sqlite3.connect(db)
    d = figures.build(conn, s, MONTH, now=NOW)
    assert sorted(r["condo"]["name"] for r in d["reports"]) == ["jones_lab", "smith"]
    assert all(not r["condo"]["confirmed"] for r in d["reports"])
    assert d["clusters"][0]["flags"][0]["kind"] == "not_configured"


# --- formats and mail ------------------------------------------------------------

def test_html_is_email_safe(data, settings):
    owners, summary = run.build_all(data, settings, ["html"])
    html = owners[0].html
    assert "<script" not in html and "<style" not in html and "<link" not in html
    assert "Cluster: Cluster One" in html and "s1" in html
    assert "<img" not in html


def test_pdf_when_fpdf2_is_there(data, settings):
    pytest.importorskip("fpdf")
    from nomad.condo import pdf
    raw = pdf.render(content.owner_doc(report(data, "smith"), settings.delivery))
    assert raw.startswith(b"%PDF") and len(raw) > 2000
    raw = pdf.render(content.summary_doc(data, settings.delivery, settings.policy))
    assert raw.startswith(b"%PDF")


def test_files_are_private(tmp_path, data, settings):
    owners, summary = run.build_all(data, settings, ["html", "json", "md"])
    paths = run.write(tmp_path / "out", MONTH, owners, summary, ["html", "json", "md"])
    assert (tmp_path / "out" / "hpc1" / "smith" / f"{MONTH}.json") in paths
    assert (tmp_path / "out" / "summary" / f"{MONTH}.html") in paths
    for p in paths:
        assert p.stat().st_mode & 0o077 == 0
    assert (tmp_path / "out" / "hpc1").stat().st_mode & 0o077 == 0
    facts = json.loads((tmp_path / "out" / "hpc1" / "smith" / f"{MONTH}.json").read_text())
    assert facts["condo"]["owner"] == "jsmith"


def test_review_sends_everything_to_review(monkeypatch, tmp_path, data, settings):
    sent = []
    import nomad.mail
    monkeypatch.setattr(nomad.mail, "send", lambda msg, cfg: sent.append(msg))
    owners, summary = run.build_all(data, settings, ["html"])
    res = run.send(owners, summary, settings, {}, MONTH, tmp_path)
    assert [r["status"] for r in res] == ["sent", "sent", "sent"]      # the summary: to review_to too
    assert all(m["To"] == "rc@example.edu" for m in sent)
    assert sent[0]["Subject"].startswith("[Review] Cluster One: smith condo")
    body = sent[0].get_body(("plain",)).get_content()
    assert "jsmith@example.edu, manager@example.edu" in body
    # Not again, unless asked.
    res = run.send(owners, summary, settings, {}, MONTH, tmp_path)
    assert [r["status"] for r in res] == ["already sent"] * 3


def test_after_review_only_confirmed_owners_get_theirs(monkeypatch, tmp_path, data, settings):
    sent = []
    import nomad.mail
    monkeypatch.setattr(nomad.mail, "send", lambda msg, cfg: sent.append(msg))
    settings.delivery.review_to = []
    settings.delivery.summary_to = ["rc@example.edu"]
    owners, summary = run.build_all(data, settings, ["html"])
    res = {r["key"]: r for r in run.send(owners, summary, settings, {}, MONTH, tmp_path)}
    assert res["hpc1/smith"]["status"] == "sent" and res["hpc1/smith"]["to"] == ["jsmith@example.edu"]
    assert res["hpc1/jones_lab"]["status"] == "skipped"
    assert res["summary"]["status"] == "sent"
    assert sent[0]["Cc"] == "manager@example.edu"


def test_the_command(db, tmp_path, monkeypatch):
    from nomad.cli import cli
    import nomad.mail
    sent = []
    monkeypatch.setattr(nomad.mail, "send", lambda msg, cfg: sent.append(msg))
    monkeypatch.setattr(figures, "datetime", _Frozen)
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n')
    rt = tmp_path / "report.toml"
    rt.write_text(REPORT_TOML)
    out = tmp_path / "out"
    r = CliRunner().invoke(cli, ["-c", str(cfg), "usage-report", "condos", "--db", str(db), "--config", str(rt),
                                 "--month", MONTH, "--out", str(out), "--send", "--mask"])
    assert r.exit_code == 0, r.output
    assert "Cluster One: 2 condos" in r.output and "hpc2: no condos" in r.output
    assert "smith" not in r.output and "jsmith" not in r.output and "condo 1" in r.output
    assert len(sent) == 3 and (out / "hpc1" / "smith" / f"{MONTH}.md").exists()


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def test_settings_are_checked():
    with pytest.raises(ccfg.CondoConfigError, match="not an email address"):
        ccfg.from_dict({"report": {"labs": {"review_to": ["nobody"]}}})
    with pytest.raises(ccfg.CondoConfigError, match="soft is above hard"):
        ccfg.from_dict({"report": {"policy": {"home_quota_gb": {"soft": 70, "hard": 60}}}})
    with pytest.raises(ccfg.CondoConfigError, match="both partition"):
        ccfg.from_dict({"report": {"clusters": {"c": {"condos": {"a": {"partition": "p"}, "p": {}}}}}})
    s = ccfg.from_dict({"report": {"policy": {"lab_scratch_quota_tb": 30}}})
    assert s.policy.lab_scratch_quota_tb.soft == 30 and s.policy.home_quota_gb.hard == 60


# --- what the review of 1.15.0 found ----------------------------------------------

def _mini(path, readings, jobs=(), state_col=True, site=True):
    """readings: (node, time, state); jobs: dicts of jobs columns."""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE node_state (timestamp TEXT, node_name TEXT, "
              + ("state TEXT, " if state_col else "")
              + "cpus_total, cpus_alloc INTEGER, memory_total_mb, partitions TEXT, gres TEXT"
              + (", source_site TEXT)" if site else ")"))
    for node, ts, st in readings:
        part = "own,condos" if node.startswith("c") else "basic"
        vals = [ts.isoformat(), node] + ([st] if state_col else []) + [32, 0, 64000, part, None] + (["s"] if site else [])
        c.execute(f"INSERT INTO node_state VALUES ({','.join('?' * len(vals))})", vals)
    c.execute("CREATE TABLE jobs (job_id TEXT, user_name TEXT, partition TEXT, node_list TEXT, submit_time TEXT, "
              "eligible_time TEXT, start_time TEXT, end_time TEXT, state TEXT, req_cpus, req_mem_mb, "
              "req_gpus, runtime_seconds, account TEXT, alloc_tres TEXT, alloc_gpus, source_site TEXT)")
    for j in jobs:
        row = {"job_id": None, "user_name": None, "partition": None, "node_list": None, "submit_time": None,
               "eligible_time": None, "start_time": None, "end_time": None, "state": None, "req_cpus": 1,
               "req_mem_mb": None, "req_gpus": 0, "runtime_seconds": None, "account": None, "alloc_tres": None,
               "alloc_gpus": None, "source_site": "s" if site else None, **j}
        c.execute(f"INSERT INTO jobs VALUES ({','.join('?' * len(row))})", list(row.values()))
    c.commit()
    return c


def _every(start, stop, minutes=5, nodes=("c1", "c2"), down=None, skip=None):
    out = []
    t = start
    while t < stop:
        if not (skip and skip[0] <= t < skip[1]):
            for n in nodes:
                out.append((n, t, "DOWN" if down and down[0] <= t < down[1] else "IDLE"))
        t += timedelta(minutes=minutes)
    return out


def test_a_gap_in_the_readings_is_not_read():
    c = _mini(":memory:", _every(datetime(2026, 9, 1), datetime(2026, 10, 1),
                                 skip=(datetime(2026, 9, 10), datetime(2026, 9, 20)),
                                 down=(datetime(2026, 9, 25), datetime(2026, 9, 26))))
    nm = figures.node_months(c, "s", ["c1"], ["2026-08", "2026-09"])
    sep = nm[("c1", "2026-09")]
    assert sep.hours == 720 and abs(sep.not_read - 240) < 0.2 and abs(sep.classes["down"] - 24) < 0.2


def test_a_site_that_stopped_reporting_leaves_hours_not_read():
    c = _mini(":memory:", _every(datetime(2026, 9, 1), datetime(2026, 9, 25)))
    sep = figures.node_months(c, "s", ["c1"], ["2026-09"])[("c1", "2026-09")]
    assert sep.hours == 720 and abs(sep.not_read - 6 * 24) < 0.2
    # A node retired while the others go on is gone from then.
    c = _mini(":memory:", _every(datetime(2026, 9, 1), datetime(2026, 10, 1), nodes=("c2",))
              + _every(datetime(2026, 9, 1), datetime(2026, 9, 15), nodes=("c1",)))
    sep = figures.node_months(c, "s", ["c1"], ["2026-09"])[("c1", "2026-09")]
    assert abs(sep.hours - 14 * 24) < 0.2 and sep.not_read < 0.2


def test_no_state_column_means_in_service():
    c = _mini(":memory:", _every(datetime(2026, 9, 1), datetime(2026, 10, 1)), state_col=False)
    sep = figures.node_months(c, "s", ["c1"], ["2026-09"])[("c1", "2026-09")]
    assert sep.out == 0 and abs(sep.in_service - 720) < 0.2


def test_a_job_submitted_on_the_30th_that_starts_on_the_1st_is_judged():
    jobs = [{"job_id": "1", "user_name": "o1", "partition": "own", "node_list": "c1",
             "submit_time": t(30, 22), "start_time": t(1, 3, 0, 10), "end_time": t(1, 4, 0, 10),
             "state": "COMPLETED", "runtime_seconds": 3600}]
    c = _mini(":memory:", _every(datetime(2026, 9, 1), datetime(2026, 10, 2)), jobs)
    s = ccfg.from_dict({"report": {"clusters": {"s": {"condos": {"own": {"owner": "o1"}}}}}})
    d = figures.build(c, s, MONTH, now=datetime(2026, 10, 2, 7))
    o = d["reports"][0]["owner_jobs"]
    assert o["tracked"] == 1 and o["within"] == 0 and o["longest_wait_minutes"] == 300.0


def test_unknown_jobs_end_when_nomad_saw_them_gone():
    j = figures.make_job({"job_id": "1", "user_name": "u", "state": "UNKNOWN", "start_time": t(5),
                          "end_time": t(5, 6), "req_cpus": 32, "runtime_seconds": None},
                         datetime(2026, 9, 1), NOW)
    assert j.end == datetime(2026, 9, 5, 6) and not j.ended


def test_text_in_number_columns_is_not_a_number():
    j = figures.make_job({"job_id": "1", "user_name": "u", "state": "COMPLETED", "start_time": t(5),
                          "end_time": t(5, 1), "req_cpus": "NaN", "req_gpus": "N/A", "alloc_gpus": "x"},
                         datetime(2026, 9, 1), NOW)
    assert j.cpus == 0 and j.gpus == 0
    rows = [("c1", datetime(2026, 9, 1), "IDLE")]
    c = _mini(":memory:", rows)
    c.execute("UPDATE node_state SET cpus_total = 'N/A', memory_total_mb = 'unknown'")
    found = discover.read_nodes(c, "s", until=datetime(2026, 9, 2))
    assert found.nodes["c1"].cores == 0


def test_gpus_from_the_request_where_accounting_has_none():
    j = figures.make_job({"job_id": "1", "user_name": "u", "state": "COMPLETED", "start_time": t(5),
                          "end_time": t(5, 1), "req_gpus": 2, "alloc_gpus": 0, "alloc_tres": "cpu=4,mem=8G"},
                         datetime(2026, 9, 1), NOW)
    assert j.gpus == 2
    j = figures.make_job({"job_id": "1", "user_name": "u", "state": "COMPLETED", "start_time": t(5),
                          "end_time": t(5, 1), "req_gpus": 2, "alloc_tres": "cpu=4,gres/gpu=1"},
                         datetime(2026, 9, 1), NOW)
    assert j.gpus == 1


def test_a_non_members_row_holds_only_their_jobs_in_the_partition(db, settings, tmp_path):
    c = sqlite3.connect(db)
    for i in range(5):
        c.execute("INSERT INTO jobs (job_id, user_name, partition, node_list, submit_time, start_time, end_time, "
                  "state, req_cpus, req_gpus, runtime_seconds, alloc_tres, source_site) VALUES "
                  "(?, 'x9', 'condos', 'c02', ?, ?, ?, 'FAILED', 1, 1, 3600, 'cpu=1,gres/gpu=1', 'hpc1')",
                  (f"g{i}", t(16, i), t(16, i), t(16, i + 1)))
    c.commit()
    d = figures.build(c, settings, MONTH, nomad_config=NOMAD_CONFIG, now=NOW)
    x9 = next(r for r in report(d, "smith")["members"]["each"] if r["user"] == "x9")
    assert x9["jobs_ended"] == 1 and x9["failed"] == 0 and x9["gpu_hours"] == 0


def test_a_sites_own_database_is_this_cluster(tmp_path):
    from nomad.cli import cli
    path = tmp_path / "nomad.db"
    _mini(path, _every(datetime(2026, 9, 1), datetime(2026, 10, 1), minutes=60), site=False).close()
    rt = tmp_path / "report.toml"
    r = CliRunner().invoke(cli, ["usage-report", "condos", "init", "--db", str(path), "--config", str(rt), "--apply"])
    assert r.exit_code == 0, r.output
    s = ccfg.load(rt)
    assert list(s.clusters[discover.LOCAL].condos) == ["own"]
    d = figures.build(sqlite3.connect(path), s, MONTH, now=NOW)
    assert d["clusters"][0]["name"] == discover.LOCAL and not any(
        f["kind"] == "not_configured" for f in d["clusters"][0]["flags"])


def test_a_partial_run_writes_and_sends_no_summary(db, tmp_path, monkeypatch):
    from nomad.cli import cli
    import nomad.mail
    sent = []
    monkeypatch.setattr(nomad.mail, "send", lambda msg, cfg: sent.append(msg))
    monkeypatch.setattr(figures, "datetime", _Frozen)
    rt = tmp_path / "report.toml"
    rt.write_text(REPORT_TOML)
    out = tmp_path / "out"
    args = ["usage-report", "condos", "--db", str(db), "--config", str(rt), "--month", MONTH, "--out", str(out)]
    r = CliRunner().invoke(cli, args + ["--condo", "smith", "--send"])
    assert r.exit_code == 0, r.output
    assert not (out / "summary" / f"{MONTH}.html").exists() and len(sent) == 1
    r = CliRunner().invoke(cli, args + ["--send"])
    assert r.exit_code == 0 and "sent summary" in r.output
    r = CliRunner().invoke(cli, args + ["--cluster", "nosuch"])
    assert r.exit_code != 0 and "no cluster nosuch" in r.output


def test_typos_in_settings_are_refused():
    with pytest.raises(ccfg.CondoConfigError, match="did you mean 'review_to'"):
        ccfg.from_dict({"report": {"labs": {"reveiw_to": ["rc@example.edu"]}}})
    with pytest.raises(ccfg.CondoConfigError, match="did you mean 'confirmed'"):
        ccfg.from_dict({"report": {"clusters": {"c": {"condos": {"a": {"confirmd": False}}}}}})
    s = ccfg.from_dict({"report": {"labs": {"send_from": ""}, "clusters": {"c": {"condos": {"a": {"email": " "}}}}}})
    assert s.delivery.send_from is None and s.clusters["c"].condos["a"].email is None


def test_masked_errors_name_no_condo(tmp_path, db):
    from nomad.cli import cli
    rt = tmp_path / "report.toml"
    rt.write_text('[report.clusters.hpc1.condos.smith]\nownr = "jsmith"\n')
    r = CliRunner().invoke(cli, ["usage-report", "condos", "--db", str(db), "--config", str(rt), "--mask"])
    assert r.exit_code != 0 and "smith" not in r.output


def test_init_never_declares_a_table_twice(tmp_path, db):
    from nomad.cli import cli
    rt = tmp_path / "report.toml"
    rt.write_text('[report.clusters]\nhpc1.label = "One"\n')
    r = CliRunner().invoke(cli, ["usage-report", "condos", "init", "--db", str(db), "--config", str(rt),
                                 "--cluster", "hpc1", "--apply"])
    assert r.exit_code == 0, r.output
    s = ccfg.load(rt)
    assert s.clusters["hpc1"].label == "One" and "smith" in s.clusters["hpc1"].condos
    assert "hpc2" in s.clusters and not s.clusters["hpc2"].condos     # its section, no condos drafted
    new = tmp_path / "new.toml"
    r = CliRunner().invoke(cli, ["usage-report", "condos", "init", "--db", str(db), "--config", str(new), "--apply"])
    assert r.exit_code == 0 and new.stat().st_mode & 0o077 == 0
