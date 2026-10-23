"""nomad 1.17.1: storage (account floor, copies per path, nested scan paths,
owner mode), the hub's index and thinning, the per-user report's host roles,
`nomad sync status`, and the storage.use and sync.status actions."""
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nomad import storage
from nomad.collectors import storage_use as su
from nomad.storage import views

SH = shutil.which("dash") or shutil.which("sh")
REAL_PATH = "PATH=/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin"
GNU_TEST = "if find / -maxdepth 0 -printf '' >/dev/null 2>&1; then GNUFIND=1; else GNUFIND=; fi"
ROOT = os.geteuid() == 0


def cli_main():
    from nomad.cli import cli
    return cli


def _script(tmp_path, bsd=False) -> Path:
    text = storage.script_text()
    assert REAL_PATH in text and GNU_TEST in text
    text = text.replace(REAL_PATH, 'PATH=${PATH:+$PATH:}' + REAL_PATH[5:])
    if bsd:
        text = text.replace(GNU_TEST, "GNUFIND=")
    p = tmp_path / ("nomad-storage-bsd.sh" if bsd else "nomad-storage.sh")
    p.write_text(text)
    return p


def _run(tmp_path, *args, env=None, check=True, bsd=False):
    e = dict(os.environ)
    e.update(env or {})
    r = subprocess.run([SH, str(_script(tmp_path, bsd=bsd)), *args], capture_output=True, text=True,
                       env=e, timeout=120)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def _scan(tmp_path, out, *args, env=None):
    _run(tmp_path, "-d", str(out), *args, env=env)
    return storage.parse((out / "scan.txt").read_text())


def _names(monkeypatch, known):
    """This machine's accounts, for the test: uid -> name."""
    def getpwuid(uid):
        if uid in known:
            return SimpleNamespace(pw_name=known[uid])
        raise KeyError(uid)
    monkeypatch.setattr(su.pwd, "getpwuid", getpwuid)


SCAN = ("#nomad-storage\t1\tscan\n#started\t1700000000\n#ages\t180\t365\n#present\t/home\nP\tsys\nP\tann\n"
        "#top\t/home\tsys\t2\t1700000000\nF\tcache\t2\t4096\t1\t1700000000\t0\t0\n#rc\t0\n#denied\t0\n#endtop\t1\n"
        "#top\t/home\tann\t5001\t1700000000\nF\tdata\t5001\t8192\t2\t1700000000\t0\t0\n"
        "F\tother\t5002\t4096\t1\t1700000000\t0\t0\n#rc\t0\n#denied\t0\n#endtop\t1\n"
        "#not-scanned\t0\n#finished\t1700000001\n")


# --- A1: the account floor ----------------------------------------------------------------

def test_scan_owners_are_named_only_where_this_machine_may_name_them(tmp_path, monkeypatch):
    """The scan's owners are uids: named as this machine knows them for its own
    files, for a server sharing its accounts only at or above the floor, and
    for any other server never (they were named whatever the server)."""
    _names(monkeypatch, {2: "sysacct", 5001: "ann", 5002: "ben"})
    rep = storage.parse(SCAN)
    cases = {
        "local": ({"names": "local"}, {"sys": "sysacct", "ann": "ann"}, {"cache": "sysacct", "data": "ann",
                                                                          "other": "ben"}),
        "same": ({"names": "same", "account_floor": 1000}, {"sys": None, "ann": "ann"},
                 {"cache": None, "data": "ann", "other": "ben"}),
        "same-high": ({"names": "same", "account_floor": 5002}, {"sys": None, "ann": None},
                      {"cache": None, "data": None, "other": "ben"}),
        "none": ({"names": "none"}, {"sys": None, "ann": None}, {"cache": None, "data": None, "other": None}),
        "a record without names, not same_accounts": ({}, {"sys": None, "ann": None},
                                                      {"cache": None, "data": None, "other": None}),
    }
    for i, (what, (src, tops, folders)) in enumerate(cases.items()):
        db = tmp_path / f"site{i}.db"
        c = su.StorageUseCollector({"sources": []}, db)
        c.store([{"type": "storage_report", "server": "fs1", "read_at": "2026-10-23T01:00:00", "report": rep,
                  **src}])
        conn = sqlite3.connect(db)
        assert dict(conn.execute("SELECT top, top_owner FROM storage_tops")) == tops, what
        assert dict(conn.execute("SELECT folder, owner FROM storage_folders")) == folders, what
    # Unnamed, an owner goes by number in the views, never by the folder's name.
    assert set(views.folders_by_owner(conn, "fs1", "/home")) == {"uid 2", "uid 5001"}


