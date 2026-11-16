"""nomad 1.48.2: the term reports as the first run on a real hub read them."""
import sqlite3
import sys
from datetime import datetime

import pytest

from nomad.condo import config as ccfg, term
from nomad.condo.figures import make_job

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from test_term_reports import FALL, NOMAD_CONFIG, NOW, REPORT_TOML, _cli, build, lab, t  # noqa: E402


def test_a_little_use_is_under_1_not_0():
    assert term.hours(0.04) == "under 1"
    assert term.hours(0.99) == "under 1"
    assert term.hours(0) == "0"
    assert term.hours(1.4) == "1"
    assert term.hours(208806.36) == "208,806"
    assert term.hours(None) == "-"


def _job(jid, state, start, end, cpus=1):
    return make_job({"job_id": jid, "user_name": "kim", "partition": "basic", "node_list": "n01",
                     "submit_time": start, "start_time": start, "end_time": end, "state": state,
                     "req_cpus": cpus, "runtime_seconds": 0, "source_site": "hpc1"},
                    FALL[0], NOW)


def test_what_the_failed_share_is_of():
    # As on the hub: 6 ended, 1 cancelled; 3 failed and 1 timed out of the 5 that ran to an end.
    jobs = [_job("1", "FAILED", t(9, 1), t(9, 1, 1)), _job("2", "FAILED", t(9, 2), t(9, 2, 1)),
            _job("3", "FAILED", t(9, 3), t(9, 3, 1)), _job("4", "TIMEOUT", t(9, 4), t(9, 4, 1)),
            _job("5", "COMPLETED", t(9, 5), t(9, 5, 1)), _job("6", "CANCELLED by 1234", t(9, 6), t(9, 6, 1))]
    u = term._use(jobs, FALL[0], FALL[1], {})
    assert (u["jobs_ended"], u["judged"], u["failed"], u["failed_pct"]) == (6, 5, 4, 80.0)
    assert term.failed_line(u) == "6 jobs ended; 4 failed of the 5 that ran to an end (80%)"
    assert term._ended_tile(u) == "4 failed, 1 cancelled"
    # None stopped: the share is of them all.
    u = term._use(jobs[:5], FALL[0], FALL[1], {})
    assert term.failed_line(u) == "5 jobs ended, 4 failed (80%)"
    assert term._ended_tile(u) == "4 of 5 failed"
    # Many: with separators; and none at all.
    many = {"jobs_ended": 53430, "judged": 52141, "failed": 2949, "failed_pct": 5.656,
            "outcomes": {"cancelled": 1289}}
    assert term.failed_line(many) == "53,430 jobs ended; 2,949 failed of the 52,141 that ran to an end (5.7%)"
    assert term._ended_tile(many) == "2,949 failed, 1,289 cancelled"
    assert term.failed_line({"jobs_ended": 0, "judged": 0, "failed": 0, "failed_pct": None}) == "no job ended"
    assert term.failed_line({"jobs_ended": 2, "judged": 0, "failed": 0, "failed_pct": None}) == \
        "2 jobs ended; none ran to an end"


def test_term_min_space():
    assert ccfg.Delivery().term_min_space == 1_000_000_000
    read = ccfg.from_dict({"report": {"labs": {"term_min_space": "500 MB"}}}, None).delivery.term_min_space
    assert read == 500_000_000
    assert ccfg.from_dict({"report": {"labs": {"term_min_space": "2G"}}}, None).delivery.term_min_space == 2 * 1024 ** 3
    assert ccfg.from_dict({"report": {"labs": {"term_min_space": 0}}}, None).delivery.term_min_space == 0
    for bad in ("lots", -1, True):
        with pytest.raises(ccfg.CondoConfigError, match="term_min_space"):
            ccfg.from_dict({"report": {"labs": {"term_min_space": bad}}}, None)


@pytest.fixture
def hub(tmp_path):
    """The fixture's labs, and quinn$ (no job all term) holding 25 KB, then 5 GB."""
    path = tmp_path / "combined.db"
    c = build(path)
    c.execute("INSERT INTO storage_owners (timestamp, server, path, kind, name, used_bytes) "
              "VALUES (?, 'fs1', '/data', 'user', 'quinn', 25000)", (t(12, 1),))
    c.commit()
    c.close()
    return path


