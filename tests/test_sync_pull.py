# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""How `nomad sync` copies a site's database (nomad.db.pull), and what it
says when a site keeps failing.

Found on a real hub: a site's database grew past the room left in its login
node's /tmp, where the snapshot was written; two snapshots left behind by
copies cut off at a fixed two-minute limit had filled that /tmp; a failed
copy had destroyed the hub's cached copy; the log showed the site's login
banner instead of the error; and nothing was said anywhere but the hub's log.

The remote side here is played by small stand-ins for ssh, scp and sqlite3
that run the same commands in a local directory standing for the site.
"""
import os
import sqlite3
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nomad.db import pull as P

BANNER = ("This is a University computer system.\n\nAll data contained within "
          "these systems is owned by the University, and may be monitored, intercepted, "
          "recorded, read, copied, or captured in any manner and disclosed in any manner.\n")

SSH = r'''#!{py}
import os, subprocess, sys, time
args = sys.argv[1:]
i = 0
while i < len(args) and args[i].startswith("-"):
    i += 2 if args[i] in ("-o", "-i", "-p") else 1
cmd = args[i + 1] if i + 1 < len(args) else ""
sys.stderr.write(os.environ.get("FAKE_BANNER", ""))
sys.stderr.flush()
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("ssh\t" + cmd + "\n")
env = dict(os.environ, HOME=os.environ["FAKE_REMOTE_HOME"])
if os.environ.get("FAKE_REMOTE_PATH"):
    env["PATH"] = os.environ["FAKE_REMOTE_PATH"]
sys.exit(subprocess.run(["bash", "-c", cmd], env=env).returncode)
'''

SCP = r'''#!{py}
import os, shutil, sys, time
args = [a for a in sys.argv[1:]]
pos, i = [], 0
while i < len(args):
    if args[i] in ("-o", "-i", "-P"):
        i += 2
    elif args[i].startswith("-"):
        i += 1
    else:
        pos.append(args[i]); i += 1
src, dst = pos[-2], pos[-1]
path = src.split(":", 1)[1]
if path.startswith("~/"):
    path = os.path.join(os.environ["FAKE_REMOTE_HOME"], path[2:])
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("scp\t" + path + "\n")
mode = os.environ.get("FAKE_SCP", "")
sys.stderr.write(os.environ.get("FAKE_BANNER", ""))
if mode == "sleep":
    time.sleep(5)
if mode == "short":
    data = open(path, "rb").read()
    open(dst, "wb").write(data[:-4096])
    sys.exit(0)
if mode == "partial":
    data = open(path, "rb").read()
    open(dst, "wb").write(data[: len(data) // 2])
    sys.stderr.write("scp: Connection reset by peer\n")
    sys.exit(1)
shutil.copyfile(path, dst)
'''

SQLITE = r'''#!{py}
import os, sqlite3, sys
args = sys.argv[1:]
while args and args[0] == "-cmd":
    args = args[2:]
db, dot = args[0], args[1]
dest = dot.split(" ", 1)[1]
counter = os.environ.get("FAKE_LOCKED_ONCE")
if counter and not os.path.exists(counter):
    open(counter, "w").close()
    sys.stderr.write("Error: database is locked\n")
    sys.exit(1)
full = os.environ.get("FAKE_FULL")
if full and (full == "*" or dest.startswith(full)):
    open(dest, "wb").write(b"SQLite format 3\0" + b"x" * 1000)
    open(dest + "-journal", "wb").write(b"j" * 512)
    sys.stderr.write("Error: database or disk is full\n")
    sys.exit(1)
try:
    src = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    src.backup(out)
    out.close()
except sqlite3.Error as e:
    sys.stderr.write("Error: " + str(e) + "\n")
    sys.exit(1)
'''

DF = r'''#!{py}
import os, sys
path = sys.argv[-1].rstrip("/")
free = os.environ["FAKE_DF"].split(",")
avail = int(free[1] if path == os.environ["FAKE_TMP"].rstrip("/") else free[0]) // 1024
print("Filesystem 1024-blocks Used Available Capacity Mounted on")
print(f"/dev/x 100000000 1 {avail} 1% /")
'''


def _script(path, text):
    path.write_text(text.replace("{py}", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _site_db(path, rows=50):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
        c.executemany("INSERT INTO t (v) VALUES (?)", [("x" * 100,)] * rows)


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A site (its home, its database, its /tmp) reachable through stand-ins."""
    fake = tmp_path / "bin"
    fake.mkdir()
    _script(fake / "ssh", SSH)
    _script(fake / "scp", SCP)
    _script(fake / "sqlite3", SQLITE)
    dfbin = tmp_path / "dfbin"
    dfbin.mkdir()
    _script(dfbin / "df", DF)
    home = tmp_path / "remote"
    db = home / ".local/share/nomad/site.db"
    _site_db(db)
    rtmp = tmp_path / "rtmp"
    rtmp.mkdir()
    log = tmp_path / "log"
    log.write_text("")
    monkeypatch.setattr(P, "TMP", str(rtmp))
    # Free space as the test says, not as this machine has it.
    monkeypatch.setenv("PATH", f"{dfbin}:{fake}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DF", f"{10**12},{10**12}")
    monkeypatch.setenv("FAKE_REMOTE_HOME", str(home))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_BANNER", BANNER)
    monkeypatch.setenv("FAKE_TMP", str(rtmp))
    hub = tmp_path / "hub"
    hub.mkdir()

    class S:
        pass
    s = S()
    s.home, s.db, s.tmp, s.log, s.hub, s.dfbin, s.fake = home, db, rtmp, log, hub, dfbin, fake
    s.copy = hub / "site.db"
    s.calls = lambda kind: [line.split("\t", 1)[1] for line in log.read_text().splitlines()
                            if line.startswith(kind + "\t")]
    s.leftovers = lambda: sorted(p.name for d in (rtmp, db.parent) for p in d.iterdir()
                                 if "nomad_sync_" in p.name)
    s.room = lambda beside, tmp: monkeypatch.setenv("FAKE_DF", f"{beside},{tmp}")
    return s


