# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The incremental sync (nomad.db.delta_site, nomad.db.delta_hub): each site
logs what changes in its database, and the hub applies only that, in place.

The full rebuild it replaces copied every site whole and rebuilt the combined
database every run: about 24 GB of writes a run on a real four-site hub,
which wore out the hub's SSD. What must hold is that the hub ends up with
exactly what a full copy would give it, whatever the sites' collectors do:
plain inserts, upserts that change a row or rewrite it as it was, INSERT OR
REPLACE (which deletes the old row without telling a delete trigger), keys
renamed in place, deletes and pruning, tables and columns added by
migrations, a table recreated, a site whose log started over.
"""
import os
import random
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from nomad.db import delta_hub as H
from nomad.db import delta_site as S

SCHEMA = [
    # append-only, AUTOINCREMENT id
    "CREATE TABLE samples (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, node TEXT, v REAL)",
    # text key, rewritten with INSERT OR REPLACE (as group_membership, job_accounting)
    "CREATE TABLE members (user TEXT, grp TEXT, seen TEXT, PRIMARY KEY (user, grp))",
    # upserted in place, key renamed (as jobs)
    "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, state TEXT, end_time TEXT, mem INTEGER, blob BLOB)",
    # no key at all (as storage_owners)
    "CREATE TABLE owners (ts TEXT, name TEXT, used INTEGER)",
    # rewritten with INSERT OR REPLACE, then its key moved aside (job_accounting
    # by the groups collector, then nomad.jobkeys in the same window)
    "CREATE TABLE accounting (job_id TEXT NOT NULL, state TEXT, cpu REAL, UNIQUE (job_id))",
    # two unique keys and a text id the hub does not copy (as interactive_servers)
    "CREATE TABLE servers (id TEXT PRIMARY KEY, host TEXT UNIQUE, seen TEXT)",
    # the site's own working state: never merged
    "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)",
]


def site_db(path):
    with sqlite3.connect(path) as c:
        for sql in SCHEMA:
            c.execute(sql)
    return path


def mutate(path, rnd, step):
    """A run of a site's collectors: a little of everything they do."""
    c = sqlite3.connect(path)
    for _ in range(rnd.randint(1, 6)):
        c.execute("INSERT INTO samples (ts, node, v) VALUES (?, ?, ?)",
                  (f"t{step}", f"n{rnd.randint(1, 3)}", rnd.random()))
    for _ in range(rnd.randint(0, 3)):
        c.execute("INSERT OR REPLACE INTO members (user, grp, seen) VALUES (?, ?, ?)",
                  (f"u{rnd.randint(1, 4)}", f"g{rnd.randint(1, 2)}", f"t{step}"))
    for _ in range(rnd.randint(0, 3)):
        jid = f"{rnd.randint(1, 8)}"
        state = rnd.choice(["RUNNING", "COMPLETED", "FAILED"])
        c.execute("INSERT INTO jobs (job_id, state, end_time, mem, blob) VALUES (?, ?, ?, ?, ?) "
                  "ON CONFLICT (job_id) DO UPDATE SET state = excluded.state, end_time = excluded.end_time",
                  (jid, state, f"t{step}", rnd.randint(1, 9), bytes([rnd.randint(0, 255)])))
    if rnd.random() < 0.3:                              # an upsert that changes nothing
        row = c.execute("SELECT job_id, state, end_time FROM jobs LIMIT 1").fetchone()
        if row:
            c.execute("INSERT INTO jobs (job_id, state, end_time) VALUES (?, ?, ?) "
                      "ON CONFLICT (job_id) DO UPDATE SET state = excluded.state, "
                      "end_time = excluded.end_time", row)
    if rnd.random() < 0.3:                              # a key renamed in place (jobkeys)
        row = c.execute("SELECT job_id FROM jobs WHERE job_id NOT LIKE '%@%' LIMIT 1").fetchone()
        if row:
            c.execute("UPDATE OR IGNORE jobs SET job_id = ? WHERE job_id = ?",
                      (f"{row[0]}@{step}", row[0]))
    if rnd.random() < 0.3:                              # pruning
        c.execute("DELETE FROM samples WHERE id IN (SELECT id FROM samples ORDER BY id LIMIT 2)")
    for _ in range(rnd.randint(0, 2)):
        c.execute("INSERT INTO owners (ts, name, used) VALUES (?, ?, ?)",
                  (f"t{step}", f"o{rnd.randint(1, 3)}", rnd.randint(0, 100)))
    if rnd.random() < 0.2:                              # thinning
        c.execute("DELETE FROM owners WHERE rowid IN (SELECT rowid FROM owners LIMIT 1)")
    for _ in range(rnd.randint(0, 2)):                  # REPLACE, then the key moved aside
        jid = f"{rnd.randint(1, 5)}"
        c.execute("INSERT OR REPLACE INTO accounting (job_id, state, cpu) VALUES (?, ?, ?)",
                  (jid, rnd.choice(["RUNNING", "COMPLETED"]), rnd.random()))
        if rnd.random() < 0.4:
            c.execute("UPDATE OR IGNORE accounting SET job_id = ? WHERE job_id = ?",
                      (f"{jid}@{step}", jid))
    for _ in range(rnd.randint(0, 2)):                  # REPLACE on either of two keys
        c.execute("INSERT OR REPLACE INTO servers (id, host, seen) VALUES (?, ?, ?)",
                  (f"s{rnd.randint(1, 3)}", f"h{rnd.randint(1, 3)}", f"t{step}"))
    if rnd.random() < 0.2:                              # UPDATE OR REPLACE onto another's key
        c.execute("UPDATE OR REPLACE servers SET host = ? WHERE id = ?",
                  (f"h{rnd.randint(1, 3)}", f"s{rnd.randint(1, 3)}"))
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('cursor', ?)", (str(step),))
    c.commit()
    c.close()


def contents(db, site):
    """Every row the hub has from ``site``, as the site sent it (the hub's
    own numbering left out), table by table."""
    c = sqlite3.connect(db)
    out = {}
    for t in H.site_tables(c):
        # By column name: a column a site gained later is added at the end
        # here (ALTER TABLE), wherever a fresh copy puts it.
        cols = sorted(r[1] for r in c.execute(f'PRAGMA table_info("{t}")') if r[1] != "id")
        rows = c.execute(f'SELECT {", ".join(cols)} FROM "{t}" WHERE _sync_site = ?', (site,)).fetchall()
        out[t] = (cols, sorted(rows, key=repr))
    c.close()
    return out


def full_copy(site_path, site="alpha"):
    """What the full rebuild gives: the site copied whole into a new hub."""
    hub = Path(str(site_path) + ".fullhub")
    if hub.exists():
        hub.unlink()
    conn = H.connect(hub)
    H.apply_snapshot(conn, site, site_path)
    conn.close()
    return contents(hub, site)