def test_the_floor_is_set_per_source_or_for_all(tmp_path):
    ok, problems = su.sources_from({"account_floor": 10000, "sources": [
        {"server": "fs1", "host": "fs1", "same_accounts": True},
        {"server": "fs2", "host": "fs2", "same_accounts": True, "account_floor": 500},
        {"server": "fs3", "host": "fs3"},
        {"server": "here"},
        {"server": "here2", "same_accounts": False},
        {"server": "bad1", "account_floor": -1},
        {"server": "bad2", "account_floor": "1000"},
        {"server": "bad3", "account_floor": True}]})
    got = {s["server"]: (s["names"], s["account_floor"]) for s in ok}
    assert got == {"fs1": ("same", 10000), "fs2": ("same", 500), "fs3": ("none", 10000),
                   "here": ("local", 10000), "here2": ("none", 10000)}
    assert len(problems) == 3 and all("account_floor" in p for p in problems)
    ok, problems = su.sources_from({"sources": [{"server": "fs1", "host": "fs1", "same_accounts": True}]})
    assert ok[0]["account_floor"] == su.ACCOUNT_FLOOR == 1000 and not problems
    ok, problems = su.sources_from({"account_floor": "lots", "sources": [{"server": "fs1"}]})
    assert ok[0]["account_floor"] == 1000 and "account_floor" in problems[0]


def test_usage_owners_follow_the_floor_too(tmp_path, monkeypatch):
    _names(monkeypatch, {999: "svc", 1000: "first", 20000: "zed"})
    text = ("#nomad-storage\t1\tusage\n#started\t1700000000\n#path\t/home\txfs\t-\n#xfs-users\n"
            "#999        2048      0      0     00 [--------]\n"
            "#1000       2048      0      0     00 [--------]\n"
            "#20000      1024      0      0     00 [--------]\n#finished\t1700000001\n")
    rep = storage.parse(text)
    for floor, expected in ((1000, {"999", "first", "zed"}), (10000, {"999", "1000", "zed"})):
        db = tmp_path / f"s{floor}.db"
        c = su.StorageUseCollector({"sources": []}, db)
        c.store([{"type": "storage_report", "server": "fs1", "read_at": "2026-10-23T01:00:00", "report": rep,
                  "names": "same", "account_floor": floor}])
        assert {n for (n,) in sqlite3.connect(db).execute("SELECT name FROM storage_owners")} == expected


# --- A2: copies per path -------------------------------------------------------------------

def _usage(server_paths):
    lines = ["#nomad-storage\t1\tusage", "#started\t1700000000"]
    for path, owners in server_paths:
        lines += [f"#path\t{path}\tzfs\ttank{path}", f"#df\ttank{path}\t1000000\t500000\t500000\t50%\t{path}",
                  f"#zfs-users\ttank{path}"]
        lines += [f"POSIX User\t{n}\t{b}\t0" for n, b in owners]
    lines.append("#finished\t1700000001")
    return storage.parse("\n".join(lines) + "\n")


def test_copies_name_the_paths_of_a_server_that_hold_copies(tmp_path):
    ok, problems = su.sources_from({"sources": [
        {"server": "bk", "host": "bk", "copies": {"/backup/home/": "fs0:/home", "/backup/proj": "fs0"}},
        {"server": "b1", "copies": {"home": "fs0:/home"}},
        {"server": "b2", "copies": {"/x": "fs0:home"}},
        {"server": "b3", "copies": {"/x": "a;b"}},
        {"server": "b4", "copies": ["/x"]}]})
    assert [s["server"] for s in ok] == ["bk"] and len(problems) == 4
    assert ok[0]["copies"] == {"/backup/home": "fs0:/home", "/backup/proj": "fs0"}
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": []}, db)
    rep = _usage([("/backup/home", [("ann", 4 * 10**12)]), ("/scratch", [("ann", 10**12)])])
    c.store([{"type": "storage_report", "server": "bk", "read_at": "2026-10-23T01:00:00", "report": rep,
              "copies": ok[0]["copies"]},
             {"type": "storage_report", "server": "fs0", "read_at": "2026-10-23T01:00:00",
              "report": _usage([("/home", [("ann", 4 * 10**12)])])}])
    conn = sqlite3.connect(db)
    ps = {(p["server"], p["path"]): p for p in views.paths(conn)}
    assert (ps[("bk", "/backup/home")]["copy_of"], ps[("bk", "/backup/home")]["copy_of_path"]) == ("fs0", "/home")
    assert ps[("bk", "/scratch")]["copy_of"] is None and ps[("fs0", "/home")]["copy_of"] is None
    rows = views.person(conn, "ann")
    assert {(r["server"], r["path"]): r["copy_of"] for r in rows} == {
        ("bk", "/backup/home"): "fs0", ("bk", "/scratch"): None, ("fs0", "/home"): None}
    # The copy isn't counted twice: what ann holds is /home and /scratch.
    assert views.totals(rows) == {"total_bytes": 5 * 10**12, "copies_bytes": 4 * 10**12}
    out = CliRunner().invoke(cli_main(), ["storage", "use", "--db", str(db)]).output
    block = out.split("bk:/backup/home")[1].split("bk:/scratch")[0]
    assert "holds copies of fs0:/home's files" in block
    assert "holds copies" not in out.split("bk:/scratch")[1]
    out = CliRunner().invoke(cli_main(), ["storage", "use", "--db", str(db), "--list"]).output
    assert "copies of fs0:/home's files, not added to totals" in out
    out = CliRunner().invoke(cli_main(), ["storage", "status", "--db", str(db)]).output
    assert "/backup/home holds copies of fs0:/home's files" in out


