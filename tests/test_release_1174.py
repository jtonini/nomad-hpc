# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.17.4: a demo the Console runs on, with a cast (nomad demo --console)."""
import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture()
def made(tmp_path):
    from nomad.demo import make_console_demo
    return make_console_demo(tmp_path / "demo", n_jobs=60, days=3, seed=3)


def test_the_demo_has_its_cast_and_says_it_is_a_demo(made):
    from nomad.demo_cast import CAST, is_demo_db
    assert is_demo_db(made["db"])
    c = sqlite3.connect(made["db"])
    cast = json.loads(c.execute("SELECT value FROM demo_info WHERE key = 'cast'").fetchone()[0])
    assert [p["user"] for p in cast] == [p.user for p in CAST] == ["bob", "eve", "grace", "henry", "olive", "adam"]
    assert {p["role"] for p in cast} == {"viewer", "operator", "admin"}
    lab = {r[0] for r in c.execute("SELECT username FROM group_membership WHERE group_name = 'grace$'")}
    assert lab == {"alice", "charlie", "eve", "grace"}
    now = c.execute("SELECT user_name, state, pending_reason FROM jobs WHERE end_time IS NULL ORDER BY job_id").fetchall()
    assert ("bob", "RUNNING", None) in now and ("bob", "PENDING", "Priority") in now
    assert c.execute("SELECT COUNT(*) FROM node_state WHERE cluster = 'demo-cluster'").fetchone()[0] > 0


def test_its_nomad_toml_makes_grace_a_pi_and_henry_an_instructor(made):
    from nomad.config import read_toml
    from nomad.config.access import access_from
    cfg = read_toml(made["toml"])
    acc = access_from(cfg, log=False)
    assert acc.role("adam") == "admin" and acc.role("olive") == "operator" and acc.role("bob") is None
    assert acc.group_pattern == "{netid}$"
    assert cfg["console"]["courses"]["groups"]["cs101"]["instructors"] == ["henry"]


def test_it_replaces_its_own_demo_but_never_another_database(tmp_path, made):
    from nomad.demo import make_console_demo
    again = make_console_demo(made["dir"], n_jobs=20, days=1, seed=4)
    assert again["db"] == made["db"]
    other = tmp_path / "real"
    other.mkdir()
    sqlite3.connect(other / "nomad_demo.db").execute("CREATE TABLE jobs (job_id TEXT)").connection.commit()
    with pytest.raises(ValueError, match="isn't a demo"):
        make_console_demo(other, n_jobs=20, days=1)


def test_the_command_says_how_to_start_the_console(tmp_path):
    from nomad.cli import cli
    r = CliRunner().invoke(cli, ["demo", "--console", str(tmp_path / "d"), "--jobs", "30", "--days", "2",
                                 "--seed", "5"])
    assert r.exit_code == 0, r.output
    assert "NOMAD_CONSOLE_DEMO=1" in r.output and "grace" in r.output and "Research assistant" in r.output
    assert (tmp_path / "d" / ".config" / "nomad" / "nomad.toml").exists()


def test_the_stress_scenarios_go_in_and_leave_the_database_unlocked(tmp_path):
    from nomad.demo import run_demo
    db = tmp_path / "x.db"
    run_demo(n_jobs=20, days=1, seed=1, launch_dashboard=False, db_path=db, clear=False)
    c = sqlite3.connect(db, timeout=0)
    c.execute("BEGIN IMMEDIATE")              # nothing still holds it
    assert c.execute("SELECT COUNT(*) FROM workstation_state WHERE hostname = 'login-node-01'").fetchone()[0] == 6


def test_never_in_a_home_or_over_a_nomad_toml_the_demo_did_not_make(tmp_path, monkeypatch):
    from nomad.demo import make_console_demo
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    (tmp_path / "me").mkdir()
    with pytest.raises(ValueError, match="home folder"):
        make_console_demo(tmp_path / "me", n_jobs=10, days=1)
    real = tmp_path / "x" / ".config" / "nomad"
    real.mkdir(parents=True)
    (real / "nomad.toml").write_text("[console.roles]\nadmin = ['someone']\n")
    with pytest.raises(ValueError, match="isn't the demo's"):
        make_console_demo(tmp_path / "x", n_jobs=10, days=1)
    assert "someone" in (real / "nomad.toml").read_text()
    # a symlinked one is never followed
    target = tmp_path / "etc.toml"
    target.write_text("[general]\n")
    real2 = tmp_path / "y" / ".config" / "nomad"
    real2.mkdir(parents=True)
    (real2 / "nomad.toml").symlink_to(target)
    with pytest.raises(ValueError):
        make_console_demo(tmp_path / "y", n_jobs=10, days=1)
    assert target.read_text() == "[general]\n"


