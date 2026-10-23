"""nomad 1.17.1, from the independent review: forged lines in owner mode,
the find wrapper, paths given twice or nested, a path that lists nothing,
the time running out mid-owner, folders their owner can't read, ZFS datasets
counted once, naming files written elsewhere, `sync -c FILE status`, copies
that name no path, and a top folder that changed hands."""
import os
import pwd
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad import storage
from nomad.collectors import storage_use as su
from nomad.storage import views

from tests.test_release_1171 import (ROOT, SH, _account, _calls, _combined, _owned_tree, _run, _scan, _script, _sudo,
                               cli_main)

NL = "\n"


def _blocks(p: Path) -> int:
    return os.stat(p).st_blocks * 512


# --- 1: no line can be forged ------------------------------------------------------------

def test_owner_mode_folder_names_cant_forge_lines(tmp_path):
    """A folder name with a line break followed by what looks like one of
    find's lines: no D line is printed for it, and the numbers are checked,
    so the owner's figures are what is on disk."""
    home = tmp_path / "home"
    top = home / "me"
    (top / "bigdata").mkdir(parents=True)
    big = top / "bigdata" / "real.bin"
    big.write_bytes(os.urandom(300_000))
    for fake in (f"z{NL}L -99999999 0 1 1 {top}/bigdata/q",              # would hide it
                 f"y{NL}L 99999999999 0 1 1 {top}/bigdata/q",              # would add 51 TB
                 f"x{NL}D 1 2002 {top}/proj",                               # would give it to another
                 f"w{NL}D 1 99999999999999999999 {top}/huge"):              # would stop the store
        (top / fake / "q").mkdir(parents=True)
    env, log = _sudo(tmp_path)
    out = tmp_path / "out"
    rep = _scan(tmp_path, out, "-O", "scan", str(home), env=env)
    (t,) = rep.tops
    folders = {f.name: f for f in t.folders}
    assert folders["bigdata"].bytes == _blocks(big) and folders["bigdata"].files == 1
    assert "proj" not in folders and "huge" not in folders
    assert all(f.owner_uid in (None, os.getuid()) for f in t.folders)
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": [{"server": "fs1", "dir": str(out)}], "every_minutes": 0.001}, db)
    c.store(c.collect())
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM storage_tops").fetchone()[0] == 1


def test_a_made_up_account_number_cant_stop_a_server_being_stored(tmp_path):
    text = ("#nomad-storage\t1\tscan\n#started\t1700000000\n#ages\t180\t365\n#present\t/h\nP\ta\n"
            "#top\t/h\ta\t99999999999999999999\t1700000000\n"
            "F\tx\t99999999999999999999\t4096\t1\t1700000000\t0\t0\n#rc\t0\n#denied\t0\n#endtop\t1\n"
            "#not-measured\t/h\tb\t99999999999999999999\tsudo\n#not-scanned\t0\n#finished\t1700000001\n")
    rep = storage.parse(text)
    assert rep.tops[0].owner_uid is None and rep.tops[0].folders[0].owner_uid is None
    assert rep.not_measured == [("/h", "b", None, "sudo")]
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": []}, db)
    c.store([{"type": "storage_report", "server": "fs1", "read_at": "2026-10-23T01:00:00", "report": rep}])
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM storage_folders").fetchone()[0] == 1


# --- 2: the find wrapper -----------------------------------------------------------------

@pytest.fixture
def root_only():
    """A folder only root may change, from / down (as -F wants the wrapper's)."""
    if not ROOT or os.stat("/root").st_uid != 0 or os.stat("/root").st_mode & 0o022:
        pytest.skip("needs root and a root-only /root")
    d = Path(tempfile.mkdtemp(dir="/root"))
    d.chmod(0o755)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _wrapper(where: Path, text: str | None = None) -> Path:
    w = where / "nomad-storage-find"
    w.write_text(text if text is not None else storage.script_text(storage.FIND_WRAPPER))
    w.chmod(0o755)
    return w