def test_a_whole_server_copy_still_counts_every_path(tmp_path):
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": []}, db)
    c.store([{"type": "storage_report", "server": "bk", "read_at": "2026-10-23T01:00:00",
              "report": _usage([("/a", [("ann", 10)]), ("/b", [("ann", 20)])]), "copy_of": "fs0"}])
    rows = views.person(sqlite3.connect(db), "ann")
    assert all(r["copy_of"] == "fs0" and r["copy_of_path"] is None for r in rows)
    assert views.totals(rows) == {"total_bytes": 0, "copies_bytes": 30}


def test_the_columns_come_to_a_table_made_before_them(tmp_path):
    db = tmp_path / "site.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE storage_reports (server TEXT NOT NULL, mode TEXT NOT NULL, started TEXT NOT NULL, "
                 "finished TEXT, host TEXT, os TEXT, version INTEGER, read_at TEXT, paths INTEGER, owners INTEGER, "
                 "tops INTEGER, not_scanned INTEGER, run_by TEXT, run_uid INTEGER, copy_of TEXT, odd_names INTEGER, "
                 "unreadable TEXT)")
    conn.commit()
    c = su.StorageUseCollector({"sources": []}, db)
    c.store([{"type": "storage_report", "server": "bk", "read_at": "2026-10-23T01:00:00",
              "report": _usage([("/a", [("ann", 10)])]), "copies": {"/a": "fs0:/z"}}])
    cols = {r[1] for r in conn.execute("PRAGMA table_info(storage_reports)")}
    assert {"copies", "nested", "scan_as", "carried", "not_measured"} <= cols
    assert json.loads(conn.execute("SELECT copies FROM storage_reports").fetchone()[0]) == {"/a": "fs0:/z"}


# --- A3: nested scan paths -----------------------------------------------------------------

def _pool(tmp_path):
    pool = tmp_path / "pool"
    (pool / "alice" / "work").mkdir(parents=True)
    (pool / "alice" / "work" / "a.bin").write_bytes(os.urandom(20_000))
    (pool / "home" / "bob" / "data").mkdir(parents=True)
    (pool / "home" / "bob" / "data" / "b.bin").write_bytes(os.urandom(30_000))
    return pool


@pytest.mark.parametrize("flavour", ["gnu", "bsd"])
def test_a_top_folder_that_is_another_scan_path_is_left_to_it(tmp_path, flavour):
    pool = _pool(tmp_path)
    (tmp_path / "homes").symlink_to(pool / "home")            # the same folder, by another name
    out = tmp_path / "out"
    env = None
    if flavour == "bsd":
        shim = tmp_path / "shim"
        shim.mkdir()
        (shim / "stat").write_text("#!/bin/sh\nif [ \"$1\" = -f ] && [ \"$2\" != -c ]; then\n"
                                   "  fmt=$(printf '%s' \"$2\" | sed -e 's/%m/%Y/g' -e 's/%l/%h/g' -e 's/%N/%n/g')\n"
                                   f"  shift 2; exec {shutil.which('stat')} -c \"$fmt\" \"$@\"\nfi\n"
                                   f"exec {shutil.which('stat')} \"$@\"\n")
        (shim / "stat").chmod(0o755)
        env = {"PATH": f"{shim}:{os.environ['PATH']}"}
    for second in (pool / "home", tmp_path / "homes"):
        _run(tmp_path, "-d", str(out), "scan", str(pool), str(second), env=env, bsd=flavour == "bsd")
        rep = storage.parse((out / "scan.txt").read_text())
        assert rep.present[str(pool)] == ["alice"]
        assert rep.nested == [(str(pool), "home", str(second))]
        assert {(t.path, t.name) for t in rep.tops} == {(str(pool), "alice"), (str(second), "bob")}
        bob = next(t for t in rep.tops if t.name == "bob")
        assert sum(f.bytes for f in bob.folders) >= 30_000          # counted once, as its own path's
        time.sleep(1.1)


def test_the_collector_drops_a_nested_folder_counted_before(tmp_path):
    pool = _pool(tmp_path)
    out = tmp_path / "out"
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": [{"server": "fs1", "dir": str(out)}], "every_minutes": 0.001}, db)
    _run(tmp_path, "-d", str(out), "scan", str(pool))          # before: home is one of pool's top folders
    c.store(c.collect())
    conn = sqlite3.connect(db)
    assert {t for (t,) in conn.execute("SELECT DISTINCT top FROM storage_tops")} == {"alice", "home"}
    time.sleep(1.1)
    _run(tmp_path, "-d", str(out), "scan", str(pool), str(pool / "home"))
    c.store(c.collect())
    assert {(p, t) for p, t in conn.execute("SELECT DISTINCT path, top FROM storage_tops")} == {
        (str(pool), "alice"), (str(pool / "home"), "bob")}
    (r,) = [r for r in views.reads(conn) if r["server"] == "fs1"]
    assert r["scan"]["nested"] == [{"path": str(pool), "top": "home", "is": str(pool / "home")}]
    out_text = CliRunner().invoke(cli_main(), ["storage", "status", "--db", str(db)]).output
    assert f"left out {pool}/home: it is {pool / 'home'}, scanned as that path" in out_text


