# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Everyone at once (nomad.edu.population), and the honesty fixes it rests on:
jobs matched to their measurements by site as well as job ID, and nothing
scored that was not measured."""

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad.edu import population as popmod
from nomad.edu.explain import load_job
from nomad.edu.progress import summary_join, user_trajectory
from nomad.edu.scoring import score_job

NOW = datetime.now()


def _ago(days, hours=0):
    return (NOW - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")


def _make_db(path):
    """A combined database in miniature: every table carries source_site."""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE jobs (job_id TEXT, user_name TEXT, partition TEXT, state TEXT,
            submit_time TEXT, start_time TEXT, end_time TEXT, req_cpus INTEGER,
            req_mem_mb INTEGER, req_gpus INTEGER, req_time_seconds INTEGER,
            runtime_seconds INTEGER, wait_time_seconds INTEGER, exit_code INTEGER,
            source_site TEXT);
        CREATE TABLE job_summary (job_id TEXT, peak_cpu_percent REAL, peak_memory_gb REAL,
            avg_cpu_percent REAL, avg_memory_gb REAL, avg_io_wait_percent REAL,
            total_nfs_read_gb REAL, total_nfs_write_gb REAL, total_local_read_gb REAL,
            total_local_write_gb REAL, nfs_ratio REAL, used_gpu INTEGER, avg_gpu_util REAL,
            health_score REAL, source_site TEXT);
        CREATE TABLE group_membership (group_name TEXT, username TEXT, source_site TEXT);
    """)
    return c


def _job(c, job_id, user, site, end, cpu=None, gpus=0, used_gpu=None, state="COMPLETED"):
    c.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (job_id, user, "basic", state, end, end, end, 8, 16000, gpus,
               4 * 3600, 3 * 3600, 60, 0, site))
    if cpu is not None:
        c.execute("INSERT INTO job_summary VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (job_id, cpu, 10, cpu, 8, 1, 0.5, 0.5, 5, 5, 0.1, used_gpu, None, 0.9, site))


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "combined.db")
    c = _make_db(path)
    # The same job ID at two sites, measured very differently.
    _job(c, "100", "alice", "spydur", _ago(2), cpu=90)
    _job(c, "100", "bob", "arachne", _ago(2), cpu=10)
    # alice: more measured jobs across the window, and one never measured.
    _job(c, "101", "alice", "spydur", _ago(40), cpu=30)
    _job(c, "102", "alice", "spydur", _ago(39), cpu=30)
    _job(c, "103", "alice", "spydur", _ago(3), cpu=90)
    _job(c, "104", "alice", "spydur", _ago(1))                      # no summary
    # A GPU job whose GPU use nobody measured.
    _job(c, "200", "bob", "arachne", _ago(5), cpu=80, gpus=1, used_gpu=None)
    # Outside the window, and not finished.
    _job(c, "300", "alice", "spydur", _ago(200), cpu=10)
    _job(c, "301", "alice", "spydur", _ago(1), cpu=10, state="RUNNING")
    for g, u in [("lab$", "alice"), ("lab$", "bob"), ("lab$", "carol"),
                 ("people", "alice"), ("people", "bob"), ("empty$", "dave")]:
        c.execute("INSERT INTO group_membership VALUES (?,?,?)", (g, u, "spydur"))
    c.commit()
    c.close()
    popmod._CACHE.clear()
    return path


# ── The fixes underneath ───────────────────────────────────────────────


def test_jobs_meet_their_own_measurements(db):
    c = sqlite3.connect(db)
    assert "source_site" in summary_join(c)
    c.close()
    pop = popmod.population(db)
    # bob's job 100 ran at 10% CPU on arachne (score 11) and job 200 at 80%
    # (88). Matched by job ID alone, job 100 would also take alice's 90% from
    # spydur, and each would count twice.
    assert pop.people["bob"].dimensions["cpu"] == pytest.approx(49.5)
    assert pop.people["bob"].scored_jobs == 2


def test_io_without_data_is_not_scored():
    fp = score_job({"req_cpus": 1, "req_time_seconds": 3600, "runtime_seconds": 1800}, {})
    assert fp.dimensions["io"].applicable is False
    assert "not measured" in fp.dimensions["io"].detail
    # The job collector's old placeholder -- nfs_ratio 0.0 with no NFS traffic
    # measured -- is not a measurement either.
    placeholder = score_job({"req_cpus": 1}, {"nfs_ratio": 0.0, "total_local_write_gb": 5.0,
                                              "avg_cpu_percent": 50})
    assert placeholder.dimensions["io"].applicable is False
    measured = score_job({"req_cpus": 1}, {"nfs_ratio": 0.8, "total_nfs_write_gb": 8.0,
                                           "total_local_write_gb": 2.0})
    assert measured.dimensions["io"].applicable and measured.dimensions["io"].score < 65


def test_unmeasured_gpu_is_not_called_unused():
    fp = score_job({"req_gpus": 1}, {"avg_cpu_percent": 50})
    assert fp.dimensions["gpu"].applicable is False
    assert "never" not in fp.dimensions["gpu"].detail


def test_explain_finds_a_job_by_site_in_a_combined_database(db):
    assert load_job(db, "100", "arachne")["user_name"] == "bob"
    with pytest.raises(ValueError, match="arachne"):
        load_job(db, "100")