def test_a_run_cut_short_leaves_the_last_demo(tmp_path, monkeypatch, made):
    from nomad import demo
    from nomad.demo_cast import is_demo_db
    before = Path(made["db"]).stat().st_size

    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr("nomad.demo_cast.add_cast", boom)
    with pytest.raises(KeyboardInterrupt):
        demo.make_console_demo(made["dir"], n_jobs=10, days=1)
    assert is_demo_db(made["db"]) and Path(made["db"]).stat().st_size == before
    monkeypatch.undo()
    again = demo.make_console_demo(made["dir"], n_jobs=10, days=1, seed=1)     # and runs again
    assert is_demo_db(again["db"])


def test_the_nodes_hold_what_the_running_jobs_hold(made):
    c = sqlite3.connect(made["db"])
    stamp = c.execute("SELECT MAX(timestamp) FROM node_state WHERE cluster = 'demo-cluster'").fetchone()[0]
    nodes = {r[0]: r[1:] for r in c.execute(
        "SELECT node_name, cpus_alloc, memory_alloc_mb, memory_free_mb, memory_total_mb, cpu_alloc_percent, "
        "state FROM node_state WHERE timestamp = ?", (stamp,))}
    held = {}
    for node, cpus, mem in c.execute("SELECT node_list, req_cpus, req_mem_mb FROM jobs WHERE state = 'RUNNING' "
                                     "AND end_time IS NULL"):
        a, b = held.get(node, (0, 0))
        held[node] = (a + cpus, b + mem)
    for name, (cpus, mem, free, total, pct, state) in nodes.items():
        want = held.get(name, (0, 0))
        assert (cpus, mem) == (want[0], min(want[1], total)), name
        assert free == total - mem and pct is not None
        assert (state == "idle") == (cpus == 0), name
    # alice's 400 GB waits for memory the big-memory nodes don't have free
    assert all(nodes[n][2] < 400 * 1024 for n in ("node07", "node08"))


def test_jobs_moved_back_keep_their_accounting_and_stay_in_the_window(made):
    from datetime import datetime, timedelta
    c = sqlite3.connect(made["db"])
    made_at = datetime.fromisoformat(c.execute("SELECT value FROM demo_info WHERE key = 'made_at'").fetchone()[0])
    bad = c.execute("SELECT COUNT(*) FROM jobs j JOIN job_accounting a ON a.job_id = j.job_id "
                    "AND a.cluster = j.cluster WHERE a.submit_time != j.submit_time").fetchone()[0]
    assert bad == 0
    ends = [datetime.fromisoformat(r[0]) for r in c.execute("SELECT end_time FROM jobs WHERE end_time IS NOT NULL")]
    assert max(ends) <= made_at
    recent = sum(1 for e in ends if made_at - e < timedelta(hours=1))
    assert recent < len(ends) / 4                           # not all piled up just before now


def test_the_stress_scenarios_survive_a_second_demo_and_their_jobs_add_up(tmp_path):
    from datetime import datetime
    from nomad.demo import run_demo
    db = tmp_path / "x.db"
    for _ in range(2):
        run_demo(n_jobs=10, days=1, seed=1, launch_dashboard=False, db_path=db, clear=False)
    c = sqlite3.connect(db)
    rows = c.execute("SELECT submit_time, start_time, end_time, runtime_seconds, wait_time_seconds FROM jobs "
                     "WHERE CAST(job_id AS INTEGER) >= 7700000").fetchall()
    assert len(rows) == 35
    for sub, start, end, run, wait in rows:
        s, b, e = (datetime.fromisoformat(t) for t in (sub, start, end))
        assert (b - s).total_seconds() == wait and (e - b).total_seconds() == run


def test_the_same_seed_gives_the_same_jobs_in_any_process(tmp_path):
    import os
    import subprocess
    import sys
    code = ("import random, sys; from nomad.demo import DemoGenerator; "
            "j = DemoGenerator(seed=42).generate_jobs(200, days=3); "
            "print(sum(x.failure_reason == 0 for x in j))")
    out = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                          env={**os.environ, "PYTHONHASHSEED": h}).stdout for h in ("1", "2", "3")}
    assert len(out) == 1