def _build(path, toml=REPORT_TOML, **kw):
    rt = path.parent / "report.toml"
    rt.write_text(toml)
    conn = sqlite3.connect(path)
    try:
        return term.build(conn, ccfg.load(rt), FALL[0], FALL[1], name=FALL[2], nomad_config=NOMAD_CONFIG,
                          now=NOW, **kw)
    finally:
        conn.close()


def test_a_lab_with_no_job_gets_a_report_only_for_the_space_it_holds(hub):
    d = _build(hub)
    assert {"lab": "quinn$", "name": "quinn$", "why": "quiet"} in d["left_out"]
    assert term.quiet_words(d) == "hold under 1 GB" and term.quiet_words(d, 1) == "holds under 1 GB"
    # Every lab with space, as 1.48.0 did: term_min_space = 0.
    d = _build(hub, REPORT_TOML.replace("[report.labs]\n", "[report.labs]\nterm_min_space = 0\n"))
    assert lab(d, "quinn$")["storage"]["paths"][0]["used_bytes"] == 25000
    assert term.quiet_words(d) == "hold no space"
    # Asked for by name, it is written whatever it holds.
    assert lab(_build(hub, only=["quinn$"]), "quinn$")
    # 5 GB: a report, for the space.
    c = sqlite3.connect(hub)
    c.execute("UPDATE storage_owners SET used_bytes = 5000000000 WHERE name = 'quinn'")
    c.commit()
    c.close()
    f = lab(_build(hub), "quinn$")
    assert f["total"]["people"] == 0
    labs, _ = term.build_all({**_build(hub)}, ccfg.load(hub.parent / "report.toml"), ["md"])
    text = next(b.text for b in labs if "quinn$" in b.text)
    assert "No one in your lab ran a job" in text


def test_someone_in_two_labs(hub, tmp_path):
    d = _build(hub)
    assert d["shared_people"] == 0
    c = sqlite3.connect(hub)
    c.execute("INSERT INTO group_membership VALUES ('pat', 'olive$', ?, 1, 'hpc1', 'hpc1')", (NOW.isoformat(),))
    c.commit()
    c.close()
    d = _build(hub)
    assert d["shared_people"] == 1
    # pat's jobs count in both labs.
    assert lab(d, "olive$")["total"]["core_hours"] == lab(d, "kim$")["members"]["each"][0]["core_hours"] + 80
    _, summary = term.build_all(d, ccfg.load(hub.parent / "report.toml"), ["md"])
    assert ("1 person is in more than one of these labs and counts in each, so the rows add up to more than "
            "the clusters' use.") in summary.text


def test_the_command_says_it(hub, tmp_path, monkeypatch):
    r = _cli(hub, tmp_path, "--out", str(tmp_path / "o"), monkeypatch=monkeypatch)
    assert "kim$ (kim): 3 of 3 ran jobs; 368 core-hours, 5 GPU-hours; 4 jobs ended, 2 failed (50%);" in r.output
    assert "no report: 1 lab with no job all term that holds under 1 GB: quinn$ (--lab writes one anyway)" in r.output
    assert "more than one of these labs" not in r.output


def test_under_1_in_a_lab_report(tmp_path):
    path = tmp_path / "combined.db"
    c = build(path)
    # quinn$: a minute's work on one core all term.
    c.execute("INSERT INTO jobs (job_id, user_name, partition, node_list, submit_time, start_time, end_time, "
              "state, req_cpus, req_mem_mb, req_gpus, runtime_seconds, account, alloc_tres, source_site) "
              "VALUES ('9', 'quinn', 'basic', 'n01', ?, ?, ?, 'COMPLETED', 1, 1000, 0, 60, 'default', "
              "'cpu=1,mem=1000M,node=1', 'hpc1')", (t(10, 5), t(10, 5), datetime(2026, 10, 5, 0, 1).isoformat()))
    c.commit()
    c.close()
    d = _build(path)
    f = lab(d, "quinn$")
    assert 0 < f["total"]["core_hours"] < 1
    labs, summary = term.build_all(d, ccfg.load(path.parent / "report.toml"), ["md"])
    text = next(b.text for b in labs if "# quinn$" in b.text)
    assert "- Core-hours: under 1" in text and "- Jobs ended: 1 (0 of 1 failed)" in text
    row = next(r for r in summary.text.splitlines() if r.startswith("| quinn$"))
    assert [c.strip() for c in row.split("|")[1:5]] == ["quinn$", "quinn", "1 of 1", "under 1"]