def local_site(path):
    return {"name": "alpha", "host": "localhost", "db_path": str(path)}


def sync_once(hub, site, hub_name=S.DEFAULT_HUB):
    """One incremental round, as `nomad sync` does it."""
    conn = H.connect(hub)
    try:
        state = H.get_state(conn, site["name"])
        with H.fetch_delta(site, state, hub=hub_name) as answer:
            got = H.apply_delta(conn, site["name"], answer.records())
        if "need_copy" in got:
            token = H.init_site(site, hub=hub_name)["token"]
            got = H.apply_snapshot(conn, site["name"], site["db_path"], token=token)
            got["copied"] = True
        return got
    finally:
        conn.close()


def bootstrap(hub, site, hub_name=S.DEFAULT_HUB):
    """A whole copy, as `nomad sync` takes it: the log set up first (and
    what the site calls this copy), then the database."""
    token = H.init_site(site, hub=hub_name)["token"]
    conn = H.connect(hub)
    try:
        return H.apply_snapshot(conn, site["name"], site["db_path"], token=token)
    finally:
        conn.close()


# --- the site's log -----------------------------------------------------------------------

def test_the_log_records_what_changes_and_not_what_does_not(tmp_path):
    db = site_db(tmp_path / "site.db")
    conn = S._connect(str(db))
    epoch, resend = S.install(conn, H.SKIP_TABLES)
    assert epoch and resend == []
    assert S.install(conn, H.SKIP_TABLES) == (epoch, [])          # idempotent
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"_nomad_i_jobs", "_nomad_u_jobs", "_nomad_d_jobs"} <= names
    assert not any("config" in n for n in names)                   # working state: not tracked
    conn.execute("INSERT INTO jobs (job_id, state) VALUES ('1', 'RUNNING')")
    since = S.last_seq(conn)
    conn.execute("INSERT INTO jobs (job_id, state) VALUES ('1', 'RUNNING') "
                 "ON CONFLICT (job_id) DO UPDATE SET state = excluded.state")   # as it was: no news
    # ... only a note that the row holding the key might have been replaced:
    # it is still there, so nothing is sent for it.
    recs = list(S.export(conn, since, epoch, H.SKIP_TABLES))
    assert [r for r in recs if "r" in r or "x" in r] == []
    conn.execute("UPDATE jobs SET state = 'COMPLETED' WHERE job_id = '1'")
    conn.execute("DELETE FROM jobs WHERE job_id = '1'")
    ops = conn.execute("SELECT tbl, op FROM _nomad_changes ORDER BY seq").fetchall()
    assert ops == [("jobs", "k"), ("jobs", "u"), ("jobs", "d")]
    conn.close()


def test_a_new_table_and_a_new_column_are_sent_whole(tmp_path):
    db = site_db(tmp_path / "site.db")
    conn = S._connect(str(db))
    epoch, _ = S.install(conn, H.SKIP_TABLES)
    conn.execute("CREATE TABLE later (a TEXT)")
    conn.execute("INSERT INTO later VALUES ('before the triggers')")
    conn.execute("ALTER TABLE jobs ADD COLUMN extra TEXT")
    assert S.install(conn, H.SKIP_TABLES) == (epoch, ["jobs", "later"])
    recs = list(S.export(conn, 0, epoch, H.SKIP_TABLES))
    whole = [r["t"] for r in recs if "t" in r and r["reset"]]
    assert whole == ["jobs", "later"]
    conn.close()


def test_a_hub_with_the_wrong_epoch_or_behind_is_told_to_copy(tmp_path):
    db = site_db(tmp_path / "site.db")
    conn = S._connect(str(db))
    epoch, _ = S.install(conn, H.SKIP_TABLES)
    assert "need_copy" in next(S.export(conn, 0, "another", H.SKIP_TABLES))
    assert "need_copy" in next(S.export(conn, 0, None, H.SKIP_TABLES))
    conn.execute("INSERT INTO samples (ts) VALUES ('x')")
    list(S.export(conn, 1, epoch, H.SKIP_TABLES))                   # acknowledges 1
    assert "need_copy" in next(S.export(conn, 0, epoch, H.SKIP_TABLES))
    assert "need_copy" in next(S.export(conn, 99, epoch, H.SKIP_TABLES))
    conn.close()


def test_a_hub_too_far_behind_copies_and_no_longer_holds_the_log(tmp_path):
    db = site_db(tmp_path / "site.db")
    conn = S._connect(str(db))
    epoch, _ = S.install(conn, H.SKIP_TABLES, "far")
    conn.executemany("INSERT INTO samples (ts) VALUES (?)", [("x",)] * 20)
    first = next(S.export(conn, 0, epoch, H.SKIP_TABLES, max_log=10, hub="far"))
    assert "need_copy" in first and first["epoch"] == epoch       # for that hub only
    assert "far" not in S._hubs(conn)
    assert conn.execute("SELECT COUNT(*) FROM _nomad_changes").fetchone()[0] == 0   # nobody else needs it
    conn.close()


# --- the hub ends up with what a full copy gives -------------------------------------------

@pytest.mark.parametrize("seed,part", [(1, None), (2, None), (3, None), (4, 3), (5, 1)])
def test_the_hub_matches_a_full_copy_after_every_round(tmp_path, monkeypatch, seed, part):
    if part:                                             # the log read a few rows at a time
        monkeypatch.setattr(S, "PART", part)
        monkeypatch.setattr(S, "LOG_BATCH", 2)
    rnd = random.Random(seed)
    db = site_db(tmp_path / "site.db")
    site = local_site(db)
    hub = tmp_path / "hub.db"
    for step in range(3):
        mutate(db, rnd, step)
    bootstrap(hub, site)
    assert contents(hub, "alpha") == full_copy(db)
    for step in range(3, 25):
        mutate(db, rnd, step)
        if step == 12:                                   # a migration adds a column
            with sqlite3.connect(db) as c:
                c.execute("ALTER TABLE owners ADD COLUMN quota INTEGER")
                c.execute("UPDATE owners SET quota = 5")
        if step == 18:                                   # a table recreated
            with sqlite3.connect(db) as c:
                c.execute("DROP TABLE owners")
                c.execute("CREATE TABLE owners (ts TEXT, name TEXT, used INTEGER, quota INTEGER)")
                c.execute("INSERT INTO owners VALUES ('t18', 'o9', 1, 2)")
        got = sync_once(hub, site)
        assert "copied" not in got, got
        assert contents(hub, "alpha") == full_copy(db), f"round {step}"
    # Nothing of the site's working state, nor of the log, reached the hub.
    tables = {r[0] for r in sqlite3.connect(hub).execute("SELECT name FROM sqlite_master")}
    assert "config" not in tables and "_nomad_changes" not in tables