def test_a_hub_never_takes_a_demo_marker(tmp_path):
    from nomad.db.delta_hub import SKIP_TABLES
    from nomad.demo_cast import is_demo_db
    assert "demo_info" in SKIP_TABLES
    odd = tmp_path / "a#b?c%d"
    odd.mkdir()
    db = odd / "nomad_demo.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE demo_info (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT INTO demo_info VALUES ('made_by', 'nomad demo --console')")
    c.commit()
    c.close()
    assert is_demo_db(db) and sorted(p.name for p in odd.iterdir()) == ["nomad_demo.db"]
    # a synced copy (the sync's columns added) is not one
    synced = tmp_path / "synced.db"
    c = sqlite3.connect(synced)
    c.execute("CREATE TABLE demo_info (key TEXT, value TEXT, source_site TEXT, _sync_site TEXT)")
    c.execute("INSERT INTO demo_info VALUES ('made_by', 'nomad demo --console', 'a', 'a')")
    c.commit()
    c.close()
    assert not is_demo_db(synced)


# --- nomad console role: one person's role in [console.roles] -------------------------------

ROLES_TOML = """# the Console's settings
[general]
cluster_name = "c"

[console]
title = "x"

[console.roles]
admin = ["jtonini"]            # everything, settings included
operator = []                  # everything, no settings

[console.labs]
group_pattern = "{netid}$"
"""


def _role(tmp_path, text, *args):
    from nomad.cli import cli
    toml = tmp_path / "nomad.toml"
    toml.write_text(text)
    r = CliRunner().invoke(cli, ["-c", str(toml), "console", "role", *args])
    return r, toml


def test_console_role_shows_then_writes_one_change(tmp_path):
    from nomad.config import read_toml
    from nomad.config.access import access_from
    r, toml = _role(tmp_path, ROLES_TOML, "JDoe", "operator")
    assert r.exit_code == 0, r.output
    assert "jdoe is an operator" in r.output and "--apply" in r.output
    assert toml.read_text() == ROLES_TOML                          # shown only
    r, toml = _role(tmp_path, ROLES_TOML, "jdoe", "operator", "--apply")
    assert r.exit_code == 0 and "Written." in r.output, r.output
    text = toml.read_text()
    assert 'operator = ["jdoe"]                  # everything, no settings\n' in text
    assert text.replace('["jdoe"]', "[]") == ROLES_TOML           # nothing else moved
    assert list(tmp_path.glob("nomad.toml.bak-*"))
    acc = access_from(read_toml(toml), log=False)
    assert acc.role("jdoe") == "operator" and acc.role("jtonini") == "admin"


def test_console_role_moves_and_removes(tmp_path):
    from nomad.config.edit import TomlEdit
    from nomad.config.roles import check_for, role_of, set_role
    toml = tmp_path / "nomad.toml"
    toml.write_text(ROLES_TOML.replace('operator = []', 'operator = ["amy", "jdoe"]'))

    def change(netid, role):
        edit = TomlEdit(toml)
        before = edit.data
        said = set_role(edit, netid, role)
        edit.save(check_for(before, netid, role))
        return said
    assert change("jdoe", "admin") == ["jdoe is an admin ([console.roles] admin)",
                                       "jdoe is no longer listed as an operator"]
    data = TomlEdit(toml).data
    assert role_of(data, "jdoe") == "admin" and role_of(data, "amy") == "operator"
    # viewer: named one, whatever else says; none: on no list
    said = change("jtonini", "viewer")
    assert said[0] == "jtonini is no longer listed as an admin" and "viewer ([console.roles] viewer)" in said[1]
    text = toml.read_text()
    assert 'admin = ["jdoe"]' in text and 'operator = ["amy"]' in text and 'viewer = ["jtonini"]' in text
    assert "# everything, settings" in text
    assert role_of(TomlEdit(toml).data, "jtonini") == "viewer"
    assert change("jtonini", "none")[-1].endswith("the Console's own record for them decides")
    assert role_of(TomlEdit(toml).data, "jtonini") == "none" and 'viewer = []' in toml.read_text()
    # already so: nothing to change
    assert set_role(TomlEdit(toml), "nobody", "none") == []
    assert set_role(TomlEdit(toml), "amy", "operator") == []