# --- A4: which database the commands read ----------------------------------------------------

def _hub_config(tmp_path, sites: bool):
    combined = tmp_path / "combined.db"
    site_db = tmp_path / "nomad.db"
    for p in (combined, site_db):
        sqlite3.connect(p).execute("CREATE TABLE t (x)")
    cfg = tmp_path / "nomad.toml"
    text = f'[database]\npath = "{site_db}"\n\n[hub]\noutput_db = "{combined}"\n'
    if sites:
        text += '\n[[hub.sites]]\nname = "c1"\nhost = "c1-head"\n'
    cfg.write_text(text)
    return cfg, combined, site_db


@pytest.mark.parametrize("sites", [False, True])
def test_the_combined_database_is_read_only_on_a_hub(tmp_path, monkeypatch, capsys, sites):
    """A combined.db left on a host that is no hub (no [hub] sites) is not
    read in place of the host's own database; and the command says which."""
    import click

    from nomad import cli as cli_mod
    from nomad.config import load_config
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg, combined, site_db = _hub_config(tmp_path, sites)
    ctx = click.Context(cli_mod.cli, obj={"config_target": str(cfg), "config": load_config(cfg)})
    got = cli_mod._usage_db(ctx, None)
    said = capsys.readouterr().err
    assert got == (combined if sites else site_db)
    assert ("the hub's combined database" if sites else "this host's database") in said
    assert str(got) in said
    # Asked quietly (the condo status line), it says nothing.
    cli_mod._usage_db(ctx, None, need=False, say=False)
    assert capsys.readouterr().err == ""


# --- A5: the hub's index, thinning, new owners ----------------------------------------------

def test_the_hub_indexes_jobs_by_user_and_submit_time():
    from nomad.cli import SYNC_INDEXES
    assert ("jobs", "user_name, submit_time") in SYNC_INDEXES


def test_old_scans_of_a_top_folder_are_thinned_to_one_a_week(tmp_path):
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": [], "keep_days": 30}, db)
    conn = sqlite3.connect(db)
    conn.executescript(su.SCHEMA)
    now = datetime.now().replace(microsecond=0)
    made = []
    for d in range(0, 60):
        t = now - timedelta(days=d, hours=1)
        # Every night's scan finished, but Saturdays', stopped at the budget:
        # a week's latest scan isn't always the one kept.
        complete = 0 if t.weekday() == 5 else 1
        made.append((t.isoformat(), complete))
        for top in ("ann", "ben"):
            conn.execute("INSERT INTO storage_tops (scanned_at, server, path, top, complete) "
                         "VALUES (?, 'fs1', '/home', ?, ?)", (t.isoformat(), top, complete))
    conn.execute("INSERT INTO storage_tops (scanned_at, server, path, top) VALUES ('2026-x', 'fs1', '/home', 'ann')")
    cut = datetime.now() - timedelta(days=30)
    c._thin(conn)
    conn.commit()
    best = {}
    for when, complete in made:
        t = datetime.fromisoformat(when)
        if t >= cut:
            continue
        w = t.isocalendar()[:2]
        best[w] = max(best.get(w, (0, "")), (complete, when))
    expected = {when for _, when in best.values()} | {w for w, _ in made if datetime.fromisoformat(w) >= cut}
    for top in ("ann", "ben"):
        kept = {w for (w,) in conn.execute("SELECT scanned_at FROM storage_tops WHERE top = ? "
                                           "AND scanned_at != '2026-x'", (top,))}
        assert kept == expected, top
    assert len(best) >= 4 and all(cpl == 1 for cpl, _ in best.values())
    assert conn.execute("SELECT COUNT(*) FROM storage_tops WHERE scanned_at = '2026-x'").fetchone()[0] == 1


def test_a_new_owners_change_is_all_of_their_use(tmp_path):
    db = tmp_path / "site.db"
    conn = sqlite3.connect(db)
    conn.executescript(su.SCHEMA)
    then, now = "2026-09-20T12:00:00", "2026-10-21T12:00:00"
    for ts, rows in ((then, [("ann", 10)]), (now, [("ann", 15), ("ben", 7)])):
        for n, b in rows:
            conn.execute("INSERT INTO storage_owners (timestamp, server, path, kind, name, used_bytes) "
                         "VALUES (?, 'fs1', '/home', 'user', ?, ?)", (ts, n, b))
    rows = {r["name"]: r for r in views.owners(conn, "fs1", "/home")}
    assert (rows["ann"]["change_bytes"], rows["ann"]["new"]) == (5, False)
    assert (rows["ben"]["change_bytes"], rows["ben"]["new"], rows["ben"]["was_bytes"]) == (7, True, None)
    # No reading as far back: no one is new, and no change is known.
    rows = {r["name"]: r for r in views.owners(conn, "fs1", "/home", days=60)}
    assert rows["ben"]["change_bytes"] is None and not rows["ben"]["new"]