def _rows(path):
    with sqlite3.connect(path) as c:
        return c.execute("SELECT COUNT(*) FROM t").fetchone()[0]


def _backups(site):
    return [c for c in site.calls("ssh") if ".backup" in c]


def test_the_snapshot_goes_to_tmp_when_it_has_room_and_is_removed(site):
    why = []
    assert P.pull("u", "site", "~/.local/share/nomad/site.db", site.copy, why=why), why
    assert _rows(site.copy) == 50
    assert len(_backups(site)) == 1 and f"{site.tmp}/nomad_sync_" in _backups(site)[0]
    assert "umask 077" in _backups(site)[0]               # private from the start
    assert site.calls("scp")[0].startswith(f"{site.tmp}/nomad_sync_")
    assert site.leftovers() == [] and list(site.hub.glob("*.part")) == []


def test_beside_the_database_when_tmp_has_no_room(site):
    site.room(beside=10**9, tmp=10_000)
    assert P.pull("u", "site", "~/.local/share/nomad/site.db", site.copy)
    assert site.calls("scp")[0].startswith(f"{site.db.parent}/.nomad_sync_")
    assert site.leftovers() == []


def test_no_room_anywhere_is_said_and_nothing_is_tried(site):
    _site_db(site.copy, rows=7)                         # the hub's last good copy
    site.room(beside=10_000, tmp=20_000)
    why = []
    assert not P.pull("u", "site", "~/.local/share/nomad/site.db", site.copy, why=why)
    assert why and why[0].startswith("no room for a snapshot: the database is")
    assert not _backups(site) and site.calls("scp") == []
    assert _rows(site.copy) == 7                        # untouched