def test_a_named_viewer_is_a_viewer_whatever_else():
    from nomad.config.access import access_from
    acc = access_from({"console": {"roles": {"admin": ["a"], "viewer": ["v", "a"], "operator": []}}}, log=False)
    assert acc.file_role("v") == "viewer" and acc.role("v") is None and "v" in acc.named()
    assert acc.file_role("a") == "admin" and any("viewer and as admin" in p for p in acc.problems)
    assert acc.file_role("x") is None


def test_console_role_adds_the_table_or_the_list(tmp_path):
    from nomad.config.edit import TomlEdit
    from nomad.config.roles import role_of, set_role
    toml = tmp_path / "nomad.toml"
    toml.write_text('[console]\ntitle = "x"\n\n[console.labs]\ngroup_pattern = ""\n')
    edit = TomlEdit(toml)
    set_role(edit, "jdoe", "admin")
    assert edit.text.index("[console.roles]") < edit.text.index("[console.labs]")   # after [console]
    assert role_of(edit.data, "jdoe") == "admin"
    toml.write_text('[console.roles]\nadmin = "jtonini"   # one\n')
    edit = TomlEdit(toml)
    set_role(edit, "jdoe", "operator")
    set_role(edit, "amy", "admin")
    assert edit.text == '[console.roles]\noperator = ["jdoe"]\nadmin = ["jtonini", "amy"]   # one\n'


@pytest.mark.parametrize("text", [
    '[console]\nroles = { admin = ["jtonini"] }\n',
    '[console]\nroles.admin = ["jtonini"]\n',
    '[console.roles]\nadmin = [\n  "jtonini",\n]\n',
])
def test_console_role_never_rewrites_what_it_cant_read_line_by_line(tmp_path, text):
    r, toml = _role(tmp_path, text, "jdoe", "admin", "--apply")
    assert r.exit_code != 0 and "by hand" in r.output and "nothing changed" in r.output, r.output
    assert toml.read_text() == text


def test_console_role_takes_netids_only(tmp_path):
    for bad in ("a b", "-x", "jdoe@richmond.edu", ""):
        r, toml = _role(tmp_path, ROLES_TOML, bad, "admin", "--apply")
        assert r.exit_code != 0 and toml.read_text() == ROLES_TOML, (bad, r.output)


@pytest.mark.parametrize("text, role", [
    ("[console.roles]\nadmin = '''jt'''\n", "admin"),
    ("[console.roles]\noperator = '''jdoe'''\n", "viewer"),
    ("[console.roles]\noperator = '''jdoe'''\n", "admin"),
    ('[console.roles]\nadmin = ["a", """b"""]\n', "admin"),
])
def test_console_role_refuses_multiline_strings_and_leaves_the_edit(tmp_path, text, role):
    from nomad.config.edit import TomlEdit
    from nomad.config.roles import RoleError, set_role
    toml = tmp_path / "nomad.toml"
    toml.write_text(text)
    edit = TomlEdit(toml)
    with pytest.raises(RoleError, match="by hand"):
        set_role(edit, "jdoe", role)
    assert edit.text == text
    r, _ = _role(tmp_path, text, "jdoe", role, "--apply")
    assert r.exit_code == 1 and "Traceback" not in r.output and toml.read_text() == text


def test_console_role_in_a_file_with_no_console_tables(tmp_path):
    from nomad.config.edit import TomlEdit
    from nomad.config.roles import role_of
    r, toml = _role(tmp_path, '[general]\ncluster_name = "c"\n', "jdoe", "admin", "--apply")
    assert r.exit_code == 0 and "Written." in r.output, r.output
    assert role_of(TomlEdit(toml).data, "jdoe") == "admin"


def test_an_edit_never_overwrites_a_change_made_meanwhile(tmp_path):
    from nomad.config.edit import TomlEdit
    from nomad.config.roles import check_for, set_role
    toml = tmp_path / "nomad.toml"
    toml.write_text(ROLES_TOML)
    edit = TomlEdit(toml)
    before = edit.data
    set_role(edit, "jdoe", "operator")
    toml.write_text(ROLES_TOML + '\n[support]\nemail = "help@x.edu"\n')        # someone else, meanwhile
    with pytest.raises(ValueError, match="changed since it was read"):
        edit.save(check_for(before, "jdoe", "operator"))
    assert "[support]" in toml.read_text() and "jdoe" not in toml.read_text()