def test_two_sites_stay_apart(tmp_path):
    rnd = random.Random(7)
    hub = tmp_path / "hub.db"
    sites = []
    for name in ("alpha", "beta"):
        db = site_db(tmp_path / f"{name}.db")
        mutate(db, rnd, 0)
        sites.append({"name": name, "host": "localhost", "db_path": str(db)})
        bootstrap(hub, sites[-1])
    for step in range(1, 8):
        for s in sites:
            mutate(Path(s["db_path"]), rnd, step)
            sync_once(hub, s)
    for s in sites:
        assert contents(hub, s["name"]) == full_copy(Path(s["db_path"]), s["name"])
    c = sqlite3.connect(hub)
    assert dict(c.execute("SELECT source_site, COUNT(*) FROM samples GROUP BY 1")).keys() == {"alpha", "beta"}
    c.close()


def test_a_site_that_starts_over_is_copied_whole_once(tmp_path):
    rnd = random.Random(3)
    db = site_db(tmp_path / "site.db")
    site = local_site(db)
    hub = tmp_path / "hub.db"
    mutate(db, rnd, 0)
    bootstrap(hub, site)
    with sqlite3.connect(db) as c:                       # the site's database replaced, say
        c.execute("DELETE FROM _nomad_capture_meta")
    mutate(db, rnd, 1)
    got = sync_once(hub, site)
    assert got.get("copied")
    assert contents(hub, "alpha") == full_copy(db)
    mutate(db, rnd, 2)
    assert "copied" not in sync_once(hub, site)
    assert contents(hub, "alpha") == full_copy(db)


def test_an_answer_cut_short_changes_nothing(tmp_path):
    rnd = random.Random(5)
    db = site_db(tmp_path / "site.db")
    site = local_site(db)
    hub = tmp_path / "hub.db"
    mutate(db, rnd, 0)
    bootstrap(hub, site)
    before = contents(hub, "alpha")
    conn = H.connect(hub)
    state = H.get_state(conn, "alpha")
    mutate(db, rnd, 1)
    with H.fetch_delta(site, state) as answer:
        records = list(answer.records())
    with pytest.raises(H.DeltaError):
        H.apply_delta(conn, "alpha", [r for r in records if not r.get("end")])
    assert H.get_state(conn, "alpha") == state
    conn.close()
    assert contents(hub, "alpha") == before
    sync_once(hub, site)                                 # and the next round catches up
    assert contents(hub, "alpha") == full_copy(db)


# --- over ssh, run by the site's own python3 ---------------------------------------------

SSH = r'''#!{py}
import os, subprocess, sys
args = sys.argv[1:]
i = 0
while i < len(args) and args[i].startswith("-"):
    i += 2 if args[i] in ("-o", "-i", "-p") else 1
cmd = args[i + 1]
sys.stdout.write("Welcome to the site (a message the site prints first)\n")
sys.stdout.flush()
env = dict(os.environ, HOME=os.environ["FAKE_REMOTE_HOME"], PYTHONHOME="/nonexistent")
sys.exit(subprocess.run(["bash", "-c", cmd], env=env).returncode)
'''


def test_the_exporter_runs_on_the_site_from_standard_input(tmp_path, monkeypatch):
    if not any(Path(d, "python3").exists() for d in os.environ.get("PATH", "").split(":")):
        pytest.skip("no python3 on PATH")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ssh").write_text(SSH.replace("{py}", sys.executable))
    (fake / "ssh").chmod((fake / "ssh").stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    home = tmp_path / "remote"
    (home / ".local/share/nomad").mkdir(parents=True)
    monkeypatch.setenv("FAKE_REMOTE_HOME", str(home))
    db = site_db(home / ".local/share/nomad/nomad.db")
    rnd = random.Random(11)
    mutate(db, rnd, 0)
    site = {"name": "alpha", "host": "site-head", "ssh_user": "x",
            "db_path": "~/.local/share/nomad/nomad.db"}
    hub = tmp_path / "hub.db"
    assert H.init_site(site)["epoch"]                    # the log set up over "ssh"
    conn = H.connect(hub)
    H.apply_snapshot(conn, "alpha", db)                  # (the copy is nomad.db.pull's)
    conn.close()
    for step in range(1, 6):
        mutate(db, rnd, step)
        got = sync_once(hub, site)
        assert "copied" not in got
    assert contents(hub, "alpha") == full_copy(db)


def test_a_site_without_python3_is_a_delta_error(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ssh").write_text("#!/bin/sh\necho 'bash: python3: command not found' >&2\nexit 127\n")
    (fake / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    with pytest.raises(H.DeltaError, match="python3: command not found"):
        H.init_site({"name": "old", "host": "h", "ssh_user": "x", "db_path": "x.db"})


def test_the_exporter_needs_only_python_3_6():
    """It runs with each site's own python3 (3.6 on RHEL 8): no syntax or
    library newer than that."""
    src = Path(S.__file__).read_text()
    for newer in ("from __future__ import annotations", ":=", "dataclass", "removeprefix",
                  "functools.cache", "match ", "| None", "list[", "dict["):
        assert newer not in src, newer


# --- nomad sync itself --------------------------------------------------------------------

def _nomad_site(path, nodes=3):
    from tests.conftest import _bootstrap_db_with_migrations
    _bootstrap_db_with_migrations(str(path))
    with sqlite3.connect(path) as c:
        for i in range(nodes):
            c.execute("INSERT INTO node_state (timestamp, node_name, state, is_healthy) "
                      "VALUES (?, ?, 'IDLE', 1)", (f"2026-10-22T0{i}:00:00", f"n{i}"))


def _hub_config(tmp_path, sites):
    cfg = tmp_path / "nomad.toml"
    body = "[hub]\n"
    for name, db in sites:
        body += f'\n[[hub.sites]]\nname = "{name}"\nhost = "localhost"\ndb_path = "{db}"\n'
    cfg.write_text(body)
    return cfg


def test_nomad_sync_builds_once_then_applies_only_changes(tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    from click.testing import CliRunner
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    sites = []
    for name in ("alpha", "beta"):
        db = tmp_path / f"{name}.db"
        _nomad_site(db)
        sites.append((name, db))
    cfg = _hub_config(tmp_path, sites)
    out = tmp_path / "combined.db"
    run = lambda *extra: CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "sync", "-o", str(out), *extra])

    r = run()
    assert r.exit_code == 0, r.output
    assert "Merging alpha... OK" in r.output and H.is_incremental(out)
    inode = out.stat().st_ino

    with sqlite3.connect(sites[0][1]) as c:
        c.execute("INSERT INTO node_state (timestamp, node_name, state, is_healthy) "
                  "VALUES ('2026-10-22T09:00:00', 'n9', 'DOWN', 0)")
        c.execute("UPDATE node_state SET state = 'MIXED' WHERE node_name = 'n0'")
        c.execute("DELETE FROM node_state WHERE node_name = 'n1'")
    r = run()
    assert r.exit_code == 0, r.output
    assert "Changes since the last sync" in r.output
    assert "alpha: OK (2 rows, 1 removed" in r.output, r.output
    assert "beta: OK (0 rows, 0 removed" in r.output, r.output
    assert out.stat().st_ino == inode                    # updated in place, not rebuilt
    cache = home / ".local/share/nomad/sync_cache"
    assert not list(cache.glob("*.db"))                  # no copies of the sites kept

    full = tmp_path / "full.db"
    r = CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "sync", "--full", "-o", str(full)])
    assert r.exit_code == 0, r.output
    for name, _ in sites:
        assert contents(out, name) == contents(full, name)
    c = sqlite3.connect(out)
    rows = dict(c.execute("SELECT name, source FROM sync_sites"))
    assert rows == {"alpha": "delta", "beta": "delta"}
    assert c.execute("SELECT state FROM node_state WHERE node_name = 'n0' AND source_site = 'alpha'"
                     ).fetchone() == ("MIXED",)
    c.close()

    # A site taken out of [hub] sites goes, as a rebuild would leave it out.
    cfg = _hub_config(tmp_path, sites[:1])
    r = CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "sync", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "beta: no longer in [hub] sites" in r.output
    c = sqlite3.connect(out)
    assert c.execute("SELECT COUNT(*) FROM node_state WHERE source_site = 'beta'").fetchone() == (0,)
    c.close()