# --- A6: owner mode --------------------------------------------------------------------------

SUDO = """#!/bin/sh
# A stand-in sudo: says how it was called, refuses REFUSE (#uid), runs the rest.
printf 'sudo' >> "$SUDO_LOG"
for a; do printf ' [%s]' "$a" | tr '\\n' '~' >> "$SUDO_LOG"; done
echo >> "$SUDO_LOG"
[ "$1" = -n ] && [ "$2" = -u ] && [ "$4" = -- ] || exit 1
case " $REFUSE " in *" $3 "*) echo "sudo: a password is required" >&2; exit 1 ;; esac
shift 4
exec "$@"
"""


def _sudo(tmp_path, refuse=""):
    d = tmp_path / "sudo-shim"
    d.mkdir(exist_ok=True)
    (d / "sudo").write_text(SUDO)
    (d / "sudo").chmod(0o755)
    log = tmp_path / "sudo.log"
    log.write_text("")
    return {"PATH": f"{d}:{os.environ['PATH']}", "SUDO_LOG": str(log), "REFUSE": refuse}, log


def _calls(log):
    return [line for line in log.read_text().splitlines() if line]


def _account(name):
    try:
        return pwd.getpwnam(name)
    except KeyError:
        pytest.skip(f"no {name} account here")


def _owned_tree(tmp_path):
    """Two paths; top folders of two accounts (one with a folder on each
    path), one the stand-in sudo refuses, and this account's own."""
    a, b = _account("daemon"), _account("nobody")
    home, proj = tmp_path / "home", tmp_path / "proj"
    for top in (home / "da", home / "no", home / "me", proj / "dp"):
        (top / "sub").mkdir(parents=True)
    (home / "da" / "sub" / "x.bin").write_bytes(os.urandom(40_000))
    os.link(home / "da" / "sub" / "x.bin", home / "da" / "sub" / "x-again.bin")
    (home / "da" / "loose").write_bytes(os.urandom(1000))
    (proj / "dp" / "sub" / "y.bin").write_bytes(os.urandom(20_000))
    (home / "no" / "sub" / "z.bin").write_bytes(os.urandom(5_000))
    (home / "me" / "sub" / "m.bin").write_bytes(os.urandom(3_000))
    for top, acct in ((home / "da", a), (proj / "dp", a), (home / "no", b)):
        for p in [top, *top.rglob("*")]:
            os.lchown(p, acct.pw_uid, acct.pw_gid)
    return home, proj, a, b