def test_an_edit_keeps_crlf_and_reads_unicode_line_separators_as_text(tmp_path):
    from nomad.config.edit import TomlEdit
    from nomad.config.roles import check_for, role_of, set_role
    toml = tmp_path / "nomad.toml"
    text = ROLES_TOML.replace('title = "x"', 'title = "a\u2028[console.roles]\u2028b"   # \u0085 x')
    toml.write_bytes(text.replace("\n", "\r\n").encode())
    edit = TomlEdit(toml)
    before = edit.data
    set_role(edit, "jdoe", "operator")
    edit.save(check_for(before, "jdoe", "operator"))
    raw = toml.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    data = TomlEdit(toml).data
    assert role_of(data, "jdoe") == "operator" and data["console"]["title"] == "a\u2028[console.roles]\u2028b"


def test_an_edit_never_writes_through_a_planted_link(tmp_path):
    from nomad.config.edit import TomlEdit
    from nomad.config.roles import check_for, set_role
    toml = tmp_path / "nomad.toml"
    toml.write_text(ROLES_TOML)
    target = tmp_path / "elsewhere"
    target.write_text("keep me\n")
    (tmp_path / "nomad.toml.new").symlink_to(target)
    edit = TomlEdit(toml)
    before = edit.data
    set_role(edit, "jdoe", "operator")
    edit.save(check_for(before, "jdoe", "operator"))
    assert target.read_text() == "keep me\n" and "jdoe" in toml.read_text()


def test_role_backups_are_their_own_and_only_the_newest_stay(tmp_path):
    import os
    from nomad.config.edit import TomlEdit
    from nomad.config.roles import check_for, set_role
    toml = tmp_path / "nomad.toml"
    toml.write_text(ROLES_TOML)
    (tmp_path / "nomad.toml.bak-lab-20260101-000000").write_text("a lab backup\n")
    for i, (who, role) in enumerate([("a1", "admin"), ("a2", "operator"), ("a3", "viewer"), ("a4", "admin")]):
        edit = TomlEdit(toml)
        before = edit.data
        set_role(edit, who, role)
        b = edit.save(check_for(before, who, role), stamp=f"2026010{i}-000000", tag="role", keep=2)
        os.utime(b, (1_000_000 + i, 1_000_000 + i))
    names = sorted(p.name for p in tmp_path.glob("nomad.toml.bak-*"))
    assert names == ["nomad.toml.bak-lab-20260101-000000", "nomad.toml.bak-role-20260102-000000",
                     "nomad.toml.bak-role-20260103-000000"]


# --- the demo: any account's home, a folder in use, "now" after every reading ------------------

def test_never_in_any_accounts_home_or_a_folder_in_use(tmp_path, monkeypatch):
    import pwd
    from collections import namedtuple
    from nomad.demo import make_console_demo
    other = tmp_path / "home" / "nomad"
    other.mkdir(parents=True)
    Acc = namedtuple("Acc", "pw_name pw_dir")
    monkeypatch.setattr(pwd, "getpwall", lambda: [Acc("nomad", str(other)), Acc("root", "/")])
    with pytest.raises(ValueError, match="an account's home folder"):
        make_console_demo(other, n_jobs=10, days=1)
    used = tmp_path / "projects"
    used.mkdir()
    (used / "notes.txt").write_text("mine\n")
    with pytest.raises(ValueError, match="other things in it and no demo"):
        make_console_demo(used, n_jobs=10, days=1)
    assert sorted(p.name for p in used.iterdir()) == ["notes.txt"]
    assert not (other / ".config").exists()


def test_now_is_after_every_reading_and_nothing_is_after_it(made):
    from datetime import datetime
    c = sqlite3.connect(made["db"])
    made_at = c.execute("SELECT value FROM demo_info WHERE key = 'made_at'").fetchone()[0]
    assert "." not in made_at                                           # whole seconds
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    for t in tables:
        if "timestamp" in {r[1] for r in c.execute(f'PRAGMA table_info("{t}")')}:
            latest = c.execute(f'SELECT MAX(timestamp) FROM "{t}"').fetchone()[0]
            assert latest is None or datetime.fromisoformat(latest) <= datetime.fromisoformat(made_at), t
    # the newest node reading is the cast's: node02 holds bob's 4 CPUs
    row = c.execute("SELECT cpus_alloc FROM node_state WHERE node_name = 'node02' "
                    "ORDER BY timestamp DESC LIMIT 1").fetchone()
    assert row[0] == 4
    # and the queue now is the jobs': gpu has eve's and frank's running, none waiting
    q = c.execute("SELECT running_jobs, pending_jobs FROM queue_state WHERE partition = 'gpu' "
                  "ORDER BY timestamp DESC LIMIT 1").fetchone()
    assert q == (2, 0)