# ── Everyone ───────────────────────────────────────────────────────────


def test_population_counts_every_finished_job_and_scores_measured_ones(db):
    pop = popmod.population(db)
    alice = pop.people["alice"]
    assert alice.jobs == 5              # 100, 101, 102, 103, 104; not 300 or 301
    assert alice.scored_jobs == 4       # 104 was never measured
    assert alice.sites == ["spydur"]
    assert pop.people["bob"].sites == ["arachne"]
    assert "gpu" not in pop.people["bob"].dimensions
    assert set(pop.sites) == {"arachne", "spydur"}


def test_list_and_person_page_agree(db):
    alice = popmod.population(db).people["alice"]
    traj = user_trajectory(db, "alice", days=90)
    assert traj.total_jobs == alice.scored_jobs
    assert alice.change is not None
    assert traj.overall_improvement == pytest.approx(alice.change, abs=0.2)
    assert alice.change > 0             # 30% CPU in the first window, 90% lately


def test_population_is_kept_until_the_database_changes(db):
    first = popmod.population(db)
    assert popmod.population(db) is first
    time.sleep(0.01)
    c = sqlite3.connect(db)
    _job(c, "105", "alice", "spydur", _ago(1), cpu=50)
    c.commit()
    c.close()
    os.utime(db)
    again = popmod.population(db)
    assert again is not first and again.people["alice"].scored_jobs == 5


# ── Groups ─────────────────────────────────────────────────────────────


def test_group_report(db):
    gr = popmod.group_report(db, "lab$")
    assert gr.members == 3 and gr.members_with_jobs == 2 and gr.members_scored == 2
    assert gr.without_jobs == ["carol"]
    assert gr.overall.n == 2
    assert gr.people[0].overall <= gr.people[1].overall     # lowest first
    assert dict(gr.issues).get("cpu") == 1                   # bob, at ~11
    assert popmod.group_report(db, "nobody-here") is None


def test_group_cards_keep_research_groups(db):
    cards = popmod.group_cards(db, pattern=r"\$$", exclude=["people"])
    assert [c.group for c in cards] == ["lab$", "empty$"]    # busiest first
    lab, empty = cards
    assert lab.members_with_jobs == 2 and lab.median_overall is not None
    assert empty.members_with_jobs == 0 and empty.median_overall is None


def test_research_group_rules():
    assert popmod.research_group("lab$", r"\$$", ["people"])
    assert not popmod.research_group("people", None, ["people"])
    assert not popmod.research_group("chem", r"\$$", [])
    assert popmod.research_group("chem", None, [])


def test_cli_group_report(db):
    from nomad.cli import cli
    runner = CliRunner()
    out = runner.invoke(cli, ["edu", "report", "lab$", "--db", db])
    assert out.exit_code == 0, out.output
    assert "Group Report" in out.output and "No jobs in this period: 1 member" in out.output
    data = json.loads(runner.invoke(cli, ["edu", "report", "lab$", "--db", db, "--json"]).output)
    assert data["members_with_jobs"] == 2 and data["without_jobs"] == ["carol"]


# ── One person: nomad edu me and the Console's My Activity ─────────────


def test_me_scores_only_measured_jobs_and_agrees_with_the_list(db):
    from nomad.edu.insights import user_insights
    alice = popmod.population(db).people["alice"]
    ui = user_insights(db, "alice", cluster_capacities=[])
    assert ui.finished_job_count == 5           # every finished job counted
    assert ui.job_count == 4 == alice.scored_jobs   # 104 was never measured
    assert ui.total_job_count == 6              # any state, incl. the running one
    # The same overall, dimensions and change as alice's line in a list.
    assert ui.overall_score == alice.overall
    assert ui.dimensions == alice.dimensions
    assert ui.overall_change == alice.change
    assert ui.overall_trajectory == popmod.trend(alice.change)
    for issue in ui.issues:
        if issue.kind == "dimension":
            assert issue.total_applicable <= 4


def test_me_with_nothing_measured_has_no_score(db):
    from nomad.edu.insights import format_user_insights, user_insights
    c = sqlite3.connect(db)
    _job(c, "500", "erin", "spydur", _ago(2))
    _job(c, "501", "erin", "spydur", _ago(9))
    c.commit(); c.close()
    ui = user_insights(db, "erin", cluster_capacities=[])
    assert ui.finished_job_count == 2 and ui.job_count == 0
    assert ui.overall_score is None and ui.dimensions == {} and ui.issues == []
    assert ui.overall_trajectory == "too_few_weeks"
    text = format_user_insights(ui)
    assert "2 jobs" in text and "none of them measured" in text
    assert "0 / 100" not in text and "0.0" not in text


def test_me_text_says_what_was_scored(db):
    from nomad.edu.insights import format_user_insights, user_insights
    ui = user_insights(db, "alice", cluster_capacities=[])
    text = format_user_insights(ui)
    assert "6 jobs in the last 90 days, 4 measured and scored" in text
    assert f"Overall score: {ui.overall_score:.0f} / 100" in text
    detailed = format_user_insights(ui, detailed=True)
    assert "By dimension (average over measured jobs): CPU" in detailed