@pytest.mark.skipif(not ROOT, reason="gives folders to other accounts (root only)")
def test_owner_mode_measures_each_top_folder_as_its_owner(tmp_path):
    home, proj, a, b = _owned_tree(tmp_path)
    env, log = _sudo(tmp_path, refuse=f"#{b.pw_uid}")
    out = tmp_path / "out"
    # -U 1: daemon is a system account (below 1000), measured here as any owner.
    rep = _scan(tmp_path, out, "-O", "-U", "1", "scan", str(home), str(proj), env=env)
    assert rep.scan_as == "owner" and rep.carried == 0 and rep.not_scanned == 0
    calls = _calls(log)
    # One sudo per owner (by number), for all its folders on every path:
    # find, read only; none for this account's own folder.
    assert len(calls) == 2
    da = next(c for c in calls if f"[#{a.pw_uid}]" in c)
    assert da.startswith(f"sudo [-n] [-u] [#{a.pw_uid}] [--] [{shutil.which('find')}]")
    assert f"[{home / 'da'}]" in da and f"[{proj / 'dp'}]" in da
    assert not any(w in da for w in ("[-delete]", "[-exec]", "[-execdir]", "[-fprint]", "[-ok]"))
    tops = {(t.path, t.name): t for t in rep.tops}
    assert set(tops) == {(str(home), "da"), (str(proj), "dp"), (str(home), "me")}
    assert all(t.complete for t in tops.values())
    da_top = {f.name: f for f in tops[(str(home), "da")].folders}
    assert da_top["sub"].files == 1 and da_top["sub"].bytes >= 40_000      # a hard link once
    assert da_top["sub"].owner_uid == a.pw_uid and da_top["."].files == 1
    assert tops[(str(home), "da")].owner_uid == a.pw_uid
    assert {f.name for f in tops[(str(proj), "dp")].folders} == {"sub"}
    assert rep.not_measured == [(str(home), "no", b.pw_uid, "sudo")]
    # The next night: nothing changed, nothing large: their measurements stand,
    # and sudo isn't asked again for the one it refused (it logs each refusal).
    log.write_text("")
    rep = _scan(tmp_path, out, "-O", "-U", "1", "scan", str(home), str(proj), env=env)
    assert rep.carried == 3 and not rep.tops and not _calls(log)
    assert rep.not_measured == [(str(home), "no", b.pw_uid, "sudo")]
    # A week and more later it is asked again.
    state = out / "scan.state"
    lines = [line.split("\t") for line in state.read_text().splitlines()]
    assert [x[1] for x in lines if x[2] == str(home / "no")] == ["nosudo"]
    state.write_text("".join("\t".join([str(int(x[0]) - 9 * 86400) if x[1] == "nosudo" else x[0], *x[1:]]) + "\n"
                             for x in lines))
    rep = _scan(tmp_path, out, "-O", "-U", "1", "scan", str(home), str(proj), env=env)
    assert len(_calls(log)) == 1 and f"[#{b.pw_uid}]" in _calls(log)[0]
    assert rep.not_measured == [(str(home), "no", b.pw_uid, "sudo")]
    # A folder that changed (an entry made in it) is measured again; -L 0: all are large.
    time.sleep(1.1)
    (home / "me" / "new").write_text("x")
    rep = _scan(tmp_path, out, "-O", "-U", "1", "scan", str(home), str(proj), env=env)
    assert [t.name for t in rep.tops] == ["me"] and rep.carried == 2
    log.write_text("")
    rep = _scan(tmp_path, out, "-O", "-U", "1", "-L", "0", "scan", str(home), str(proj), env=env)
    assert len(rep.tops) == 3 and rep.carried == 0
    assert [c.split("] [")[2] for c in _calls(log)] == [f"#{a.pw_uid}"]  # not the refused one
    # The collector keeps each folder's last measurement, with its time.
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": [{"server": "fs9", "dir": str(out)}], "every_minutes": 0.001}, db)
    c.store(c.collect())
    conn = sqlite3.connect(db)
    (r,) = views.reads(conn)
    assert r["scan"]["scan_as"] == "owner" and r["scan"]["not_measured_owners"] == 1
    owners = views.scan_owners(conn, "fs9", str(home))
    assert {o["name"] for o in owners} == {a.pw_name, pwd.getpwuid(os.getuid()).pw_name}
    assert all(o["scanned_at"] for o in owners)
    text = CliRunner().invoke(cli_main(), ["storage", "status", "--db", str(db)]).output
    assert "measured as each owner" in text and "not measured: 1 owners, 1 top folders" in text
    assert "sudo wouldn't act as them (asked again after a week)" in text
    text = CliRunner().invoke(cli_main(), ["storage", "use", "--db", str(db)]).output
    assert "measured" in text and "each as of when measured" in text


@pytest.mark.skipif(not ROOT, reason="gives folders to other accounts (root only)")
def test_owner_mode_never_asks_sudo_for_system_accounts(tmp_path):
    """Below -U (1000), an owner is a system account: not measured, and sudo
    isn't asked (it would log a refusal every night)."""
    home, proj, a, b = _owned_tree(tmp_path)
    if a.pw_uid >= 1000 or b.pw_uid < 1000:
        pytest.skip("daemon or nobody numbered unusually here")
    env, log = _sudo(tmp_path)
    rep = _scan(tmp_path, tmp_path / "out", "-O", "scan", str(home), str(proj), env=env)
    assert sorted(rep.not_measured) == [(str(home), "da", a.pw_uid, "system"), (str(proj), "dp", a.pw_uid, "system")]
    assert {(t.path, t.name) for t in rep.tops} == {(str(home), "no"), (str(home), "me")}
    calls = _calls(log)
    assert len(calls) == 1 and f"[#{b.pw_uid}]" in calls[0]
    r = _run(tmp_path, "-d", str(tmp_path / "out"), "-O", "-U", "x", "scan", str(home), check=False)
    assert r.returncode == 2 and "-U: an account number" in r.stderr