def test_tmp_full_after_all_falls_back_beside_the_database(site, monkeypatch):
    monkeypatch.setenv("FAKE_FULL", str(site.tmp))      # someone filled /tmp meanwhile
    why = []
    assert P.pull("u", "site", "~/.local/share/nomad/site.db", site.copy, why=why), why
    b = _backups(site)
    assert len(b) == 2 and str(site.tmp) in b[0] and f"{site.db.parent}/.nomad_sync_" in b[1]
    assert _rows(site.copy) == 50 and site.leftovers() == []


def test_a_full_disk_says_so_not_the_banner_and_leaves_nothing(site, monkeypatch):
    _site_db(site.copy, rows=7)
    monkeypatch.setenv("FAKE_FULL", "*")
    why = []
    assert not P.pull("u", "site", "~/.local/share/nomad/site.db", site.copy, why=why)
    assert why == ["snapshot failed: Error: database or disk is full"]
    assert len(_backups(site)) == 2                     # /tmp, then beside the database
    assert site.calls("scp") == []                      # never the live database
    assert site.leftovers() == []                       # snapshots and journals removed
    assert _rows(site.copy) == 7


def test_the_banner_never_hides_the_error(site):
    why = []
    assert not P.pull("u", "site", "~/.local/share/nomad/missing/x.db", site.copy, why=why)
    assert why == ["no database at ~/.local/share/nomad/missing/x.db"]
    err = "Error: unable to open database file\n"
    assert P.error_text(BANNER + err, BANNER) == "Error: unable to open database file"
    assert P.error_text(BANNER + err) == "Error: unable to open database file"   # banner unknown
    assert P.error_text(BANNER + "x\ny\nz\nw\n", BANNER) == "y | z | w"


def test_a_cut_off_copy_keeps_the_last_good_copy(site, monkeypatch):
    _site_db(site.copy, rows=7)
    monkeypatch.setenv("FAKE_SCP", "partial")
    why = []
    assert not P.pull("u", "site", "~/.local/share/nomad/site.db", site.copy, why=why)
    assert why == ["copy failed: scp: Connection reset by peer"]
    assert _rows(site.copy) == 7 and list(site.hub.glob("*.part")) == []
    assert site.leftovers() == []


def test_a_short_copy_is_not_taken(site, monkeypatch):
    _site_db(site.copy, rows=7)
    monkeypatch.setenv("FAKE_SCP", "short")
    why = []
    assert not P.pull("u", "site", "~/.local/share/nomad/site.db", site.copy, why=why)
    assert why and why[0].startswith("copy incomplete:")
    assert _rows(site.copy) == 7 and list(site.hub.glob("*.part")) == []


def test_a_copy_past_its_time_limit_is_never_followed_by_the_live_database(site, monkeypatch):
    _site_db(site.copy, rows=7)
    monkeypatch.setenv("FAKE_SCP", "sleep")
    monkeypatch.setattr(P, "limit", lambda size, floor, rate: 1 if rate == P.COPY_RATE else 60)
    why = []
    assert not P.pull("u", "site", "~/.local/share/nomad/site.db", site.copy, why=why)
    assert why == ["copy not finished within 1 s"]
    scps = site.calls("scp")
    assert len(scps) == 1 and "nomad_sync_" in scps[0]  # the snapshot only, once
    assert site.leftovers() == []                       # removed even so
    assert _rows(site.copy) == 7