@pytest.mark.skipif(not ROOT, reason="the wrapper must be root's")
def test_owner_mode_through_the_wrapper_gives_the_same(tmp_path, root_only):
    home, proj, a, b = _owned_tree(tmp_path)
    env, log = _sudo(tmp_path, refuse=f"#{b.pw_uid}")
    w = _wrapper(root_only)
    plain = _scan(tmp_path, tmp_path / "o1", "-O", "-U", "1", "scan", str(home), str(proj), env=env)
    log.write_text("")
    wrapped = _scan(tmp_path, tmp_path / "o2", "-O", "-U", "1", "-F", str(w), "scan", str(home), str(proj), env=env)

    def figures(rep):
        return {(t.path, t.name): sorted((f.name, f.owner_uid, f.bytes, f.files) for f in t.folders)
                for t in rep.tops}
    assert figures(wrapped) == figures(plain) and wrapped.not_measured == plain.not_measured
    # sudo is asked for the wrapper and the paths, nothing else.
    calls = _calls(log)
    assert any(c == f"sudo [-n] [-u] [#{a.pw_uid}] [--] [{w}] [{home / 'da'}] [{proj / 'dp'}]" for c in calls)
    # It takes absolute paths only, and the script refuses one others may change.
    r = subprocess.run([SH, str(w), "-exec"], capture_output=True, text=True)
    assert r.returncode == 2 and "absolute folder paths only" in r.stderr
    r = _run(tmp_path, "-d", str(tmp_path / "o3"), "-F", str(w), "scan", str(home), check=False)
    assert r.returncode == 2 and "with -O only" in r.stderr


@pytest.mark.skipif(not ROOT, reason="the wrapper must be root's")
def test_the_wrapper_must_be_roots_all_the_way_down(tmp_path, root_only):
    """It, every folder above it and what a link points to: root's, and only
    root may change them; and it must be nomad-storage-find."""
    home = tmp_path / "home"
    (home / "a").mkdir(parents=True)

    def refused(w, said):
        r = _run(tmp_path, "-d", str(tmp_path / "o"), "-O", "-F", str(w), "scan", str(home), check=False)
        assert r.returncode == 2 and said in r.stderr, (w, r.stderr)
    w = _wrapper(root_only)
    w.chmod(0o775)
    refused(w, "writable by root only")
    w.chmod(0o755)
    # In a folder others may change (/tmp, here): it could be swapped.
    (tmp_path / "wrap").mkdir()
    refused(_wrapper(tmp_path / "wrap"), "writable by root only")
    # A link, even root's to a root-only wrapper: sudoers would name the
    # link, and whoever may change a link may swap what sudo runs.
    link = root_only / "link"
    link.symlink_to(tmp_path / "wrap" / "nomad-storage-find")
    refused(link, "give the wrapper's real path")
    (root_only / "real").mkdir()
    good = _wrapper(root_only / "real")
    link2 = root_only / "link2"
    link2.symlink_to(good)
    refused(link2, f"give the wrapper's real path, {good}")
    # Not the wrapper at all.
    refused(shutil.which("find"), "isn't nomad-storage-find")
    other = root_only / "other"
    other.mkdir()
    refused(_wrapper(other, "#!/bin/sh\nexec find \"$@\"\n"), "isn't nomad-storage-find")


def test_the_cli_prints_the_wrapper_and_never_a_rule_for_find(tmp_path):
    r = CliRunner().invoke(cli_main(), ["storage", "script", "--find"])
    assert r.exit_code == 0 and r.output == storage.script_text(storage.FIND_WRAPPER)
    r = CliRunner().invoke(cli_main(), ["storage", "cron", "--run-as", "owners", "--user", "svc",
                                        "--dir", "/home/svc/ns", "/home"])
    assert r.exit_code == 0 and "NOPASSWD: /usr/bin/find" not in r.output
    assert "don't allow find itself" in r.output and "--find-wrapper" in r.output
    r = CliRunner().invoke(cli_main(), ["storage", "cron", "--run-as", "owners", "--user", "svc",
                                        "--dir", "/home/svc/ns", "--find-wrapper",
                                        "/usr/local/sbin/nomad-storage-find", "/home"])
    assert "svc ALL=(ALL,!root) NOPASSWD: /usr/local/sbin/nomad-storage-find" in r.output
    assert "-O -F /usr/local/sbin/nomad-storage-find -s 10800 scan /home" in r.output
    r = CliRunner().invoke(cli_main(), ["storage", "cron", "--run-as", "root", "--find-wrapper", "/x", "/home"])
    assert r.exit_code != 0 and "for --run-as owners" in r.output