@pytest.mark.skipif(not ROOT or not shutil.which("setpriv"), reason="runs the script as another account")
def test_owner_mode_never_asks_sudo_to_act_as_root(tmp_path):
    """Run as an ordinary account (nobody here): root's top folder is not
    measured and sudo isn't asked; the account's own folder is read as it is."""
    import tempfile
    nobody = _account("nobody")
    base = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        os.chmod(base, 0o755)
        home = base / "home"
        for top in ("r", "n"):
            (home / top / "d").mkdir(parents=True)
            (home / top / "d" / "f").write_bytes(os.urandom(3000))
        for p in [home / "n", *(home / "n").rglob("*")]:
            os.lchown(p, nobody.pw_uid, nobody.pw_gid)
        work = base / "work"
        work.mkdir()
        os.chown(work, nobody.pw_uid, nobody.pw_gid)
        script = _script(base)
        env, log = _sudo(base)
        os.chmod(base / "sudo-shim", 0o755)
        os.chown(log, nobody.pw_uid, nobody.pw_gid)
        e = dict(os.environ)
        e.update(env)
        r = subprocess.run(["setpriv", f"--reuid={nobody.pw_uid}", f"--regid={nobody.pw_gid}", "--clear-groups",
                            SH, str(script), "-d", str(work / "out"), "-O", "scan", str(home)],
                           capture_output=True, text=True, env=e, timeout=120, cwd=str(work))
        assert r.returncode == 0, r.stderr
        rep = storage.parse((work / "out" / "scan.txt").read_text())
        assert rep.not_measured == [(str(home), "r", 0, "root")] and not _calls(log)
        assert [t.name for t in rep.tops] == ["n"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_owner_mode_measures_this_accounts_own_folders_without_sudo(tmp_path):
    home = tmp_path / "home"
    for n in ("a", "b"):
        (home / n / "d").mkdir(parents=True)
        (home / n / "d" / "f").write_bytes(os.urandom(2000))
    env, log = _sudo(tmp_path)
    out = tmp_path / "out"
    rep = _scan(tmp_path, out, "-O", "scan", str(home), env=env)
    assert {t.name for t in rep.tops} == {"a", "b"} and not _calls(log) and not rep.not_measured
    state = [line.split("\t") for line in (out / "scan.state").read_text().splitlines()]
    assert {Path(s[2]).name: (s[1], len(s), s[5]) for s in state} == {"a": ("done", 6, str(os.getuid())),
                                                                      "b": ("done", 6, str(os.getuid()))}
    # With no time left, the due ones wait for the next run. (A change is
    # seen by the folder's mtime, in seconds.)
    time.sleep(1.1)
    (home / "a" / "new").write_text("x")
    rep = _scan(tmp_path, out, "-O", "-s", "0", "scan", str(home), env=env)
    assert not rep.tops and rep.not_scanned == 1 and rep.carried == 1


@pytest.mark.skipif(not ROOT, reason="gives a folder to a number no account has (root only)")
def test_an_owner_without_a_name_is_not_measured(tmp_path):
    home = tmp_path / "home"
    (home / "gone" / "d").mkdir(parents=True)
    os.chown(home / "gone", 4242999, 4242999)
    env, log = _sudo(tmp_path)
    rep = _scan(tmp_path, tmp_path / "out", "-O", "scan", str(home), env=env)
    assert rep.not_measured == [(str(home), "gone", 4242999, "name")] and not _calls(log)


def test_owner_mode_options_are_checked(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    out = str(tmp_path / "out")
    for args, said in ((["-O", "-S", "scan"], "-O and -S"), (["-O", "usage"], "for the scan only"),
                       (["-O", "-L", "x", "scan"], "-L: a number of GB")):
        r = _run(tmp_path, "-d", out, *args, str(home), check=False)
        assert r.returncode == 2 and said in r.stderr, (args, r.stderr)
    r = _run(tmp_path, "-d", out, "-O", "scan", str(home), check=False, bsd=True)
    assert r.returncode == 2 and "needs GNU find" in r.stderr


def test_storage_cron_prints_owner_mode(tmp_path):
    r = CliRunner().invoke(cli_main(), ["storage", "cron", "--run-as", "owners", "--user", "svc",
                                        "--dir", "/home/svc/ns", "--large", "20", "/home", "/proj"])
    assert r.exit_code == 0, r.output
    assert "NOPASSWD: /usr/bin/find" not in r.output                # never find itself
    assert "-O -L 20 -s 10800 scan /home /proj" in r.output and "--run-as sudo --user svc" in r.output
    usage_line = next(line for line in r.output.splitlines() if line.startswith("17 "))
    assert "-O" not in usage_line
    r = CliRunner().invoke(cli_main(), ["storage", "cron", "--run-as", "owners", "--user", "svc", "/home"])
    assert r.exit_code != 0 and "give --dir" in r.output
    r = CliRunner().invoke(cli_main(), ["storage", "cron", "--run-as", "owners", "--user", "svc",
                                        "--dir", "/home/svc/ns", "/home"])
    assert "-O -s 10800 scan /home" in r.output and "below 1000 (--min-uid)" in r.output
    r = CliRunner().invoke(cli_main(), ["storage", "cron", "--run-as", "owners", "--user", "svc",
                                        "--dir", "/home/svc/ns", "--min-uid", "500", "/home"])
    assert "-O -U 500 -s 10800 scan /home" in r.output


def test_the_script_parses_owner_mode_lines():
    rep = storage.parse("#nomad-storage\t1\tscan\n#started\t1\n#scan-as\towner\n#present\t/h\nP\ta\nP\tb\n"
                        "#odd-names\t0\n#nested\t/h\tc\t/h/c\n#carried\t4\n#not-measured\t/h\tb\t1234\tsudo\n"
                        "#not-scanned\t0\n#finished\t2\n")
    assert rep.present == {"/h": ["a", "b"]} and rep.nested == [("/h", "c", "/h/c")]
    assert rep.scan_as == "owner" and rep.carried == 4 and rep.not_measured == [("/h", "b", 1234, "sudo")]


# --- B: the per-user report's host roles ----------------------------------------------------

def test_the_per_user_report_says_each_hosts_role(db_path):
    from datetime import timezone

    from nomad.collectors.per_user.report import report
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db_path) as c:
        for i, (host, role) in enumerate((("login1", "headnode"), ("lab-r", "interactive"), ("n01", "compute"))):
            c.execute("INSERT INTO per_user_alert (fired_at, hostname, role, username, uid, pid, process_session_id, "
                      "rule_id, rule_type, severity, threshold_value, dedup_key, last_seen) "
                      "VALUES (?, ?, ?, 'user1', 10001, ?, ?, 'cpu', 'cpu', 'actionable', 10, ?, ?)",
                      (now, host, role, 100 + i, f"s{i}", f"k{i}", now))
        c.execute("INSERT INTO per_user_sample (timestamp, hostname, role, username, uid, pid, process_session_id) "
                  "VALUES (?, 'lab-r', 'interactive', 'user1', 10001, 1, 's9')", (now,))
        lines = report(c, days=7)
    assert "login1 (login node)" in lines and "lab-r (interactive server)" in lines and "n01 (compute node)" in lines


# --- C: nomad sync status and the actions ----------------------------------------------------

def _combined(tmp_path):
    from nomad.db import delta_hub
    db = tmp_path / "combined.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sync_sites (name TEXT PRIMARY KEY, partitions TEXT, filesystems TEXT, "
                 "cluster_type TEXT, synced_at TEXT, merged INTEGER, records INTEGER, error TEXT, source TEXT, "
                 "last_success TEXT)")
    now = datetime.now().replace(microsecond=0)
    old = (now - timedelta(hours=5)).isoformat()
    conn.execute("INSERT INTO sync_sites VALUES ('c1', NULL, NULL, NULL, ?, 1, 1234, NULL, 'delta', ?)",
                 (now.isoformat(), now.isoformat()))
    conn.execute("INSERT INTO sync_sites VALUES ('c2', NULL, NULL, NULL, ?, 0, 0, 'ssh: timed out', 'kept', ?)",
                 (old, old))
    conn.execute("INSERT INTO sync_sites VALUES ('gone', NULL, NULL, NULL, ?, 1, 5, NULL, 'copy', ?)",
                 (now.isoformat(), now.isoformat()))
    delta_hub.ensure_bookkeeping(conn)
    delta_hub.set_state(conn, "c1", "e1", 10, "delta")
    delta_hub.set_state(conn, "c2", "e2", 3, "delta")
    delta_hub.note_failure(conn, "c2")
    delta_hub.note_failure(conn, "c2")
    delta_hub.set_meta(conn, "synced_at", now.isoformat())
    conn.commit()
    conn.close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[hub]\noutput_db = "{db}"\n\n' + "".join(
        f'[[hub.sites]]\nname = "{n}"\nhost = "{n}-head"\n\n' for n in ("c1", "c2", "c3")))
    return db, cfg