def test_leftovers_older_than_two_hours_go_and_nothing_else(site):
    old = time.time() - 3 * 3600

    def make(path, data=b"x" * 100, when=old):
        path.write_bytes(data)
        os.utime(path, (when, when))
        return path
    make(site.tmp / "nomad_sync_123.db")
    make(site.tmp / "nomad_sync_123.db-journal")
    make(site.db.parent / ".nomad_sync_9_ab.db")
    recent = make(site.tmp / "nomad_sync_456.db", when=time.time())   # another sync's
    keep = [make(site.tmp / "someone_else.db"),
            make(site.db.parent / "nomad_sync_notes.txt"),             # not nomad's name
            make(site.db.parent / "nomad_sync_7.db")]                  # beside: only hidden ones
    folder = site.tmp / "nomad_sync_dir.db"
    folder.mkdir()
    os.utime(folder, (old, old))
    assert P.pull("u", "site", "~/.local/share/nomad/site.db", site.copy)
    assert sorted(p.name for p in site.tmp.iterdir()) == [
        "nomad_sync_456.db", "nomad_sync_dir.db", "someone_else.db"]
    assert recent.exists() and all(p.exists() for p in keep) and folder.is_dir()
    assert not (site.db.parent / ".nomad_sync_9_ab.db").exists()


def test_a_symlinked_database_counts_its_real_size(site, monkeypatch):
    real = site.home / "data" / "real.db"
    _site_db(real, rows=2000)
    link = site.db.parent / "link.db"
    link.symlink_to(real)
    out = subprocess.run(["bash", "-c", P.probe_command("~/.local/share/nomad/link.db")],
                         capture_output=True, text=True, env=dict(os.environ, HOME=str(site.home)))
    assert int(P.after_marker(out.stdout)["SIZE"]) == real.stat().st_size
    # A symlinked directory: its leftovers are found where they really are.
    linkdir = site.home / "linked"
    linkdir.symlink_to(real.parent, target_is_directory=True)
    old = time.time() - 3 * 3600
    stale = real.parent / ".nomad_sync_1_aa.db"
    stale.write_bytes(b"x")
    os.utime(stale, (old, old))
    assert P.pull("u", "site", "~/linked/real.db", site.copy)
    assert _rows(site.copy) == 2000 and not stale.exists()


def test_a_lock_is_waited_out(site, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_LOCKED_ONCE", str(tmp_path / "locked"))
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    assert P.pull("u", "site", "~/.local/share/nomad/site.db", site.copy)
    assert len([c for c in site.calls("ssh") if ".backup" in c]) == 2
    assert site.leftovers() == []


def test_without_sqlite3_the_database_itself_is_copied(site, monkeypatch, tmp_path):
    import shutil
    tools = tmp_path / "remote-bin"           # only what the commands use: no sqlite3
    tools.mkdir()
    for tool in ("bash", "sh", "dirname", "find", "id", "stat", "wc", "df", "awk", "rm",
                 "cat", "timeout"):
        if shutil.which(tool):
            (tools / tool).symlink_to(shutil.which(tool))
    monkeypatch.setenv("FAKE_REMOTE_PATH", str(tools))
    assert P.pull("u", "site", "~/.local/share/nomad/site.db", site.copy)
    assert site.calls("scp") == [str(site.db)]
    assert _rows(site.copy) == 50


def test_an_unreachable_site(site, monkeypatch):
    monkeypatch.setenv("FAKE_REMOTE_PATH", "/nonexistent")   # bash itself can't be found
    why = []
    assert not P.pull("u", "site", "~/.local/share/nomad/site.db", site.copy, why=why)
    assert why and why[0].startswith("ssh failed")


def test_the_plan_and_limits():
    gb = 10**9
    assert P.plan_snapshot(3 * gb, 4000 * gb, True, 8 * gb) == ["tmp", "beside"]
    assert P.plan_snapshot(3 * gb, 4000 * gb, True, 2.5 * gb) == ["beside"]   # the incident
    assert P.plan_snapshot(3 * gb, 4000 * gb, False, 8 * gb) == ["tmp"]
    assert P.plan_snapshot(3 * gb, 4000 * gb, True, 3.3 * gb) == ["beside", "tmp"]  # tight /tmp last
    assert P.plan_snapshot(3 * gb, 1 * gb, True, 2.5 * gb) == []
    assert P.plan_snapshot(3 * gb, 3 * gb, True, None) == []          # the margin counts
    assert P.limit(None, 120, P.COPY_RATE) == 120
    assert P.limit(3 * gb, 120, P.COPY_RATE) > 600
    assert P.limit(10**15, 120, P.COPY_RATE) == P.MAX_SECONDS
    assert P.remote_word("~/a b/x.db") == '"$HOME"/' + "'a b/x.db'"
    assert P.after_marker("motd KEY=1\n" + P.BEGIN + "\nSIZE=5\n") == {"SIZE": "5"}


# --- the sync command: what it records and says about a failing site --------

def _hub_site(path, host):
    from tests.conftest import _bootstrap_db_with_migrations
    _bootstrap_db_with_migrations(str(path))
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO node_state (timestamp, node_name, state, is_healthy)"
                  " VALUES (?, ?, 'IDLE', 1)", (datetime.now().isoformat(), f"{host}-n01"))