def test_the_demos_now_steps_past_a_reading_in_the_same_second(tmp_path):
    from datetime import datetime, timedelta
    from nomad.demo_cast import _after_readings
    c = sqlite3.connect(tmp_path / "x.db")
    now = datetime(2026, 10, 26, 18, 0, 0, 300000)
    c.execute("CREATE TABLE node_state (timestamp TEXT)")
    c.execute("CREATE TABLE gpu_stats (timestamp TEXT)")
    c.execute("INSERT INTO node_state VALUES (?)", ((now + timedelta(milliseconds=500)).isoformat(),))
    c.execute("INSERT INTO node_state VALUES (?)", ((now + timedelta(days=3)).isoformat(),))   # not "now"
    c.execute("INSERT INTO gpu_stats VALUES (?)", ((now + timedelta(days=6)).isoformat(),))
    got = _after_readings(c, {"node_state", "gpu_stats"}, now)
    assert got == datetime(2026, 10, 26, 18, 0, 1)
    assert c.execute("SELECT COUNT(*) FROM gpu_stats").fetchone()[0] == 0


# --- third review: condo edits count lines as TomlEdit does; saves one at a time ---------------

def test_a_condo_confirmation_lands_on_its_own_line_whatever_the_file_holds(tmp_path):
    from nomad.condo import config as ccfg
    from nomad.condo import edit as cedit
    path = tmp_path / "report.toml"
    path.write_text("# notes\u2028more\u2028and\u2028more \u0085 x\n"
                    "[report.clusters.c.condos.alpha]\nowner = \"a1\"\nconfirmed = false\n\n"
                    "[report.clusters.c.condos.beta]\nowner = \"b1\"\nconfirmed = false\n")
    plan = cedit.plan(path, cedit.Changes(confirm=["beta"]))
    cedit.save(plan)
    condos = ccfg.load(path).clusters["c"].condos
    assert condos["beta"].confirmed and not condos["alpha"].confirmed
    assert " " in path.read_text()


def test_two_saves_at_once_never_lose_one_or_empty_the_file(tmp_path):
    import threading
    from nomad.config.edit import TomlEdit, loads
    from nomad.config.roles import check_for, set_role
    toml = tmp_path / "nomad.toml"
    toml.write_text(ROLES_TOML)
    edits, results, seen = [], [], []
    for who in ("u1", "u2", "u3", "u4", "u5", "u6"):
        e = TomlEdit(toml)
        before = e.data
        set_role(e, who, "operator")
        edits.append((e, check_for(before, who, "operator"), who))
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            try:
                seen.append(bool(loads(toml.read_text())))
            except Exception:
                seen.append(False)

    def go(e, check, who):
        try:
            e.save(check, tag="role")
            results.append(who)
        except ValueError:
            pass
    w = threading.Thread(target=watch)
    w.start()
    threads = [threading.Thread(target=go, args=a) for a in edits]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    w.join()
    ops = loads(toml.read_text())["console"]["roles"]["operator"]
    assert results and set(ops) == set(results)          # what was reported saved is what's there
    assert all(seen), "nomad.toml was empty or unreadable for a moment"
    assert not list(tmp_path.glob(".nomad.toml.*.new"))


def test_a_fresh_backup_is_never_the_one_pruned(tmp_path):
    import os
    from nomad.config.edit import TomlEdit
    from nomad.config.roles import check_for, set_role
    toml = tmp_path / "nomad.toml"
    toml.write_text(ROLES_TOML)
    for i in range(3):
        (tmp_path / f"nomad.toml.bak-role-2026090{i}-000000").write_text("old\n")
    os.utime(toml, (1, 1))                                  # restored with cp -p: an old time
    edit = TomlEdit(toml)
    before = edit.data
    set_role(edit, "jdoe", "admin")
    b = edit.save(check_for(before, "jdoe", "admin"), tag="role", keep=2)
    assert b.exists()
    assert sorted(p.name for p in tmp_path.glob("nomad.toml.bak-role-*")) == sorted(
        [b.name, "nomad.toml.bak-role-20260902-000000"])