def test_the_wrapper_runs_the_scripts_own_find():
    """The two must stay one expression."""
    wrapper = storage.script_text(storage.FIND_WRAPPER)
    script = storage.script_text()
    for piece in ("""\\( -type d \\( -path "*$NL*" -o -printf 'D %d %U %p\\n' \\) \\( -readable -executable -o -printf 'X\\n' \\) \\) -o \\""",
                  """\\( -type f \\( -path "*$NL*" -printf 'N %b %T@ %n %i %H\\n' -o -printf 'L %b %T@ %n %i %p\\n' \\) \\)"""):
        assert piece in wrapper and piece in script, piece
    assert 'exec find "$@" -xdev' in wrapper


# --- 3: a path twice, a path within a top folder -----------------------------------------

@pytest.mark.parametrize("owner_mode", [False, True])
def test_a_path_given_twice_is_scanned_once(tmp_path, owner_mode):
    home = tmp_path / "home"
    (home / "a" / "d").mkdir(parents=True)
    f = home / "a" / "d" / "x.bin"
    f.write_bytes(os.urandom(50_000))
    env, log = _sudo(tmp_path)
    args = ["-O"] if owner_mode else []
    out = tmp_path / "out"
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": [{"server": "fs1", "dir": str(out)}], "every_minutes": 0.001}, db)
    rep = _scan(tmp_path, out, *args, "scan", str(home), env=env)
    c.store(c.collect())
    time.sleep(1.1)
    # The same name twice (/home and /home/): once, and nothing to say.
    rep = _scan(tmp_path, out, *args, "scan", str(home), f"{home}/", str(home), env=env)
    assert rep.nested == [] and not rep.not_measured
    if owner_mode:
        assert not rep.tops and rep.carried == 1                 # measured last night
    else:
        assert [(t.path, t.name) for t in rep.tops] == [(str(home), "a")]
        (d,) = [x for x in rep.tops[0].folders if x.name == "d"]
        assert d.bytes == _blocks(f) and d.files == 1
    c.store(c.collect())
    # Its figures stay (the carried folder's too).
    conn = sqlite3.connect(db)
    assert [(p, t) for p, t in conn.execute("SELECT DISTINCT path, top FROM storage_folders")] == [(str(home), "a")]
    assert views.scan_owners(conn, "fs1", str(home))
    # Under another name (a link to it): said, and that name's old figures go.
    (tmp_path / "homes").symlink_to(home)
    time.sleep(1.1)
    rep = _scan(tmp_path, out, *args, "scan", str(home), str(tmp_path / "homes"), env=env)
    assert rep.nested == [(str(tmp_path / "homes"), "", str(home))]
    c.store(c.collect())
    text = CliRunner().invoke(cli_main(), ["storage", "status", "--db", str(db)]).output
    assert f"left out {tmp_path / 'homes'}: the same as {home}, scanned once" in text
    assert [(p, t) for p, t in conn.execute("SELECT DISTINCT path, top FROM storage_folders")] == [(str(home), "a")]


@pytest.mark.parametrize("owner_mode", [False, True])
def test_a_top_folder_holding_another_path_is_said(tmp_path, owner_mode):
    home = tmp_path / "home"
    (home / "alice" / "proj" / "sub").mkdir(parents=True)
    (home / "alice" / "proj" / "sub" / "p.bin").write_bytes(os.urandom(50_000))
    (home / "alice" / "notes").mkdir()
    (home / "alice" / "notes" / "n.bin").write_bytes(os.urandom(20_000))
    env, log = _sudo(tmp_path)
    args = ["-O"] if owner_mode else []
    out = tmp_path / "out"
    rep = _scan(tmp_path, out, *args, "scan", str(home), str(home / "alice" / "proj"), env=env)
    assert rep.contains == [(str(home), "alice", str(home / "alice" / "proj"))]
    tops = {(t.path, t.name): {f.name: f.files for f in t.folders} for t in rep.tops}
    # Each top folder counted once by its own find: nothing credited twice to one.
    assert tops[(str(home), "alice")] == {"proj": 1, "notes": 1}
    assert tops[(str(home / "alice" / "proj"), "sub")] == {".": 1}
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": [{"server": "fs1", "dir": str(out)}], "every_minutes": 0.001}, db)
    c.store(c.collect())
    text = CliRunner().invoke(cli_main(), ["storage", "status", "--db", str(db)]).output
    assert f"{home}/alice holds {home / 'alice' / 'proj'}, also scanned: what is there is counted in both" in text