def test_a_failing_site_is_merged_from_its_cached_copy_and_said_so(tmp_path, monkeypatch):
    import shutil
    import nomad.cli as cli_mod
    from click.testing import CliRunner

    home = tmp_path / "home"
    (home / ".local/share/nomad/sync_cache").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    good = {}
    for name in ("alpha", "beta"):
        good[name] = tmp_path / f"{name}.db"
        _hub_site(good[name], f"{name}-head")
    cfg = tmp_path / "sync.toml"
    cfg.write_text('[[sites]]\nname = "alpha"\nhost = "alpha-head"\nssh_user = "x"\n\n'
                   '[[sites]]\nname = "beta"\nhost = "beta-head"\nssh_user = "x"\n')
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "offline"))
    raised = []
    import nomad.alerts.dispatcher as disp
    monkeypatch.setattr(disp, "send_alert", lambda *a, **k: raised.append((a, k)) or {})
    out = tmp_path / "combined.db"

    def live(user, host, remote_db, local_copy, ssh_key=None, why=None):
        shutil.copyfile(good[host.split("-")[0]], local_copy)
        return True
    monkeypatch.setattr(cli_mod, "_pull_via_backup", live)
    r = CliRunner().invoke(cli_mod.cli, ["sync", "-c", str(cfg), "-o", str(out)])
    assert r.exit_code == 0, r.output
    first = dict(sqlite3.connect(out).execute("SELECT name, last_success FROM sync_sites"))
    assert raised == []

    def beta_full(user, host, remote_db, local_copy, ssh_key=None, why=None):
        if host.startswith("beta"):
            why.append("snapshot failed: Error: database or disk is full")
            return False
        return live(user, host, remote_db, local_copy)
    monkeypatch.setattr(cli_mod, "_pull_via_backup", beta_full)
    r = CliRunner().invoke(cli_mod.cli, ["sync", "-c", str(cfg), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "beta: from its cached copy (live pull failed: snapshot failed: Error: database or disk is full)" in r.output
    assert "why: snapshot failed" in r.output
    rows = {n: (e, s, ls) for n, e, s, ls in sqlite3.connect(out).execute(
        "SELECT name, error, source, last_success FROM sync_sites")}
    assert rows["beta"] == ("snapshot failed: Error: database or disk is full", "cache", first["beta"])
    assert rows["alpha"][0] is None and rows["alpha"][1] == "live" and rows["alpha"][2] > first["alpha"]
    # Within the first two hours: nothing raised yet.
    assert raised == []


def test_a_site_that_keeps_failing_raises_an_alert(tmp_path, monkeypatch):
    import nomad.alerts.dispatcher as disp
    from nomad.cli import _sync_alerts
    db = tmp_path / "combined.db"
    now = datetime(2026, 10, 12, 11, 0)
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE sync_sites (name TEXT, last_success TEXT)")
        c.executemany("INSERT INTO sync_sites VALUES (?, ?)", [
            ("recent", (now - timedelta(minutes=30)).isoformat()),
            ("hours", (now - timedelta(hours=3)).isoformat()),
            ("days", (now - timedelta(hours=45)).isoformat()),
            ("never", None)])
    sent = []
    monkeypatch.setattr(disp, "send_alert", lambda *a, **k: sent.append((a, k)) or {})
    errors = dict.fromkeys(("recent", "hours", "days", "never"),
                           "snapshot failed: Error: database or disk is full")
    raised = _sync_alerts(db, errors, now=now, config={})
    assert [(r["site"], r["severity"]) for r in raised] == [
        ("days", "critical"), ("hours", "warning"), ("never", "warning")]
    assert "not synced for 45 h" in raised[0]["message"]
    assert "database or disk is full" in raised[0]["message"]
    (sev, source, message), kw = sent[0]
    assert source == "sync" and kw["subject"] == "days" and kw["details"]["metric"] == "pull"
    # The hub's own threshold.
    sent.clear()
    raised = _sync_alerts(db, {"recent": "x"}, now=now, config={"hub": {"alert_after_minutes": 20}})
    assert [r["site"] for r in raised] == ["recent"]
    assert _sync_alerts(db, {}, now=now, config={}) == []


def test_alerts_are_sent_once_then_daily_and_forgotten_on_recovery(tmp_path, monkeypatch):
    import nomad.alerts.dispatcher as disp
    from nomad.cli import _sync_alerts
    cache = tmp_path / "sync_cache"
    cache.mkdir()
    db = tmp_path / "combined.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE sync_sites (name TEXT, last_success TEXT)")
    t0 = datetime(2026, 10, 10, 14, 30)
    cached = cache / "cluster-a.db"                      # the last complete copy: its time counts
    cached.write_bytes(b"x")
    os.utime(cached, (t0.timestamp(), t0.timestamp()))
    sent = []
    monkeypatch.setattr(disp, "send_alert", lambda *a, **k: sent.append(a[0]) or {})
    full = {"cluster-a": "snapshot failed: Error: database or disk is full"}

    def run(hours):
        return _sync_alerts(db, full, cache, now=t0 + timedelta(hours=hours), config={})
    assert run(1) == [] and sent == []                 # not yet 2 h
    assert [r["severity"] for r in run(2.5)] == ["warning"]
    assert run(2.8) == [] and run(10) == []            # once, not every 20 minutes
    assert [r["severity"] for r in run(25)] == ["critical"]   # worse: sent again
    assert run(30) == []
    assert [r["severity"] for r in run(49.5)] == ["critical"]  # daily reminder
    assert "not synced for 3 days" in run(73.5)[0]["message"]
    assert sent == ["warning", "critical", "critical", "critical"]
    # It syncs again: forgotten, so its next failure is news.
    _sync_alerts(db, {}, cache, now=t0 + timedelta(hours=80), config={})
    assert (cache / "sync_alerts.json").read_text().strip() == "{}"
    os.utime(cached, ((t0 + timedelta(hours=80)).timestamp(),) * 2)
    assert [r["severity"] for r in run(83)] == ["warning"]


def test_a_stuck_combined_database_is_an_alert_too(tmp_path, monkeypatch):
    import nomad.alerts.dispatcher as disp
    from nomad.cli import _sync_alerts
    db = tmp_path / "combined.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE sync_sites (name TEXT, last_success TEXT)")
    then = time.time() - 45 * 3600
    os.utime(db, (then, then))
    monkeypatch.setattr(disp, "send_alert", lambda *a, **k: {})
    raised = _sync_alerts(db, {}, tmp_path, blocked="merged 4 site(s), the published one has 5",
                          config={})
    assert [(r["site"], r["severity"]) for r in raised] == [("combined.db", "critical")]
    assert "not been replaced for 45 h" in raised[0]["message"]
    assert "merged 4 site(s)" in raised[0]["message"]
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert _sync_alerts(tmp_path / "missing.db", {}, fresh, blocked="x", config={}) == []