def test_sync_status_says_how_each_site_went(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("nomad.config.DEFAULT_CONFIG_PATHS", [tmp_path / "none.toml"])
    db, cfg = _combined(tmp_path)
    before = db.read_bytes()
    r = CliRunner().invoke(cli_main(), ["-c", str(cfg), "sync", "status"])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    c1 = next(i for i, line in enumerate(lines) if line.startswith("  c1:"))
    assert "only what changed, 1,234 rows" in lines[c1] and "late" not in lines[c1]
    assert "next sync asks for only what changed" in lines[c1 + 1]
    c2 = next(line for line in lines if line.startswith("  c2:"))
    assert "kept what the hub had" in c2 and "late (over 120 minutes)" in c2
    assert "2 failed in a row" in r.output and "last error: ssh: timed out" in r.output
    assert "  c3: never synced" in lines
    assert any(line.startswith("  gone:") and "no longer in [hub] sites" in line for line in lines)
    assert "3 site(s) in [hub], 2 late or never synced" in r.output
    assert db.read_bytes() == before                             # read only
    r = CliRunner().invoke(cli_main(), ["sync", "status", "--db", str(db)])
    assert r.exit_code == 0 and "  c1:" in r.output
    r = CliRunner().invoke(cli_main(), ["sync", "status", "--db", str(tmp_path / "none.db")])
    assert r.exit_code != 0 and "no combined database" in r.output


def test_sync_still_syncs_when_no_subcommand_is_given(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("nomad.config.DEFAULT_CONFIG_PATHS", [tmp_path / "none.toml"])
    r = CliRunner().invoke(cli_main(), ["sync", "--dry-run"])
    assert r.exit_code == 0 and "No hub configuration found" in r.output
    r = CliRunner().invoke(cli_main(), ["sync", "--help"])
    assert "status" in r.output and "--dry-run" in r.output


def test_storage_use_takes_one_path(tmp_path):
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": []}, db)
    c.store([{"type": "storage_report", "server": "fs1", "read_at": "2026-10-23T01:00:00",
              "report": _usage([("/home", [("ann", 10**12)]), ("/scratch", [("ben", 10**12)])])}])
    out = CliRunner().invoke(cli_main(), ["storage", "use", "--db", str(db), "--path", "/scratch/"]).output
    assert "fs1:/scratch" in out and "fs1:/home" not in out