def test_a_site_that_fails_keeps_what_the_hub_had(tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    from click.testing import CliRunner
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    db = tmp_path / "alpha.db"
    _nomad_site(db)
    cfg = _hub_config(tmp_path, [("alpha", db)])
    out = tmp_path / "combined.db"
    assert CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "sync", "-o", str(out)]).exit_code == 0
    before = contents(out, "alpha")
    seen = []
    monkeypatch.setattr(cli_mod, "_sync_alerts", lambda *a, **k: seen.append((a, k)) or [])

    def down(*a, **k):
        raise H.DeltaError("ssh failed: Connection timed out")
    monkeypatch.setattr(H, "fetch_delta", down)
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM node_state")
    r = CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "sync", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "alpha: FAILED, the hub keeps what it had: ssh failed: Connection timed out" in r.output
    assert contents(out, "alpha") == before
    (args, kw), = seen
    assert args[1] == {"alpha": "ssh failed: Connection timed out"}
    assert kw["blocked"] == "no site could be synced"
    row = sqlite3.connect(out).execute("SELECT merged, source, error FROM sync_sites").fetchone()
    assert row == (0, "kept", "ssh failed: Connection timed out")


def test_the_log_can_be_taken_out_and_put_back(tmp_path):
    rnd = random.Random(2)
    db = site_db(tmp_path / "site.db")
    site = local_site(db)
    hub = tmp_path / "hub.db"
    mutate(db, rnd, 0)
    bootstrap(hub, site)
    # 3 triggers on each table, 2 more on each with a unique key (members,
    # jobs, accounting, servers; not samples, owners)
    assert H.remove_log(site) == 3 * 6 + 2 * 4
    c = sqlite3.connect(db)
    left = c.execute("SELECT name FROM sqlite_master WHERE name LIKE '\\_nomad\\_%' ESCAPE '\\'").fetchall()
    c.close()
    assert left == []
    mutate(db, rnd, 1)                                  # nothing logged now
    got = sync_once(hub, site)
    assert got.get("copied")                            # so it is copied whole, and the log is back
    assert contents(hub, "alpha") == full_copy(db)


# --- found by the review of 1.17.0 ---------------------------------------------------------

def test_a_row_replaced_then_moved_aside_leaves_nothing_behind(tmp_path):
    """The groups collector REPLACEs a job_accounting row and nomad.jobkeys
    renames its key in the same window: the row REPLACE deleted must not
    stay on the hub beside the moved one (the job would count twice)."""
    db = site_db(tmp_path / "site.db")
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO accounting (job_id, state, cpu) VALUES ('4711', 'PENDING', 0)")
    site = local_site(db)
    hub = tmp_path / "hub.db"
    bootstrap(hub, site)
    with sqlite3.connect(db) as c:
        c.execute("INSERT OR REPLACE INTO accounting (job_id, state, cpu) VALUES ('4711', 'RUNNING', 1.0)")
        c.execute("UPDATE OR IGNORE accounting SET job_id = '4711@SUBMIT' WHERE job_id = '4711'")
    assert "copied" not in sync_once(hub, site)
    assert contents(hub, "alpha") == full_copy(db)
    rows = sqlite3.connect(hub).execute("SELECT job_id, state FROM accounting").fetchall()
    assert rows == [("4711@SUBMIT", "RUNNING")]


def test_the_export_holds_no_lock_while_the_hub_reads(tmp_path):
    """However slowly the hub takes the answer (ssh, a full pipe), a
    collector can write meanwhile: nothing is read from the site while a
    record is being handed out."""
    db = site_db(tmp_path / "site.db")
    conn = S._connect(str(db))
    epoch, _ = S.install(conn, H.SKIP_TABLES)
    since = S.last_seq(conn)
    conn.executemany("INSERT INTO jobs (job_id, state) VALUES (?, 'RUNNING')",
                     [(str(i),) for i in range(3 * S.BATCH)])
    out = S.export(conn, since, epoch, H.SKIP_TABLES)
    for _ in range(10):                                 # the header, the table, some rows
        next(out)
    writer = sqlite3.connect(db, timeout=0.2)
    writer.execute("INSERT INTO samples (ts) VALUES ('meanwhile')")
    writer.commit()                                     # "database is locked" before 1.17.0's review
    writer.close()
    assert sum(1 for r in out if "r" in r) == 3 * S.BATCH - 8
    conn.close()


