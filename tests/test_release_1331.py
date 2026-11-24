# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.33.1: the lab check's PIs are their own labs' (a shared condo's
owners don't answer for each other's), not_sites leaves a teaching server's
groups out, and each person's last job is shown, with one answer for many."""
from __future__ import annotations

from datetime import timedelta

import pytest

from nomad import lab_check as L
from tests.test_release_1330 import CONFIG, NOW, REPORT, hub


@pytest.fixture(autouse=True)
def not_root(monkeypatch):
    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)


@pytest.fixture
def world(tmp_path):
    conn = hub(tmp_path / "combined.db")
    rec = L.open_records(tmp_path / "lab_check.db", write=True)
    yield conn, rec, tmp_path
    conn.close()
    rec.close()


def _with(**check):
    return {"console": {"labs": {**CONFIG["console"]["labs"],
                                 "check": {**CONFIG["console"]["labs"]["check"], **check}}}}


def test_a_shared_condos_owners_answer_only_for_their_own_labs(world):
    conn, rec, _ = world
    got = {lab.group: lab for lab in L.labs(conn, CONFIG, REPORT, now=NOW)}
    # chennovak: owners tchen and lnovak, groups tchen$ and lnovak$
    assert got["tchen$"].leaders == ("tchen",) and got["lnovak$"].leaders == ("lnovak",)
    assert L.led_by(conn, CONFIG, REPORT, "lnovak", now=NOW) == ["lnovak$"]
    assert "tchen$" in L.led_by(conn, CONFIG, REPORT, "tchen", now=NOW)
    assert "lnovak$" not in L.led_by(conn, CONFIG, REPORT, "tchen", now=NOW)


def test_a_condo_group_named_for_no_one_is_still_its_owners(world):
    conn, rec, _ = world
    conn.executemany("INSERT INTO group_membership (username, group_name, gid, cluster, collected_at, has_account, "
                     "source_site) VALUES (?, 'chemlab$', 1, 'hpc1', ?, 1, 'hpc1')",
                     [(u, (NOW - timedelta(hours=1)).isoformat()) for u in ("pat", "quinn")])
    conn.commit()
    from nomad.condo.config import from_dict
    report = from_dict({"report": {"labs": {"mail_domain": "example.edu"}, "clusters": {"hpc1": {"condos": {
        "chem": {"owner": ["rchem", "schem"], "members": "group:chemlab$"}}}}}})
    got = {lab.group: lab for lab in L.labs(conn, CONFIG, report, now=NOW)}
    assert got["chemlab$"].leaders == ("rchem", "schem")


def test_not_sites_leaves_a_teaching_servers_groups_out(world):
    conn, rec, _ = world
    t = (NOW - timedelta(hours=1)).isoformat()
    conn.executemany("INSERT INTO group_membership (username, group_name, gid, cluster, collected_at, has_account, "
                     "source_site) VALUES (?, ?, 1, 'interactive1', ?, 1, 'interactive1')",
                     [("s1", "rames$", t), ("s2", "rames$", t), ("tina", "tina$", t), ("s3", "tina$", t)])
    conn.commit()
    every = {lab.group: lab for lab in L.labs(conn, CONFIG, REPORT, now=NOW)}
    assert {"s1", "s2"} <= set(every["rames$"].members) and "tina$" in every
    cfg = _with(not_sites=["interactive1"])
    assert L.settings(cfg).not_sites == ("interactive1",)
    got = {lab.group: lab for lab in L.labs(conn, cfg, REPORT, now=NOW)}
    assert "tina$" not in got                                          # only on interactive1: not asked about
    assert set(got["rames$"].members) == {"ann", "bo"}
    assert got["rames$"].members["bo"] == {"hpc1": True}           # interactive1 isn't where
    # and what the answers leave to do leaves it out too
    rnd = L.round_of(L.settings(cfg), NOW.date())
    L.record(rec, rnd, got["rames$"], "bo", "left", "keep", by="rames")
    sites = {i["site"] for i in L.todo(conn, rec, cfg, REPORT, now=NOW)["items"] if i["kind"] == "remove"}
    assert sites == {"hpc1"}
    assert L.settings(_with(not_sites=[1])).problems