# --- 5: a night a path lists nothing --------------------------------------------------------

def test_a_path_that_lists_nothing_keeps_its_folders_schedule(tmp_path):
    home = tmp_path / "home"
    for n in ("a", "b"):
        (home / n / "d").mkdir(parents=True)
        (home / n / "d" / "f").write_bytes(os.urandom(2000))
    env, log = _sudo(tmp_path)
    out = tmp_path / "out"
    rep = _scan(tmp_path, out, "-O", "scan", str(home), env=env)
    assert len(rep.tops) == 2
    state = out / "scan.state"
    before = sorted(state.read_text().splitlines())
    # Not mounted one night (an empty mount point).
    home.rename(tmp_path / "home.away")
    home.mkdir()
    rep = _scan(tmp_path, out, "-O", "scan", str(home), env=env)
    assert not rep.tops and rep.present == {str(home): []}
    assert sorted(state.read_text().splitlines()) == before
    home.rmdir()
    (tmp_path / "home.away").rename(home)
    rep = _scan(tmp_path, out, "-O", "scan", str(home), env=env)
    assert not rep.tops and rep.carried == 2                       # nothing due again


# --- 7: the time running out in an owner's find ---------------------------------------------

@pytest.mark.skipif(not ROOT, reason="the wrapper must be root's")
def test_when_time_runs_out_the_folders_finished_count(tmp_path, root_only):
    """One owner, three top folders; the time runs out in the second: the
    first is measured whole, the second short, the third waits."""
    home = tmp_path / "home"
    tops = [home / n for n in ("t1", "t2", "t3")]
    for t in tops:
        (t / "d").mkdir(parents=True)
    # A stand-in for the wrapper that stops in the second folder.
    w = root_only / "slow-find"
    w.write_text("#!/bin/sh\nNOMAD_STORAGE_FIND=1\nuid=$(id -u)\n"
                 f"printf 'D 0 %s %s\\nD 1 %s %s/d\\nL 8 1700000000 1 11 %s/d/f\\n' \"$uid\" {tops[0]} \"$uid\" "
                 f"{tops[0]} {tops[0]}\n"
                 f"printf 'D 0 %s %s\\nL 8 1700000000 1 12 %s/d/f\\n' \"$uid\" {tops[1]} {tops[1]}\n"
                 "exec sleep 30\n")
    w.chmod(0o755)
    script = _script(tmp_path)
    script.write_text(script.read_text().replace('timeout -k 10 "$remaining"', 'timeout -k 1 2'))
    out = tmp_path / "out"
    r = subprocess.run([SH, str(script), "-d", str(out), "-O", "-F", str(w), "scan", str(home)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    rep = storage.parse((out / "scan.txt").read_text())
    got = {t.name: t.complete for t in rep.tops}
    assert got == {"t1": True, "t2": False} and rep.not_scanned == 1 and not rep.not_measured
    state = {Path(x.split("\t")[2]).name: x.split("\t")[1] for x in (out / "scan.state").read_text().splitlines()}
    assert state == {"t1": "done", "t2": "partial"}


# --- 8: a folder its owner made unreadable ----------------------------------------------------

@pytest.mark.skipif(not ROOT or not shutil.which("setpriv"), reason="runs the script as another account")
def test_a_folder_its_owner_cant_read_all_of_is_measured_short(tmp_path):
    nobody = _account("nobody")
    base = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        os.chmod(base, 0o755)
        home = base / "home"
        (home / "n" / "bigdata").mkdir(parents=True)
        (home / "n" / "bigdata" / "f").write_bytes(os.urandom(9000))
        (home / "n" / "open").mkdir()
        (home / "n" / "open" / "g").write_bytes(os.urandom(3000))
        for p in [home / "n", *(home / "n").rglob("*")]:
            os.lchown(p, nobody.pw_uid, nobody.pw_gid)
        (home / "n" / "bigdata").chmod(0)
        work = base / "work"
        work.mkdir()
        os.chown(work, nobody.pw_uid, nobody.pw_gid)
        script = _script(base)
        r = subprocess.run(["setpriv", f"--reuid={nobody.pw_uid}", f"--regid={nobody.pw_gid}", "--clear-groups",
                            SH, str(script), "-d", str(work / "out"), "-O", "scan", str(home)],
                           capture_output=True, text=True, timeout=120, cwd=str(work))
        assert r.returncode == 0, r.stderr
        rep = storage.parse((work / "out" / "scan.txt").read_text())
        (t,) = rep.tops
        assert t.denied >= 1 and not t.complete
        state = (work / "out" / "scan.state").read_text().split("\t")
        assert state[1] == "denied"
        db = base / "site.db"
        c = su.StorageUseCollector({"sources": [{"server": "fs1", "dir": str(work / "out")}],
                                    "every_minutes": 0.001}, db)
        c.store(c.collect())
        text = CliRunner().invoke(cli_main(), ["storage", "status", "--db", str(db)]).output
        assert "1 top folders measured short (time ran out, or their owner can't read all of them)" in text
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- 11: a top folder given to someone else ------------------------------------------------

@pytest.mark.skipif(not ROOT, reason="gives a folder to another account (root only)")
def test_a_top_folder_that_changes_hands_is_measured_again(tmp_path):
    home = tmp_path / "home"
    (home / "x" / "d").mkdir(parents=True)
    a = _account("nobody")
    env, log = _sudo(tmp_path)
    out = tmp_path / "out"
    rep = _scan(tmp_path, out, "-O", "scan", str(home), env=env)
    assert [t.owner_uid for t in rep.tops] == [os.getuid()]
    time.sleep(1.1)
    os.chown(home / "x", a.pw_uid, a.pw_gid)                  # no entry made: only its ctime says so
    rep = _scan(tmp_path, out, "-O", "scan", str(home), env=env)
    assert [t.owner_uid for t in rep.tops] == [a.pw_uid] and rep.carried == 0
    assert len(_calls(log)) == 1


# --- 4: ZFS datasets counted once --------------------------------------------------------------

def test_a_persons_total_counts_each_dataset_once(tmp_path):
    conn = sqlite3.connect(tmp_path / "site.db")
    conn.executescript(su.SCHEMA)
    when = "2026-10-23T01:00:00"
    for path, ds, used in (("/mnt/pool", "pool", 4000), ("/mnt/pool", "pool/home", 6000),
                           ("/mnt/pool/home", "pool/home", 6000)):
        conn.execute("INSERT INTO storage_owners (timestamp, server, path, dataset, kind, name, used_bytes) "
                     "VALUES (?, 'nas', ?, ?, 'user', 'alice', ?)", (when, path, ds, used))
    for path in ("/mnt/pool", "/mnt/pool/home"):
        conn.execute("INSERT INTO storage_paths (timestamp, server, path, fs, dataset, total_bytes, used_bytes) "
                     "VALUES (?, 'nas', ?, 'zfs', ?, 100000, 50000)", (when, path, path.replace("/mnt/", "")))
    conn.commit()
    rows = views.person(conn, "alice")
    assert {r["path"]: r["used_bytes"] for r in rows} == {"/mnt/pool": 10000, "/mnt/pool/home": 6000}
    assert views.totals(rows) == {"total_bytes": 10000, "copies_bytes": 0}


# --- 6: files written elsewhere, read here ------------------------------------------------------

def test_files_another_machine_wrote_are_named_from_the_floor(tmp_path, monkeypatch):
    from tests.test_release_1171 import SCAN, _names
    _names(monkeypatch, {2: "sys", 5001: "ann", 5002: "ben"})
    monkeypatch.setattr(su.socket, "gethostname", lambda: "here.example.org")
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": []}, db)
    for host, expect in ((None, {"sys": "sys", "ann": "ann"}),            # this machine's own
                         ("here", {"sys": "sys", "ann": "ann"}),
                         ("fileserver", {"sys": None, "ann": "ann"})):   # written there, read here
        text = SCAN.replace("#started\t1700000000\n", "#started\t1700000000\n"
                            + (f"#host\t{host}\n" if host else ""))
        c.store([{"type": "storage_report", "server": f"fs-{host}", "read_at": "2026-10-23T01:00:00",
                  "report": storage.parse(text), "names": "local", "account_floor": 1000}])
        conn = sqlite3.connect(db)
        got = dict(conn.execute("SELECT top, top_owner FROM storage_tops WHERE server = ?", (f"fs-{host}",)))
        assert got == expect, host


def test_status_says_when_numbers_stay_numbers(tmp_path):
    from tests.test_release_1171 import SCAN
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": []}, db)
    c.store([{"type": "storage_report", "server": "nas", "read_at": "2026-10-23T01:00:00",
              "report": storage.parse(SCAN), "names": "none", "copies": {"/mnt/elsewhere": "fs0"}}])
    text = CliRunner().invoke(cli_main(), ["storage", "status", "--db", str(db)]).output
    assert "set same_accounts = true for it" in text
    assert "copies: /mnt/elsewhere isn't one of its paths (/home)" in text


# --- 10: sync -c FILE status ---------------------------------------------------------------------

def test_sync_status_reads_the_hub_sync_is_given(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("nomad.config.DEFAULT_CONFIG_PATHS", [tmp_path / "none.toml"])
    db, cfg = _combined(tmp_path)
    r = CliRunner().invoke(cli_main(), ["sync", "-c", str(cfg), "status"])
    assert r.exit_code == 0, r.output
    assert "  c1:" in r.output and "3 site(s) in [hub]" in r.output
    other = tmp_path / "other.db"
    shutil.copy(db, other)
    r = CliRunner().invoke(cli_main(), ["sync", "-c", str(cfg), "-o", str(other), "status"])
    assert r.exit_code == 0 and str(other) in r.output
    r = CliRunner().invoke(cli_main(), ["sync", "--dry-run", "status"])
    assert r.exit_code == 2 and "are for a sync" in r.output


# --- second review ---------------------------------------------------------------------------

def _scan_text(top_bytes: int, when: int, rc: int = 0, denied: int = 0, owner: bool = False) -> str:
    return (f"#nomad-storage\t1\tscan\n#started\t{when}\n#ages\t180\t365\n"
            + ("#scan-as\towner\n" if owner else "")
            + "#present\t/home\nP\talice\n"
            f"#top\t/home\talice\t5001\t{when}\nF\tdata\t5001\t{top_bytes}\t1\t{when}\t0\t0\n"
            f"#rc\t{rc}\n#denied\t{denied}\n#endtop\t1\n#not-scanned\t0\n#finished\t{when + 1}\n")


def test_the_standing_scan_is_decided_once_for_tops_and_folders(tmp_path):
    """Whole 100, short 150, short 80: 150 stands, in the top folder's figure
    and in its folders alike (a short one is at least what is there)."""
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": []}, db)
    for n, size, kw in ((1, 100_000, {}), (2, 150_000, {"denied": 1, "owner": True}),
                        (3, 80_000, {"denied": 1, "owner": True})):
        c.store([{"type": "storage_report", "server": "fs1", "read_at": f"2026-10-2{n}T02:00:00",
                  "report": storage.parse(_scan_text(size, 1792000000 + n * 86400, **kw))}])
    conn = sqlite3.connect(db)
    (row,) = views._latest_tops(conn, "fs1", "/home")
    assert (row[2], bool(row[10]), row[13]) == (150_000, False, "unreadable")
    assert conn.execute("SELECT SUM(bytes) FROM storage_folders").fetchone()[0] == 150_000
    assert [b for (b,) in conn.execute("SELECT bytes FROM storage_tops WHERE stands = 1")] == [150_000]
    # Thinning (one a week, the week's whole one) keeps it all the same.
    from datetime import datetime
    when = [w for (w,) in conn.execute("SELECT scanned_at FROM storage_tops")]
    assert all(w.startswith("2026-10-1") for w in when), when
    su.StorageUseCollector._thin_tops(conn, datetime(2026, 11, 10))
    assert sorted(b for (b,) in conn.execute("SELECT bytes FROM storage_tops")) == [100_000, 150_000]
    (row,) = views._latest_tops(conn, "fs1", "/home")
    assert row[2] == 150_000


def test_a_short_reading_stands_for_a_new_owner_or_after_four_weeks(tmp_path):
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": []}, db)
    day = 1792000000

    def store(n, size, uid=5001, **kw):
        text = _scan_text(size, day + n * 86400, **kw).replace("\talice\t5001\t", f"\talice\t{uid}\t")
        c.store([{"type": "storage_report", "server": "fs1", "read_at": f"2026-10-23T0{n % 10}:00:{n:02d}",
                  "report": storage.parse(text)}])
        conn = sqlite3.connect(db)
        (row,) = views._latest_tops(conn, "fs1", "/home")
        assert conn.execute("SELECT SUM(bytes) FROM storage_folders").fetchone()[0] == row[2]
        return row[2], row[12]
    assert store(0, 3_000_000) == (3_000_000, 5001)
    # Handed to another account, who can't read all of it: theirs now.
    assert store(1, 200_000, uid=5002, denied=1, owner=True) == (200_000, 5002)
    assert store(2, 3_000_000, uid=5002) == (3_000_000, 5002)
    # The same owner, short and smaller: the whole one stands, for 4 weeks.
    assert store(9, 1_000_000, uid=5002, denied=1, owner=True) == (3_000_000, 5002)
    assert store(29, 1_000_000, uid=5002, denied=1, owner=True) == (3_000_000, 5002)
    assert store(31, 1_000_000, uid=5002, denied=1, owner=True) == (1_000_000, 5002)   # (a DST hour)


def test_a_folder_stopped_at_the_budget_never_drops_its_whole_figure(tmp_path):
    """Stopped by the time, a scan is a partial walk: no age makes it stand
    over a larger whole one; and an owner no one knows is no new owner."""
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": []}, db)
    day = 1792000000
    for n, size, rc, uid in ((0, 1000, 0, "5001"), *((k, 30, 124, "5001") for k in (7, 28, 40, 60)),
                             (61, 20, 124, "-")):
        text = _scan_text(size, day + n * 86400, rc=rc).replace("\talice\t5001\t", f"\talice\t{uid}\t")
        c.store([{"type": "storage_report", "server": "fs1", "read_at": f"2026-10-23T00:00:{n:02d}",
                  "report": storage.parse(text)}])
        conn = sqlite3.connect(db)
        (row,) = views._latest_tops(conn, "fs1", "/home")
        assert (row[2], conn.execute("SELECT SUM(bytes) FROM storage_folders").fetchone()[0]) == (1000, 1000), n


def test_a_short_reading_stands_when_it_is_the_larger(tmp_path):
    """A short figure is at least what is there: it replaces an earlier whole
    one that is smaller (the folder grew), never one that is larger (it may
    have missed what it holds), and either way says it is short."""
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": []}, db)

    def night(n, size, **kw):
        c.store([{"type": "storage_report", "server": "fs1", "read_at": f"2026-10-2{n}T02:00:00",
                  "report": storage.parse(_scan_text(size, 1792000000 + n * 86400, **kw))}])
        conn = sqlite3.connect(db)
        (row,) = views._latest_tops(conn, "fs1", "/home")
        folders = conn.execute("SELECT SUM(bytes), MIN(complete) FROM storage_folders").fetchone()
        return row[2], bool(row[10]), row[13], folders
    assert night(1, 3_000_000) == (3_000_000, True, None, (3_000_000, 1))
    # Grew to 8 MB, but a folder inside became unreadable to its owner.
    assert night(2, 8_000_000, denied=1, owner=True) == (8_000_000, False, "unreadable", (8_000_000, 0))
    # Made unreadable to hide it: the earlier whole figure stands.
    c2 = night(3, 8_000_000)
    assert c2[:3] == (8_000_000, True, None)
    assert night(4, 1_000, denied=1, owner=True) == (8_000_000, True, None, (8_000_000, 1))
    out = CliRunner().invoke(cli_main(), ["storage", "use", "--db", str(db)]).output
    assert "short of what is there" not in out
    night(5, 9_000_000, denied=2, owner=True)
    out = CliRunner().invoke(cli_main(), ["storage", "use", "--db", str(db)]).output
    assert "1 short of what is there: their owner can't read all of them (more time won't help)" in out
    assert "never finished" not in out


@pytest.mark.skipif(not ROOT or not shutil.which("setpriv"), reason="runs the script as another account")
@pytest.mark.parametrize("name", ["q'q", "é", "sp ace", "nl"])
def test_unreadable_folders_are_counted_in_any_locale(tmp_path, name):
    """Counted from find's own output, not its messages (which quote odd
    names differently in each locale)."""
    nobody = _account("nobody")
    base = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        os.chmod(base, 0o755)
        home = base / "home"
        # "nl": the unreadable folder's own name holds a line break (no D
        # line is printed for it, but its X line still is).
        top = home / name
        hidden = top / ("hid\nden" if name == "nl" else "hidden")
        hidden.mkdir(parents=True)
        (hidden / "f").write_bytes(os.urandom(5000))
        for p in [top, *top.rglob("*")]:
            os.lchown(p, nobody.pw_uid, nobody.pw_gid)
        hidden.chmod(0)
        work = base / "work"
        work.mkdir()
        os.chown(work, nobody.pw_uid, nobody.pw_gid)
        script = _script(base)
        r = subprocess.run(["setpriv", f"--reuid={nobody.pw_uid}", f"--regid={nobody.pw_gid}", "--clear-groups",
                            SH, str(script), "-d", str(work / "out"), "-O", "scan", str(home)],
                           capture_output=True, text=True, timeout=120, cwd=str(work),
                           env={"PATH": os.environ["PATH"], "LC_ALL": "C"})
        assert r.returncode == 0, r.stderr
        (t,) = storage.parse((work / "out" / "scan.txt").read_text()).tops
        assert t.denied == 1 and t.why_short == "unreadable"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_rows_of_a_path_now_scanned_once_go(tmp_path):
    """An older script scanned the same folder under two names; the new one
    scans it once and says so: the second name's figures go."""
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": []}, db)
    old = _scan_text(8_000, 1792000000).replace("#present\t/home\nP\talice\n", "#present\t/home\nP\talice\n"
                                                "#present\t/link\nP\talice\n")
    old = old.replace("#not-scanned", "#top\t/link\talice\t5001\t1792000000\n"
                      "F\tdata\t5001\t8000\t1\t1792000000\t0\t0\n#rc\t0\n#denied\t0\n#endtop\t1\n#not-scanned")
    c.store([{"type": "storage_report", "server": "fs1", "read_at": "2026-10-21T02:00:00",
              "report": storage.parse(old)}])
    conn = sqlite3.connect(db)
    assert {p for (p,) in conn.execute("SELECT DISTINCT path FROM storage_tops")} == {"/home", "/link"}
    new = _scan_text(8_000, 1792086400).replace("#not-scanned", "#nested\t/link\t\t/home\n#not-scanned")
    c.store([{"type": "storage_report", "server": "fs1", "read_at": "2026-10-22T02:00:00",
              "report": storage.parse(new)}])
    assert {p for (p,) in conn.execute("SELECT DISTINCT path FROM storage_tops")} == {"/home"}
    assert {p for (p,) in conn.execute("SELECT DISTINCT path FROM storage_folders")} == {"/home"}


def test_a_copy_under_a_path_that_is_none_is_still_a_copy():
    rows = [{"server": "bk", "path": "/backup", "copy_of": None, "used_bytes": 10_000,
             "by_dataset": {"bk": 1_000, "bk/home": 9_000}},
            {"server": "bk", "path": "/backup/home", "copy_of": "fs0", "used_bytes": 9_000,
             "by_dataset": {"bk/home": 9_000}}]
    assert views.totals(rows) == {"total_bytes": 1_000, "copies_bytes": 9_000}


def test_a_folders_access_time_reset_doesnt_make_it_due(tmp_path):
    """Backups that keep access times change a folder's ctime every night:
    only its mtime, owner or mode make it due."""
    home = tmp_path / "home"
    (home / "a" / "d").mkdir(parents=True)
    env, log = _sudo(tmp_path)
    out = tmp_path / "out"
    assert len(_scan(tmp_path, out, "-O", "scan", str(home), env=env).tops) == 1
    time.sleep(1.1)
    st = os.stat(home / "a")
    os.utime(home / "a", (st.st_atime - 3600, st.st_mtime))          # a new ctime, the same mtime
    rep = _scan(tmp_path, out, "-O", "scan", str(home), env=env)
    assert not rep.tops and rep.carried == 1
    os.chmod(home / "a", 0o750)                                       # a new mode
    rep = _scan(tmp_path, out, "-O", "scan", str(home), env=env)
    assert len(rep.tops) == 1