def test_a_table_renamed_away_is_tracked_under_its_own_name(tmp_path):
    """ALTER TABLE ... RENAME moves a table's triggers with it: the new table
    of the old name, and the renamed one, are each sent as they are."""
    db = site_db(tmp_path / "site.db")
    with sqlite3.connect(db) as c:
        c.executemany("INSERT INTO owners VALUES (?, ?, ?)", [("t0", "bob", 1), ("t0", "eve", 2)])
    site = local_site(db)
    hub = tmp_path / "hub.db"
    bootstrap(hub, site)
    with sqlite3.connect(db) as c:
        c.execute("ALTER TABLE owners RENAME TO owners_v1")
        c.execute("CREATE TABLE owners (ts TEXT, name TEXT, used INTEGER)")
        c.execute("INSERT INTO owners VALUES ('t1', 'cleo', 3)")
        c.execute("UPDATE owners_v1 SET used = 9 WHERE name = 'bob'")
    sync_once(hub, site)
    assert contents(hub, "alpha") == full_copy(db)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO owners VALUES ('t2', 'dan', 4)")
        c.execute("DELETE FROM owners_v1 WHERE name = 'eve'")
        on = dict(c.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'"))
    assert all(S._made_for(n) == t for n, t in on.items())
    sync_once(hub, site)
    assert contents(hub, "alpha") == full_copy(db)


def test_a_column_dropped_after_untrack_is_sent_whole(tmp_path):
    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("no DROP COLUMN in this SQLite")
    db = site_db(tmp_path / "site.db")
    site = local_site(db)
    hub = tmp_path / "hub.db"
    mutate(db, random.Random(4), 0)
    bootstrap(hub, site)
    c = S._connect(str(db))
    with pytest.raises(sqlite3.OperationalError):       # the triggers name the column
        c.execute("ALTER TABLE jobs DROP COLUMN mem")
    assert S.untrack(c, "jobs") == 5
    c.execute("ALTER TABLE jobs DROP COLUMN mem")
    c.close()
    got = sync_once(hub, site)
    assert got["reset"] == ["jobs"]
    # The hub keeps the column (others may have it); this site's rows leave it empty.
    assert sqlite3.connect(hub).execute("SELECT COUNT(*) FROM jobs WHERE mem IS NOT NULL").fetchone() == (0,)


def test_two_hubs_each_keep_their_place(tmp_path):
    """A trial sync to another file (another hub) must not make the real hub
    copy every site whole: each hub has its own place in the log."""
    rnd = random.Random(8)
    db = site_db(tmp_path / "site.db")
    site = local_site(db)
    real, trial = tmp_path / "real.db", tmp_path / "trial.db"
    mutate(db, rnd, 0)
    bootstrap(real, site, "real")
    bootstrap(trial, site, "trial")
    for step in range(1, 4):
        mutate(db, rnd, step)
        assert "copied" not in sync_once(trial, site, "trial")
    mutate(db, rnd, 9)
    assert "copied" not in sync_once(real, site, "real")
    assert contents(real, "alpha") == full_copy(db)
    # The log is kept back to the real hub's place, and no further.
    c = S._connect(str(db))
    hubs = S._hubs(c)
    assert int(S._meta(c, "pruned")) == min(hubs["real"][0], hubs["trial"][0])
    c.close()
    # A hub not heard from for days is no longer waited for.
    later = S._now() + (S.RECENT_DAYS + 1) * 86400
    mutate(db, rnd, 10)
    sync_once(trial, site, "trial")
    mutate(db, rnd, 11)
    c = S._connect(str(db))
    state = H.get_state(sqlite3.connect(trial), "alpha")
    list(S.export(c, state["seq"], state["epoch"], H.SKIP_TABLES, hub="trial", token=state["token"],
                  now=later))
    assert int(S._meta(c, "pruned")) == state["seq"]                # the real hub's place passed
    real_state = H.get_state(sqlite3.connect(real), "alpha")
    assert real_state["seq"] < state["seq"]
    first = next(S.export(c, real_state["seq"], real_state["epoch"], H.SKIP_TABLES, hub="real",
                          token=real_state["token"], now=later))
    assert "need_copy" in first                                     # so it copies
    c.close()


def test_a_log_nobody_reads_goes(tmp_path):
    db = site_db(tmp_path / "site.db")
    c = S._connect(str(db))
    S.install(c, H.SKIP_TABLES, "one")
    S.install(c, H.SKIP_TABLES, "two")
    assert S.forget(c, "one") == 0                      # "two" still reads it
    assert S.expire(c) is None
    week = S._now() + (S.EXPIRE_DAYS + 1) * 86400
    assert S.expire(c, now=week) == "removed"           # nobody heard from for a week
    assert S._our_triggers(c) == {}
    assert S.expire(c) is None                          # nothing there now
    S.install(c, H.SKIP_TABLES, "one")
    assert S.forget(c, "one") == 3 * 6 + 2 * 4          # the only hub: the log goes at once
    assert S._our_triggers(c) == {}
    S.install(c, H.SKIP_TABLES, "one")
    S.install(c, H.SKIP_TABLES, "two")
    c.executemany("INSERT INTO samples (ts) VALUES (?)", [("x",)] * 25)
    list(S.export(c, S.last_seq(c), S._meta(c, "epoch"), H.SKIP_TABLES, hub="two"))
    c.executemany("INSERT INTO samples (ts) VALUES (?)", [("x",)] * 5)
    # "one" is 30 behind, holding the log: it stops holding it; "two" (5
    # behind) is not affected.
    assert S.expire(c, max_log=10) == "dropped for 1 hub(s) far behind"
    assert set(S._hubs(c)) == {"two"}
    assert c.execute("SELECT COUNT(*) FROM _nomad_changes").fetchone() == (5,)
    c.close()


def test_collect_takes_out_a_log_nobody_reads(tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    db = site_db(tmp_path / "site.db")
    c = S._connect(str(db))
    S.install(c, H.SKIP_TABLES, "gone")
    c.close()
    cli_mod._expire_change_log(db)
    assert S._our_triggers(sqlite3.connect(db))         # read lately: kept
    monkeypatch.setattr(S, "_now", lambda: int(__import__("time").time()) + 8 * 86400)
    cli_mod._expire_change_log(db)
    assert S._our_triggers(sqlite3.connect(db)) == {}
    cli_mod._expire_change_log(tmp_path / "missing.db")  # never raises


def test_the_answer_is_spooled_and_read_a_record_at_a_time(tmp_path):
    db = site_db(tmp_path / "site.db")
    site = local_site(db)
    hub = tmp_path / "hub.db"
    rnd = random.Random(6)
    mutate(db, rnd, 0)
    bootstrap(hub, site)
    mutate(db, rnd, 1)
    spool = tmp_path / "spool"
    conn = H.connect(hub)
    with H.fetch_delta(site, H.get_state(conn, "alpha"), spool_dir=spool) as answer:
        assert [p.name for p in spool.iterdir()] == [answer.path.name] and answer.size > 0
        recs = answer.records()
        assert next(recs)["h"] == "nomad-delta"          # a generator, not a list
        H.apply_delta(conn, "alpha", __import__("itertools").chain([{"h": "nomad-delta", "epoch":
                      H.get_state(conn, "alpha")["epoch"], "from": 0}], recs))
    assert list(spool.iterdir()) == []
    conn.close()
    assert contents(hub, "alpha") == full_copy(db)
    broken = tmp_path / "broken"
    broken.write_bytes(S.MARKER + b"\x1f\x8b not gzip")
    with pytest.raises(H.DeltaError, match="unreadable answer"):
        list(H.Answer(broken).records())


def test_a_copy_that_cannot_say_where_the_log_stands_is_copied_again(tmp_path):
    rnd = random.Random(9)
    db = site_db(tmp_path / "site.db")
    site = local_site(db)
    H.init_site(site)
    mutate(db, rnd, 0)
    hub = tmp_path / "hub.db"
    conn = H.connect(hub)
    got = H.apply_snapshot(conn, "alpha", db, trust_log=False)
    assert got["epoch"] is None and H.get_state(conn, "alpha")["mode"] == "copy"
    conn.close()


def _cli_hub(tmp_path, monkeypatch, names=("alpha",)):
    import nomad.cli as cli_mod
    from click.testing import CliRunner
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(H, "WAL_WAIT_SECONDS", 0.5)
    sites = []
    for name in names:
        db = tmp_path / f"{name}.db"
        _nomad_site(db)
        sites.append((name, db))
    cfg = _hub_config(tmp_path, sites)
    out = tmp_path / "combined.db"

    def run(*extra, config=cfg):
        return CliRunner().invoke(cli_mod.cli, ["-c", str(config), "sync", "-o", str(out), *extra])
    return run, sites, out


def test_an_exporter_that_stops_working_is_copied_whole_instead(tmp_path, monkeypatch):
    run, sites, out = _cli_hub(tmp_path, monkeypatch)
    assert run().exit_code == 0
    db = sites[0][1]
    real = H.fetch_delta

    def broken(*a, **k):
        raise H.DeltaError("Traceback ... ModuleNotFoundError: No module named '_sqlite3'")
    monkeypatch.setattr(H, "fetch_delta", broken)
    for i in (1, 2):
        with sqlite3.connect(db) as c:
            c.execute("INSERT INTO node_state (timestamp, node_name, state, is_healthy) "
                      "VALUES (?, ?, 'IDLE', 1)", (f"2026-10-23T0{i}:00:00", f"m{i}"))
        r = run()
        assert "alpha: FAILED, the hub keeps what it had" in r.output, r.output
    r = run()
    assert "copying it whole (asking for its changes failed 3 times in a row" in r.output, r.output
    assert contents(out, "alpha") == full_copy_of(db, tmp_path)
    # One that can't run at all is copied at once.
    def cant(*a, **k):
        raise H.DeltaError("bash: python3: command not found", cant_run=True)
    monkeypatch.setattr(H, "fetch_delta", cant)
    r = run()
    assert "copying it whole (its exporter can't run there" in r.output, r.output
    monkeypatch.setattr(H, "fetch_delta", real)
    r = run()
    assert "alpha: OK (0 rows, 0 removed" in r.output, r.output   # and back to changes only


def full_copy_of(db, tmp_path):
    """What a fresh full build gives for a real nomad site database."""
    hub = tmp_path / "fresh.db"
    if hub.exists():
        hub.unlink()
    conn = H.connect(hub)
    H.apply_snapshot(conn, "alpha", db)
    conn.close()
    return contents(hub, "alpha")


def test_a_site_taken_out_is_told(tmp_path, monkeypatch):
    run, sites, out = _cli_hub(tmp_path, monkeypatch, ("alpha", "beta"))
    assert run().exit_code == 0
    beta = sites[1][1]
    assert S._our_triggers(sqlite3.connect(beta))
    r = run(config=_hub_config(tmp_path, sites[:1]))
    assert "beta: no longer in [hub] sites; its" in r.output and "change log taken out there" in r.output
    assert S._our_triggers(sqlite3.connect(beta)) == {}


def test_full_on_an_incremental_hub_copies_in_place_while_it_is_read(tmp_path, monkeypatch):
    """`nomad sync --full` with the Console reading: before 1.17.0's review it
    renamed a new file over the open database, under its write-ahead log,
    and the database came out malformed."""
    run, sites, out = _cli_hub(tmp_path, monkeypatch, ("alpha", "beta"))
    assert run().exit_code == 0
    inode = out.stat().st_ino
    reader = sqlite3.connect(out)
    reader.execute("BEGIN")
    before = reader.execute("SELECT COUNT(*) FROM node_state").fetchone()
    with sqlite3.connect(sites[0][1]) as c:
        c.executemany("INSERT INTO node_state (timestamp, node_name, state, is_healthy) "
                      "VALUES (?, ?, 'IDLE', 1)", [(f"2026-10-23T{i:02d}:00:00", "n") for i in range(20)])
    assert run().exit_code == 0                          # frames the reader keeps in the WAL
    r = run("--full")
    assert r.exit_code == 0, r.output
    assert "Every site copied whole, in place" in r.output
    assert out.stat().st_ino == inode
    assert reader.execute("SELECT COUNT(*) FROM node_state").fetchone() == before   # its snapshot
    reader.execute("COMMIT")
    assert reader.execute("SELECT COUNT(*) FROM node_state").fetchone()[0] == before[0] + 20
    reader.close()
    c = sqlite3.connect(out)
    assert c.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    c.close()
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    for name, db in sites:
        assert contents(out, name) == full_copy_of(db, fresh) or name != "alpha"


def test_the_first_sync_after_an_upgrade_builds_in_place(tmp_path, monkeypatch):
    """A combined database from an older nomad is rebuilt whole, then copied
    into the open file (SQLite's backup), not renamed over it."""
    run, sites, out = _cli_hub(tmp_path, monkeypatch)
    old = sqlite3.connect(out)
    old.execute("PRAGMA journal_mode=WAL")
    old.execute("CREATE TABLE node_state (id INTEGER PRIMARY KEY, node_name TEXT, source_site TEXT)")
    old.execute("INSERT INTO node_state (node_name, source_site) VALUES ('stale', 'alpha')")
    old.commit()
    old.close()
    inode = out.stat().st_ino
    reader = sqlite3.connect(out)
    reader.execute("BEGIN")
    assert reader.execute("SELECT COUNT(*) FROM node_state").fetchone() == (1,)
    r = run()
    assert r.exit_code == 0, r.output
    assert "built by an older nomad" in r.output
    assert out.stat().st_ino == inode and H.is_incremental(out)
    assert reader.execute("SELECT COUNT(*) FROM node_state").fetchone() == (1,)    # still its snapshot
    reader.execute("COMMIT")
    assert reader.execute("SELECT COUNT(*) FROM node_state WHERE node_name = 'stale'").fetchone() == (0,)
    reader.close()
    c = sqlite3.connect(out)
    assert c.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert c.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    c.close()
    home = tmp_path / "home"
    assert not list((home / ".local/share/nomad/sync_cache").glob("*.db"))   # copies not kept
    assert not Path(f"{out}.tmp").exists()


def test_the_write_ahead_log_is_cut_back(tmp_path, monkeypatch):
    run, sites, out = _cli_hub(tmp_path, monkeypatch)
    assert run().exit_code == 0
    with sqlite3.connect(sites[0][1]) as c:
        c.executemany("INSERT INTO node_state (timestamp, node_name, state, is_healthy) "
                      "VALUES (?, ?, 'IDLE', 1)", [(f"t{i}", "n" * 200) for i in range(3000)])
    assert run("--full").exit_code == 0
    wal = Path(f"{out}-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_the_hubs_own_alert_sees_when_data_was_last_brought_up_to_date(tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    run, sites, out = _cli_hub(tmp_path, monkeypatch)
    assert run().exit_code == 0
    first = sqlite3.connect(out).execute("SELECT synced_at FROM sync_sites").fetchone()

    def down(*a, **k):
        raise H.DeltaError("ssh failed: Connection timed out")
    monkeypatch.setattr(H, "fetch_delta", down)
    monkeypatch.setattr(cli_mod, "_sync_alerts", lambda *a, **k: [])
    assert "FAILED" in run().output
    assert sqlite3.connect(out).execute("SELECT synced_at FROM sync_sites").fetchone() == first


# --- found by the second review of 1.17.0 ---------------------------------------------------

@pytest.mark.skipif(not hasattr(sqlite3.Connection, "setlimit"), reason="Python 3.11+")
def test_no_statement_needs_more_than_999_values(tmp_path):
    """SQLite before 3.32 (RHEL 8's 3.26) allows 999 values per statement:
    an export with thousands of rows noted for REPLACE must stay under."""
    db = site_db(tmp_path / "site.db")
    conn = S._connect(str(db))
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, S.MAX_VARIABLES)
    conn.executemany("INSERT INTO members (user, grp, seen) VALUES (?, 'g', 't0')",
                     [(f"u{i}",) for i in range(1500)])
    epoch, _ = S.install(conn, H.SKIP_TABLES)
    since = S.last_seq(conn)
    conn.execute("UPDATE members SET seen = seen")            # nothing changes ...
    conn.executemany("INSERT INTO members (user, grp, seen) VALUES (?, 'g', 't0') "
                     "ON CONFLICT DO NOTHING", [(f"u{i}",) for i in range(1500)])   # ... 1500 notes
    recs = list(S.export(conn, since, epoch, H.SKIP_TABLES))
    assert recs[-1].get("end") and not [r for r in recs if "r" in r or "x" in r]
    conn.execute("DELETE FROM members WHERE rowid % 2 = 0")
    since = recs[-1]["to"]
    recs = list(S.export(conn, since, epoch, H.SKIP_TABLES, token=recs[-1]["token"]))
    assert sum(len(r["x"]) for r in recs if "x" in r) == 750
    conn.close()


def test_a_site_restored_from_a_backup_is_copied_whole(tmp_path):
    """Its log comes back with the same epoch and numbers; once its
    collectors write past the hub's place, only the answer it gave last (a
    token the hub sends back) tells them apart."""
    import shutil
    rnd = random.Random(12)
    db = site_db(tmp_path / "site.db")
    site = local_site(db)
    hub = tmp_path / "hub.db"
    mutate(db, rnd, 0)
    bootstrap(hub, site)
    sync_once(hub, site)
    backup = tmp_path / "backup.db"
    shutil.copyfile(db, backup)
    for step in range(1, 4):
        mutate(db, rnd, step)
        sync_once(hub, site)
    shutil.copyfile(backup, db)                         # restored
    for step in range(4, 12):                           # writing past the hub's place
        mutate(db, rnd, step)
    got = sync_once(hub, site)
    assert got.get("copied")
    assert contents(hub, "alpha") == full_copy(db)


def test_a_site_with_no_way_to_take_a_snapshot_is_sent_whole_by_its_exporter(tmp_path, monkeypatch):
    """A site with no sqlite3 command and Python 3.6 (no backup): its live
    file could be torn, so its exporter sends every table whole instead, and
    from then on only its changes come."""
    import nomad.cli as cli_mod
    run, sites, out = _cli_hub(tmp_path, monkeypatch)
    assert run().exit_code == 0
    db = sites[0][1]
    real = cli_mod._pull_via_backup

    def no_snapshot(*a, why=None, how=None, refuse_live=False, **k):
        if refuse_live:
            how.append("no snapshot")
            why.append("no snapshot can be taken there")
            return False
        return real(*a, why=why, how=how, **k)
    monkeypatch.setattr(cli_mod, "_pull_via_backup", no_snapshot)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO node_state (timestamp, node_name, state, is_healthy) "
                  "VALUES ('2026-10-23T00:00:00', 'w1', 'IDLE', 1)")
    r = run("--full")
    assert "copied whole, by its exporter (no snapshot can be taken there); from now on only its changes" \
        in r.output, r.output
    assert contents(out, "alpha") == full_copy_of(db, tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM node_state WHERE node_name = 'w1'")
    r = run()
    assert "alpha: OK (0 rows, 1 removed" in r.output, r.output
    assert contents(out, "alpha") == full_copy_of(db, tmp_path)


def test_a_copy_that_is_not_there_takes_nothing_away(tmp_path):
    """ATTACH makes a database that isn't there: applied, it would take every
    row of the site out of the hub."""
    db = site_db(tmp_path / "site.db")
    mutate(db, random.Random(1), 0)
    hub = tmp_path / "hub.db"
    conn = H.connect(hub)
    H.apply_snapshot(conn, "alpha", db)
    before = contents(hub, "alpha")
    with pytest.raises(H.DeltaError, match="not there"):
        H.apply_snapshot(conn, "alpha", tmp_path / "vanished.db")
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).execute("PRAGMA user_version = 1")
    with pytest.raises(H.DeltaError, match="no tables"):
        H.apply_snapshot(conn, "alpha", empty)
    conn.close()
    assert contents(hub, "alpha") == before


def test_a_change_only_in_case_or_type_is_sent(tmp_path):
    db = tmp_path / "site.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE people (login TEXT COLLATE NOCASE, note TEXT COLLATE RTRIM, v)")
        c.execute("INSERT INTO people VALUES ('ana', 'x', 1)")
    site = local_site(db)
    hub = tmp_path / "hub.db"
    bootstrap(hub, site)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE people SET login = 'Ana', note = 'x  ', v = 1.0")
    sync_once(hub, site)
    assert sqlite3.connect(hub).execute("SELECT login, note FROM people").fetchone() == ("Ana", "x  ")
    assert contents(hub, "alpha") == full_copy(db)                   # 1.0, as a copy has it