def test_a_site_with_no_cached_copy_is_reported_and_the_old_build_kept(tmp_path, monkeypatch):
    import shutil
    import nomad.cli as cli_mod
    from click.testing import CliRunner

    home = tmp_path / "home"
    cache = home / ".local/share/nomad/sync_cache"
    cache.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    good = {}
    for name in ("alpha", "beta"):
        good[name] = tmp_path / f"{name}.db"
        _hub_site(good[name], f"{name}-head")
    cfg = tmp_path / "sync.toml"
    cfg.write_text('alert_after_minutes = 1\n\n'
                   '[[sites]]\nname = "alpha"\nhost = "alpha-head"\nssh_user = "x"\n\n'
                   '[[sites]]\nname = "beta"\nhost = "beta-head"\nssh_user = "x"\n')
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "offline"))
    seen = []
    monkeypatch.setattr(cli_mod, "_sync_alerts",
                        lambda *a, **k: seen.append((a, k)) or [])

    def live(user, host, remote_db, local_copy, ssh_key=None, why=None):
        shutil.copyfile(good[host.split("-")[0]], local_copy)
        return True
    monkeypatch.setattr(cli_mod, "_pull_via_backup", live)
    out = tmp_path / "combined.db"
    assert CliRunner().invoke(cli_mod.cli, ["sync", "-c", str(cfg), "-o", str(out)]).exit_code == 0
    before = out.stat().st_mtime_ns
    (cache / "beta.db").unlink()                       # no cached copy to stand in

    def beta_down(user, host, remote_db, local_copy, ssh_key=None, why=None):
        if host.startswith("beta"):
            why.append("ssh failed: Connection timed out")
            return False
        return live(user, host, remote_db, local_copy)
    monkeypatch.setattr(cli_mod, "_pull_via_backup", beta_down)
    seen.clear()
    r = CliRunner().invoke(cli_mod.cli, ["sync", "-c", str(cfg), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "beta: pull failed, no cached copy: ssh failed: Connection timed out" in r.output
    assert "beta: not pulled: ssh failed: Connection timed out" in r.output
    assert out.stat().st_mtime_ns == before            # the old build stays published
    (args, kw), = seen
    assert args[1] == {"beta": "ssh failed: Connection timed out"}
    assert kw["blocked"] == "merged 1 site(s), the published one has 2"
    assert kw["after"] == 1                            # from the sync file


def test_an_alert_that_could_not_be_delivered_is_tried_again(tmp_path, monkeypatch):
    import nomad.alerts.dispatcher as disp
    from nomad.cli import _sync_alerts
    db = tmp_path / "combined.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE sync_sites (name TEXT, last_success TEXT, synced_at TEXT)")
        c.execute("INSERT INTO sync_sites VALUES ('cluster-a', '2026-10-10T14:20:00', "
                  "'2026-10-10T14:20:00')")
    now = datetime(2026, 10, 10, 18, 0)
    outcome = {"EmailBackend": False}
    monkeypatch.setattr(disp, "send_alert", lambda *a, **k: dict(outcome))
    full = {"cluster-a": "snapshot failed: Error: database or disk is full"}
    assert len(_sync_alerts(db, full, tmp_path, now=now, config={})) == 1
    assert len(_sync_alerts(db, full, tmp_path, now=now + timedelta(minutes=20), config={})) == 1
    outcome["EmailBackend"] = True                       # the relay is back
    assert len(_sync_alerts(db, full, tmp_path, now=now + timedelta(minutes=40), config={})) == 1
    assert _sync_alerts(db, full, tmp_path, now=now + timedelta(minutes=60), config={}) == []
    # The published build's own time, not the file's, says how old it is.
    os.utime(db, None)
    raised = _sync_alerts(db, {}, tmp_path, blocked="merged 4 site(s), the published one has 5",
                          now=now, config={})
    assert [(r["site"], r["severity"]) for r in raised] == [("combined.db", "warning")]