def test_each_persons_last_job_and_who_hasnt_run_one_in_a_year(world):
    conn, rec, _ = world
    t = (NOW - timedelta(hours=1)).isoformat()
    conn.executemany("INSERT INTO group_membership (username, group_name, gid, cluster, collected_at, has_account, "
                     "first_seen, source_site) VALUES (?, 'rames$', 1, 'hpc1', ?, 1, ?, 'hpc1')",
                     [("cal", t, None), ("new", t, (NOW - timedelta(days=20)).isoformat())])
    conn.executemany("INSERT INTO jobs VALUES (?, ?, 'basic', ?, ?)",
                     [("20", "cal", (NOW - timedelta(days=500)).isoformat(), "hpc1"),
                      ("21", "bo", (NOW - timedelta(days=3)).isoformat(), "interactive1")])
    conn.commit()
    v = L.view(conn, rec, CONFIG, REPORT, "rames$", now=NOW)
    q = {x["netid"]: x for x in v["questions"]}
    assert q["ann"]["last_job"] == (NOW - timedelta(days=10)).date().isoformat()
    assert q["cal"]["last_job"] == (NOW - timedelta(days=500)).date().isoformat() and q["cal"]["quiet"]
    assert q["new"]["last_job"] is None and not q["new"]["quiet"]      # joined 20 days ago
    assert q["bo"]["last_job"] == (NOW - timedelta(days=3)).date().isoformat() and not q["bo"]["quiet"]
    members = [x["netid"] for x in v["questions"] if x["kind"] == "member"]
    assert members == ["bo", "ann", "cal", "new"]                      # most recent first, none on record last
    # leaving interactive1 out leaves its jobs out too
    v = L.view(conn, rec, _with(not_sites=["interactive1"]), REPORT, "rames$", now=NOW)
    q = {x["netid"]: x for x in v["questions"]}
    assert q["bo"]["last_job"] is None and q["bo"]["quiet"]
    assert v["quiet_days"] == 365 and v["not_sites"] == ["interactive1"]


def test_one_answer_for_many_all_or_none(world):
    conn, rec, _ = world
    t = (NOW - timedelta(hours=1)).isoformat()
    conn.executemany("INSERT INTO group_membership (username, group_name, gid, cluster, collected_at, has_account, "
                     "source_site) VALUES (?, 'rames$', 1, 'hpc1', ?, 1, 'hpc1')", [("cal", t), ("dan", t)])
    conn.commit()
    s = L.settings(CONFIG, REPORT)
    rnd = L.round_of(s, NOW.date())
    lab = L.lab_for(conn, CONFIG, REPORT, "rames$", now=NOW)
    with pytest.raises(L.CheckError):
        L.record_many(rec, rnd, lab, ["cal", "dan"], "left")                    # left needs its data
    with pytest.raises(L.CheckError):
        L.record_many(rec, rnd, lab, ["cal", "yu"], "left", "ask")              # yu isn't Ames's
    assert not L.answers(rec, rnd.id, "rames$")                                # none kept
    with pytest.raises(L.CheckError):
        L.record_many(rec, rnd, lab, [], "here")
    rows = L.record_many(rec, rnd, lab, ["cal", "dan", "cal"], "left", "ask", by="rames", as_role="pi")
    assert [r["netid"] for r in rows] == ["cal", "dan"]
    kept = {r["netid"]: r for r in L.answers(rec, rnd.id, "rames$")}
    assert kept["dan"]["answer"] == "left" and kept["dan"]["data"] == "ask" and kept["cal"]["as_role"] == "pi"
    assert any('"answers"' in r[0] for r in rec.execute("SELECT what FROM lab_check_log"))
    items = L.todo(conn, rec, CONFIG, REPORT, now=NOW)["items"]
    assert {(i["kind"], i["netid"]) for i in items} >= {("remove", "cal"), ("data", "dan")}