def test_shell_noise_is_not_taken_for_an_exporter_that_cannot_run(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ssh").write_text("#!/bin/sh\necho 'module: command not found' >&2\n"
                              "echo 'sqlite: database is locked' >&2\nexit 2\n")
    (fake / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    site = {"name": "busy", "host": "h", "ssh_user": "x", "db_path": "x.db"}
    with pytest.raises(H.DeltaError) as e:
        H.init_site(site)
    assert not e.value.cant_run and "database is locked" in str(e.value)
    (fake / "ssh").write_text("#!/bin/sh\necho 'module: command not found' >&2\n"
                              "echo \"ModuleNotFoundError: No module named '_sqlite3'\" >&2\nexit 1\n")
    with pytest.raises(H.DeltaError) as e:
        H.init_site(site)
    assert e.value.cant_run


def test_answers_left_by_a_killed_hub_are_removed(tmp_path):
    import time
    old = tmp_path / "delta_alpha_x.part"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 3 * 3600,) * 2)
    new = tmp_path / "delta_beta_y.part"
    new.write_bytes(b"y")
    keep = tmp_path / "alpha.db"
    keep.write_bytes(b"z")
    os.utime(keep, (time.time() - 3 * 3600,) * 2)
    H.remove_old_spools(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["alpha.db", "delta_beta_y.part"]


def test_the_log_is_never_dropped_past_a_hub_answered_meanwhile(tmp_path, monkeypatch):
    """Two hubs at once: one dropping the log (a batch at a time), the other
    answered in between. Each batch works out how far it may go in its own
    transaction, and ``pruned`` only goes forward."""
    db = site_db(tmp_path / "site.db")
    a = S._connect(str(db))
    epoch, _ = S.install(a, H.SKIP_TABLES, "slow")
    a.executemany("INSERT INTO samples (ts) VALUES (?)", [("x",)] * 300)
    S.install(a, H.SKIP_TABLES, "fast")                  # both hold the log from here (300)
    a.executemany("INSERT INTO samples (ts) VALUES (?)", [("x",)] * 300)
    d = S._connect(str(db))
    with d:
        d.execute("UPDATE _nomad_capture_meta SET value = '100' WHERE key = 'ack:slow'")
        d.execute("UPDATE _nomad_capture_meta SET value = ? WHERE key = 'heard:slow'",
                  (str(S._now() - (S.RECENT_DAYS + 1) * 86400),))   # silent for days, at 10
    monkeypatch.setattr(S, "PRUNE_BATCH", 50)
    calls, answers = [], []
    real_floor = S._floor

    def floor(conn, now):
        calls.append(1)
        if len(calls) == 2:                              # between two of fast's batches ...
            conn.execute("COMMIT")
            got = list(S.export(d, 100, epoch, H.SKIP_TABLES, hub="slow"))  # ... slow comes back
            answers.append(got)
            conn.execute("BEGIN IMMEDIATE")
        return real_floor(conn, now)
    monkeypatch.setattr(S, "_floor", floor)
    out = list(S.export(a, 600, epoch, H.SKIP_TABLES, hub="fast"))
    assert out[-1].get("end")
    (got,) = answers
    # The first batch dropped the log to 50, below slow's place: slow is
    # answered, with everything after 100, and the log is kept back to it.
    assert got[-1].get("end") and sum(1 for r in got if "r" in r) == 500
    assert int(S._meta(a, "pruned")) == 100
    a.close()
    d.close()


# --- found by the third review of 1.17.0 ----------------------------------------------------

def test_a_site_restored_after_a_whole_copy_is_copied_again(tmp_path):
    """The hub's place after a whole copy carries the token --init gave it."""
    import shutil
    rnd = random.Random(12)
    db = site_db(tmp_path / "site.db")
    site = local_site(db)
    hub = tmp_path / "hub.db"
    mutate(db, rnd, 0)
    bootstrap(hub, site)
    sync_once(hub, site)
    backup = tmp_path / "backup.db"
    shutil.copyfile(db, backup)
    for step in range(1, 4):
        mutate(db, rnd, step)
        sync_once(hub, site)
    bootstrap(hub, site)                                # a whole copy (--full, say)
    shutil.copyfile(backup, db)                         # then the site restored
    for step in range(4, 12):
        mutate(db, rnd, step)
    assert sync_once(hub, site).get("copied")
    assert contents(hub, "alpha") == full_copy(db)


def test_answers_the_hub_did_not_apply_never_force_a_copy(tmp_path):
    rnd = random.Random(13)
    db = site_db(tmp_path / "site.db")
    site = local_site(db)
    hub = tmp_path / "hub.db"
    mutate(db, rnd, 0)
    bootstrap(hub, site)
    conn = H.connect(hub)
    for _ in range(4):                                  # answered, never applied
        mutate(db, rnd, 1)
        with H.fetch_delta(site, H.get_state(conn, "alpha")) as answer:
            assert list(answer.records())[-1].get("token")
    conn.close()
    assert "copied" not in sync_once(hub, site)
    assert contents(hub, "alpha") == full_copy(db)
    # The same hub under another name is told it is new, not that the site was restored.
    c = S._connect(str(db))
    state = H.get_state(sqlite3.connect(hub), "alpha")
    first = next(S.export(c, state["seq"], state["epoch"], H.SKIP_TABLES, hub="moved",
                          token=state["token"]))
    assert first["need_copy"].startswith("this hub is new to this log")
    c.close()


def test_a_whole_export_that_fails_falls_back_to_the_live_file(tmp_path, monkeypatch):
    """It may run for an hour: after it fails, the site's live file is copied
    instead (as 1.16 did), and the whole export is not tried again for a day."""
    import nomad.cli as cli_mod
    run, sites, out = _cli_hub(tmp_path, monkeypatch)
    assert run().exit_code == 0
    real = cli_mod._pull_via_backup
    pulls, wholes = [], []

    def no_snapshot(*a, why=None, how=None, refuse_live=False, **k):
        pulls.append(refuse_live)
        if refuse_live:
            how.append("no snapshot")
            why.append("no snapshot can be taken there")
            return False
        return real(*a, why=why, how=how, **k)

    def whole_times_out(*a, **k):
        wholes.append(1)
        raise H.DeltaError(f"no answer within {H.WHOLE_SECONDS} s")
    monkeypatch.setattr(cli_mod, "_pull_via_backup", no_snapshot)
    monkeypatch.setattr(H, "fetch_whole", whole_times_out)
    outs = [run("--full").output for _ in range(3)]
    assert all("alpha: OK" in o for o in outs), outs
    assert "again at each sync while its exporter could not send it whole" in outs[0]
    assert wholes == [1] and pulls == [True, False, False, False]
    assert contents(out, "alpha") == full_copy_of(sites[0][1], tmp_path)


def test_copies_left_by_a_killed_hub_are_removed(tmp_path, monkeypatch):
    import time
    run, sites, out = _cli_hub(tmp_path, monkeypatch)
    assert run().exit_code == 0
    cache = tmp_path / "home/.local/share/nomad/sync_cache"
    old = time.time() - 3 * 86400
    left = [cache / "alpha.4242.copy.db", cache / "alpha.4242.copy.db-journal",
            cache / "alpha.4242.copy.db.4242.part", cache / "delta_alpha_x.part"]
    for p in left:
        p.write_bytes(b"x" * 100)
        os.utime(p, (old, old))
    assert run().exit_code == 0
    assert not any(p.exists() for p in left)