def test_a_first_demo_cut_short_doesnt_block_the_next(tmp_path, monkeypatch):
    from nomad import demo
    from nomad.demo_cast import is_demo_db
    where = tmp_path / "fresh"

    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr("nomad.demo_cast.add_cast", boom)
    with pytest.raises(KeyboardInterrupt):
        demo.make_console_demo(where, n_jobs=10, days=1)
    assert list(where.iterdir()) == []
    monkeypatch.undo()
    (where / ".nomad_demo.db.building").write_text("left by a killed run")
    made = demo.make_console_demo(where, n_jobs=10, days=1, seed=1)
    assert is_demo_db(made["db"]) and not (where / ".nomad_demo.db.building").exists()


def test_a_save_locks_the_file_that_is_there_once_it_holds_the_lock(tmp_path, monkeypatch):
    import fcntl
    import os
    from nomad.config.edit import TomlEdit
    toml = tmp_path / "nomad.toml"
    toml.write_text(ROLES_TOML)
    edit = TomlEdit(toml)
    real, calls = fcntl.flock, []

    def flock(f, op):
        real(f, op)
        calls.append(os.fstat(f.fileno()).st_ino)
        if len(calls) == 1:                 # meanwhile, a save of the same bytes swaps the file
            spare = tmp_path / "spare"
            spare.write_text(ROLES_TOML)
            os.replace(spare, toml)
    monkeypatch.setattr(fcntl, "flock", flock)
    held = edit._lock()
    try:
        assert len(calls) == 2 and calls[0] != calls[1] == os.stat(toml).st_ino
    finally:
        held.close()


def test_pruning_keeps_backups_named_by_hand(tmp_path):
    from nomad.config.edit import TomlEdit
    from nomad.config.roles import check_for, set_role
    toml = tmp_path / "nomad.toml"
    toml.write_text(ROLES_TOML)
    (tmp_path / "nomad.toml.bak-role-before-ldap-move").write_text("mine\n")
    for i in range(3):
        edit = TomlEdit(toml)
        before = edit.data
        set_role(edit, f"u{i}", "operator")
        edit.save(check_for(before, f"u{i}", "operator"), stamp=f"2026100{i}-000000", tag="role", keep=1)
    assert sorted(p.name for p in tmp_path.glob("nomad.toml.bak-role-*")) == [
        "nomad.toml.bak-role-20261002-000000", "nomad.toml.bak-role-before-ldap-move"]


def test_console_roles_says_whose_word_decides(tmp_path):
    from nomad.cli import cli
    toml = tmp_path / "nomad.toml"
    toml.write_text(ROLES_TOML.replace("operator = []", 'operator = []\nviewer = ["vic"]'))
    out = {}
    for who in ("vic", "jdoe", "jtonini"):
        r = CliRunner().invoke(cli, ["-c", str(toml), "console", "roles", who, "--db", str(tmp_path / "none.db")])
        assert r.exit_code == 0, r.output
        out[who] = next(line for line in r.output.splitlines() if line.strip().startswith("role:"))
    assert "viewer (named one in the file" in out["vic"]
    assert "as the Console's own record says" in out["jdoe"] and not out["jdoe"].strip().startswith("role: viewer")
    assert out["jtonini"].strip() == "role: admin"


def test_a_swap_while_saving_is_refused(tmp_path, monkeypatch):
    import os
    from nomad.config import edit as tedit
    from nomad.config.roles import check_for, set_role
    toml = tmp_path / "nomad.toml"
    toml.write_text(ROLES_TOML)
    e = tedit.TomlEdit(toml)
    before = e.data
    set_role(e, "jdoe", "operator")
    real = os.chmod

    def chmod(path, mode, **kw):                 # an editor saves the same bytes meanwhile
        real(path, mode, **kw)
        if str(path).endswith(".new"):
            spare = tmp_path / "spare"
            spare.write_text(ROLES_TOML)
            os.replace(spare, toml)
    monkeypatch.setattr(tedit.os, "chmod", chmod)
    with pytest.raises(ValueError, match="changed while this was saving"):
        e.save(check_for(before, "jdoe", "operator"), tag="role")
    assert toml.read_text() == ROLES_TOML
    assert not list(tmp_path.glob(".nomad.toml.*.new")) and not list(tmp_path.glob("nomad.toml.bak-*"))
