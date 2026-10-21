"""nomad 1.16.0: who uses each storage server's space (nomad-storage.sh, the
storage_use collector, `nomad storage`)."""
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad import storage
from nomad.collectors import storage_use as su
from nomad.storage import views

SH = shutil.which("dash") or shutil.which("sh")
GNU_STAT = shutil.which("stat")

BSD_STAT = """#!/bin/sh
# BSD stat's -f FORMAT, on GNU stat (the FreeBSD branch, tested on Linux).
if [ "$1" = "-f" ] && [ "$2" != "-c" ]; then
    fmt=$(printf '%s' "$2" | sed -e 's/%m/%Y/g' -e 's/%l/%h/g' -e 's/%N/%n/g' -e 's/%z/%s/g')
    shift 2
    exec REALSTAT -c "$fmt" "$@"
fi
exec REALSTAT "$@"
"""


REAL_PATH = "PATH=/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin"
GNU_TEST = "if find / -maxdepth 0 -printf '' >/dev/null 2>&1; then GNUFIND=1; else GNUFIND=; fi"


def _script(tmp_path, bsd=False, real=False) -> Path:
    """The script, for a test: by default it keeps the test's PATH first (for
    the shims) and, with bsd, takes the BSD way on this GNU system (with the
    stat shim). `real`: as shipped."""
    text = storage.script_text()
    assert REAL_PATH in text and GNU_TEST in text
    if not real:
        text = text.replace(REAL_PATH, 'PATH=${PATH:+$PATH:}' + REAL_PATH[5:])
    if bsd:
        text = text.replace(GNU_TEST, "GNUFIND=")
    p = tmp_path / ("nomad-storage-real.sh" if real else "nomad-storage-bsd.sh" if bsd else "nomad-storage.sh")
    p.write_text(text)
    return p


def _run(tmp_path, *args, env=None, check=True, real=False):
    e = dict(os.environ)
    e.update(env or {})
    bsd = e.pop("NOMAD_STORAGE_FIND", None) == "bsd"
    r = subprocess.run([SH, str(_script(tmp_path, bsd=bsd, real=real)), *args], capture_output=True, text=True,
                       env=e, timeout=120)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def _tree(root: Path) -> Path:
    home = root / "home"
    (home / "jsmith" / "proj").mkdir(parents=True)
    (home / "jsmith" / "old").mkdir()
    (home / "rjones" / "my data").mkdir(parents=True)
    (home / "jsmith" / "proj" / "a.bin").write_bytes(os.urandom(300_000))
    os.link(home / "jsmith" / "proj" / "a.bin", home / "jsmith" / "proj" / "a-link.bin")
    old = home / "jsmith" / "old" / "b.bin"
    old.write_bytes(os.urandom(100_000))
    t = time.time() - 400 * 86400
    os.utime(old, (t, t))
    (home / "jsmith" / "loose.txt").write_text("hi\n")
    (home / "rjones" / "my data" / "c.txt").write_bytes(os.urandom(5000))
    return home


def _shims(tmp_path) -> Path:
    d = tmp_path / "shim"
    d.mkdir(exist_ok=True)
    s = d / "stat"
    s.write_text(BSD_STAT.replace("REALSTAT", GNU_STAT))
    s.chmod(0o755)
    return d


def _bsd_env(tmp_path):
    return {"PATH": f"{_shims(tmp_path)}{os.pathsep}{os.environ['PATH']}", "NOMAD_STORAGE_FIND": "bsd"}


def _shim(tmp_path, name, body):
    d = tmp_path / "shim2"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_text("#!/bin/sh\n" + body)
    f.chmod(0o755)
    return d


def _with(d, env=None):
    e = dict(env or {})
    e["PATH"] = f"{d}{os.pathsep}{e.get('PATH', os.environ['PATH'])}"
    return e


def _scan(tmp_path, out, home, env=None, *more):
    _run(tmp_path, "-d", str(out), *more, "scan", str(home), env=env)
    return storage.parse((out / "scan.txt").read_text())


# A timeout that stops the folders whose path holds WORD, and runs the rest.
SLOW = """for a; do case $a in *WORD*) exit 124 ;; esac; done
shift 3
exec "$@"
"""


# --- the script ----------------------------------------------------------------------

@pytest.mark.parametrize("flavour", ["gnu", "bsd"])
def test_the_scan_finds_each_folders_size_and_age(tmp_path, flavour):
    home = _tree(tmp_path)
    (home / "jsmith" / "back\\slash").mkdir()
    (home / "jsmith" / "back\\slash" / "d.txt").write_bytes(os.urandom(3000))
    out = tmp_path / "out"
    rep = _scan(tmp_path, out, home, _bsd_env(tmp_path) if flavour == "bsd" else None)
    assert rep.mode == "scan" and rep.ages == (180, 365) and rep.not_scanned == 0
    assert rep.run_uid == os.getuid() and rep.run_by
    assert sorted(rep.present[str(home)]) == ["jsmith", "rjones"] and rep.odd_names == 0
    tops = {t.name: t for t in rep.tops}
    assert set(tops) == {"jsmith", "rjones"} and all(t.complete for t in tops.values())
    assert all(t.denied == 0 for t in tops.values())
    js = {f.name: f for f in tops["jsmith"].folders}
    assert set(js) == {"proj", "old", ".", "back\\slash"}
    assert js["proj"].files == 1                          # a hard link counts once
    assert js["old"].older2_bytes == js["old"].bytes > 0 and js["proj"].older1_bytes == 0
    assert {f.name for f in tops["rjones"].folders} == {"my data"}
    assert (out / "scan.txt").stat().st_mode & 0o777 == 0o640
    assert (out.stat().st_mode & 0o777) == 0o750 and (out / ".nomad-storage").exists()
    state = [line.split("\t") for line in (out / "scan.state").read_text().splitlines()]
    assert sorted(s[2] for s in state) == [str(home / "jsmith"), str(home / "rjones")]
    assert {s[1] for s in state} == {"done"}
    assert not [p for p in out.iterdir() if ".tmp." in p.name or "scan.list" in p.name or ".err." in p.name
                or "scan.cand" in p.name or p.name.startswith(".lock")]


def test_the_folders_tried_longest_ago_go_first_and_gone_ones_are_dropped(tmp_path):
    home = _tree(tmp_path)
    out = tmp_path / "out"
    assert _scan(tmp_path, out, home, None, "-s", "10").not_scanned == 2      # no time at all
    (out / "scan.state").write_text(f"100\tdone\t{home}/jsmith\n50\tdone\t{home}/rjones\n"
                                    f"10\tdone\t{home}/gone\n")
    assert [t.name for t in _scan(tmp_path, out, home).tops] == ["rjones", "jsmith"]
    assert "gone" not in (out / "scan.state").read_text()


def test_a_folder_too_big_for_the_budget_goes_to_the_back(tmp_path):
    home = _tree(tmp_path)
    out = tmp_path / "out"
    env = _with(_shim(tmp_path, "timeout", SLOW.replace("WORD", "jsmith")))
    rep = _scan(tmp_path, out, home, env)
    tops = {t.name: t for t in rep.tops}
    assert tops["jsmith"].rc == 124 and not tops["jsmith"].complete and tops["rjones"].complete
    state = {line.split("\t")[2]: line.split("\t")[:2] for line in (out / "scan.state").read_text().splitlines()}
    assert state[str(home / "jsmith")][1] == "partial" and int(state[str(home / "jsmith")][0]) > 0
    # Tried later than rjones: rjones goes first next time.
    (out / "scan.state").write_text(f"200\tpartial\t{home}/jsmith\n100\tdone\t{home}/rjones\n")
    assert [t.name for t in _scan(tmp_path, out, home, env).tops] == ["rjones", "jsmith"]


def test_top_folders_with_odd_names_are_left_out_and_counted(tmp_path):
    home = _tree(tmp_path)
    (home / "evil\n..").mkdir()
    (home / "alice\t").mkdir()
    (home / "a\\b").mkdir()
    (home / "link").symlink_to(home / "jsmith")
    (home / ".zfs").mkdir()
    out = tmp_path / "out"
    rep = _scan(tmp_path, out, home)
    assert sorted(t.name for t in rep.tops) == ["jsmith", "rjones"]
    assert rep.odd_names == 3


@pytest.mark.parametrize("flavour", ["gnu", "bsd"])
def test_a_file_named_with_spaces_only_is_counted(tmp_path, flavour):
    home = _tree(tmp_path)
    (home / "rjones" / " ").write_bytes(os.urandom(50_000))
    (home / "rjones" / "  ").write_bytes(os.urandom(50_000))
    out = tmp_path / "out"
    rep = _scan(tmp_path, out, home, _bsd_env(tmp_path) if flavour == "bsd" else None)
    rj = {f.name: f for t in rep.tops if t.name == "rjones" for f in t.folders}
    assert rj["."].files == 2 and rj["."].bytes >= 100_000


@pytest.mark.parametrize("flavour", ["gnu", "bsd"])
def test_a_line_break_in_a_name_cannot_make_up_figures(tmp_path, flavour):
    home = _tree(tmp_path)
    (home / "jsmith" / "x\n1 99999999999999999999 1 77 yy").write_text("x")
    (home / "jsmith" / "d\nir").mkdir()
    (home / "jsmith" / "d\nir" / "f").write_bytes(os.urandom(4000))
    out = tmp_path / "out"
    rep = _scan(tmp_path, out, home, _bsd_env(tmp_path) if flavour == "bsd" else None)
    js = {f.name: f for t in rep.tops if t.name == "jsmith" for f in t.folders}
    assert "yy" not in js and max(f.bytes for f in js.values()) < 10**9
    assert js[views.LINE_BREAKS].files == 2               # the two whose paths hold a line break
    c, db = _collector(tmp_path, out)
    c.store(c.collect())
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM storage_folders").fetchone()[0] > 0


def test_parse_drops_figures_no_filesystem_has():
    text = ("#nomad-storage\t1\tscan\n#started\t1\n#top\t/home\tx\t0\t1\n"
            "F\t.\t0\t512\t1\t95000000000\t0\t0\n"
            "F\tmade-up\t0\t512\t1\t100000000000000000000\t0\t0\n"
            "F\tbig\t0\t9999999999999999999999\t1\t1\t0\t0\n"
            "F\tok\t0\t512\t1\t1700000000\t0\t0\n#rc\t0\n#endtop\t1\n#finished\t2\n")
    (t,) = storage.parse(text).tops
    assert [(f.name, f.newest) for f in t.folders] == [(".", None), ("ok", 1700000000)]


@pytest.mark.parametrize("names", [["x\r#top\tHOME\trjones\t0\t0"], ["f\tg\x0bh"]])
def test_control_characters_stay_in_their_field(tmp_path, names):
    home = _tree(tmp_path)
    d = home / "jsmith" / names[0].replace("/", "")
    d.mkdir()
    (d / "f").write_bytes(os.urandom(2000))
    (home / "rjones" / "my data" / "x\r__NOMAD_STORAGE_FILE__ usage").write_text("x")
    out = tmp_path / "out"
    text = (_scan(tmp_path, out, home), (out / "scan.txt").read_text())[1]
    assert "\r" not in text and "\x0b" not in text
    assert set(su.split_files(f"{su.MARK} scan\n{text}")) == {"scan"}
    rep = storage.parse(text)
    assert sorted(t.name for t in rep.tops) == ["jsmith", "rjones"]


def test_the_output_folder_must_be_its_own(tmp_path):
    home = _tree(tmp_path)
    mine = tmp_path / "pool"
    mine.mkdir(mode=0o755)
    r = _run(tmp_path, "-d", str(mine), "usage", str(home), check=False)
    assert r.returncode == 2 and "wasn't made by nomad-storage.sh" in r.stderr
    assert mine.stat().st_mode & 0o777 == 0o755              # left as it was
    (tmp_path / "l").symlink_to(tmp_path / "pool")
    r = _run(tmp_path, "-d", str(tmp_path / "l"), "usage", str(home), check=False)
    assert r.returncode == 2 and "symbolic link" in r.stderr


def test_one_run_at_a_time_and_a_stale_lock_is_taken_over(tmp_path):
    home = _tree(tmp_path)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "usage", str(home))
    lock = out / ".lock-usage"
    lock.mkdir()
    r = _run(tmp_path, "-d", str(out), "usage", str(home))             # no pid yet: being made
    assert "is starting" in r.stderr and lock.exists()
    (lock / "pid").write_text(f"{os.getpid()}\n")                       # alive: this test
    r = _run(tmp_path, "-d", str(out), "usage", str(home))
    assert "still going" in r.stderr
    # Older than any usage run can be: its number may be someone else's now.
    old = time.time() - 2 * 3600
    os.utime(lock, (old, old))
    r = _run(tmp_path, "-d", str(out), "usage", str(home))
    assert r.stderr == "" and not lock.exists()
    lock.mkdir()
    (lock / "pid").write_text("999999999\n")                            # gone
    r = _run(tmp_path, "-d", str(out), "usage", str(home))
    assert r.stderr == "" and not lock.exists()


def test_a_run_stopped_partway_leaves_nothing_behind(tmp_path):
    home = _tree(tmp_path)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "usage", str(home))
    env = _with(_shim(tmp_path, "timeout", "sleep 30\n"))
    e = dict(os.environ)
    e.update(env)
    # As systemd stops a unit (and an admin a cron job's process group): TERM to them all.
    p = subprocess.Popen([SH, str(_script(tmp_path)), "-d", str(out), "scan", str(home)], env=e,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(100):
        if (out / ".lock-scan").exists():
            break
        time.sleep(0.05)
    time.sleep(0.3)
    os.killpg(p.pid, 15)
    p.wait(timeout=20)
    left = sorted(x.name for x in out.iterdir())
    assert left == [".nomad-storage", "scan.state", "usage.txt"], left


def test_the_script_uses_its_own_path_never_the_callers(tmp_path):
    # Under sudo the caller's PATH would choose what root runs.
    home = _tree(tmp_path)
    fake = _shim(tmp_path, "hostname", "echo FAKE\n")
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "usage", str(home), env=_with(fake), real=True)
    text = (out / "usage.txt").read_text()
    assert "#host\tFAKE" not in text and "#host\t" in text


def test_folders_others_can_change_are_refused(tmp_path):
    home = _tree(tmp_path)
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o777)                                  # writable by all, not sticky
    r = _run(tmp_path, "-d", str(open_dir / "out"), "usage", str(home), check=False)
    assert r.returncode == 2 and "can be changed by others" in r.stderr and not (open_dir / "out").exists()
    shared = tmp_path / "shared"
    shared.mkdir()
    _run(tmp_path, "-d", str(shared / "out"), "usage", str(home))
    shared.chmod(0o775)                                    # writable by its group, later
    r = _run(tmp_path, "-d", str(shared / "out"), "usage", str(home), check=False)
    assert r.returncode == 2 and "above" in r.stderr
    sticky = tmp_path / "sticky"
    sticky.mkdir()
    sticky.chmod(0o1777)                                   # as /tmp: fine
    _run(tmp_path, "-d", str(sticky / "out"), "usage", str(home))
    # The script itself, writable by its group.
    script = _script(tmp_path)
    script.chmod(0o664)
    r = subprocess.run([SH, str(script), "-d", str(tmp_path / "o2"), "usage", str(home)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "install it owned by root" in r.stderr


def test_usage_from_zfs(tmp_path):
    home = _tree(tmp_path)
    d = _shims(tmp_path)
    (d / "zfs").write_text(f"""#!/bin/sh
case "$*" in
  "list -H -o name {home}") echo tank/home ;;
  "get -H -o value mountpoint tank/home") echo {home} ;;
  "list -H -p -o name,mountpoint,used,available,quota,refquota,referenced -r -t filesystem tank/home")
     printf 'tank/home\\t{home}\\t5000\\t9000\\t0\\t0\\t4000\\n'
     printf 'tank/home/lab\\t{home}/lab\\t1000\\t9000\\t2000\\t0\\t1000\\n'
     printf 'tank/home/away\\t/mnt/elsewhere\\t7\\t9000\\t0\\t0\\t7\\n' ;;
  "userspace -H -p -o type,name,used,quota tank/home") printf 'POSIX User\\tjsmith\\t3000\\tnone\\nPOSIX User\\t1234\\t1000\\t5000\\n' ;;
  "groupspace -H -p -o type,name,used,quota tank/home") printf 'POSIX Group\\tpeople\\t4000\\tnone\\n' ;;
  "userspace -H -p -o type,name,used,quota tank/home/lab") printf 'POSIX User\\tjsmith\\t1000\\t3000\\n' ;;
  "groupspace -H -p -o type,name,used,quota tank/home/lab") echo "cannot get used: permission denied" >&2; exit 1 ;;
  *) echo "unexpected: $*" >&2; exit 1 ;;
esac
""")
    (d / "zfs").chmod(0o755)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "usage", str(home), env={"PATH": f"{d}{os.pathsep}{os.environ['PATH']}"})
    rep = storage.parse((out / "usage.txt").read_text())
    (p,) = rep.paths
    assert p.fs == "zfs" and p.dataset == "tank/home" and p.accounting and p.total_bytes and p.covers is None
    assert p.errors == ["zfs groupspace tank/home/lab: cannot get used: permission denied"]
    assert {x.name: x.quota_bytes for x in rep.datasets} == {"tank/home": None, "tank/home/lab": 2000}
    users = {(o.dataset, o.name): o for o in rep.owners if o.kind == "user"}
    assert users[("tank/home", "jsmith")].used_bytes == 3000 and users[("tank/home", "1234")].hard_bytes == 5000
    assert users[("tank/home/lab", "jsmith")].hard_bytes == 3000
    # Stored and shown: jsmith's 4000 in two datasets, each with its own quota.
    c, db = _collector(tmp_path, out)
    c.store(c.collect())
    conn = sqlite3.connect(db)
    (js,) = [r for r in views.owners(conn, "fs1", str(home)) if r["name"] == "jsmith"]
    assert js["used_bytes"] == 4000 and js["hard_bytes"] is None and len(js["quotas"]) == 1
    assert [x["dataset"] for x in views.datasets(conn, "fs1", str(home))] == ["tank/home", "tank/home/lab"]


def test_a_path_inside_a_dataset_says_the_counts_are_the_datasets(tmp_path):
    home = _tree(tmp_path)
    d = _shims(tmp_path)
    (d / "zfs").write_text(f"""#!/bin/sh
case "$*" in
  "list -H -o name {home}") echo tank ;;
  "get -H -o value mountpoint tank") echo /mnt/tank ;;
  "list -H -p"*) printf 'tank\\t/mnt/tank\\t5000\\t9000\\t0\\t0\\t4000\\n' ;;
  *) exit 0 ;;
esac
""")
    (d / "zfs").chmod(0o755)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "usage", str(home), env={"PATH": f"{d}{os.pathsep}{os.environ['PATH']}"})
    (p,) = storage.parse((out / "usage.txt").read_text()).paths
    assert p.covers == "/mnt/tank"


def test_usage_from_xfs_accounting(tmp_path):
    home = _tree(tmp_path)
    d = _shims(tmp_path)
    (d / "stat").write_text(f"""#!/bin/sh
if [ "$1" = "-f" ] && [ "$2" = "-c" ] && [ "$3" = "%T" ]; then echo xfs; exit 0; fi
exec {GNU_STAT} "$@"
""")
    (d / "xfs_quota").write_text("""#!/bin/sh
case "$3" in
  "report -u -b -N") printf 'jsmith      2048      0      0     00 [--------]\\n#4242       10240  0  20480  00 [--------]\\n' ;;
  "report -g -b -N") printf 'people      12288      0      0     00 [--------]\\n' ;;
  *) exit 1 ;;
esac
""")
    for f in ("stat", "xfs_quota"):
        (d / f).chmod(0o755)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "usage", str(home), env={"PATH": f"{d}{os.pathsep}{os.environ['PATH']}"})
    rep = storage.parse((out / "usage.txt").read_text())
    p = rep.paths[0]
    assert p.fs == "xfs" and p.accounting
    assert p.covers == p.mount != str(home)          # the counts are the whole filesystem's
    by = {(o.kind, o.name): o for o in rep.owners}
    assert by[("user", "jsmith")].used_bytes == 2048 * 1024 and by[("user", "jsmith")].hard_bytes is None
    assert by[("user", "4242")].hard_bytes == 20480 * 1024          # an account the server can't name
    assert by[("group", "people")].used_bytes == 12288 * 1024


def test_xfs_with_accounting_off_says_so(tmp_path):
    home = _tree(tmp_path)
    d = _shims(tmp_path)
    (d / "stat").write_text(f"""#!/bin/sh
if [ "$1" = "-f" ] && [ "$2" = "-c" ] && [ "$3" = "%T" ]; then echo xfs; exit 0; fi
exec {GNU_STAT} "$@"
""")
    (d / "xfs_quota").write_text("#!/bin/sh\nexit 0\n")
    for f in ("stat", "xfs_quota"):
        (d / f).chmod(0o755)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "usage", str(home), env={"PATH": f"{d}{os.pathsep}{os.environ['PATH']}"})
    (p,) = storage.parse((out / "usage.txt").read_text()).paths
    assert not p.accounting and "accounting are off" in p.note


def test_a_filesystem_without_accounting_says_so(tmp_path):
    home = _tree(tmp_path)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "usage", str(home))
    (p,) = storage.parse((out / "usage.txt").read_text()).paths
    assert not p.accounting and "scan counts by folder" in p.note


@pytest.mark.skipif(os.getuid() != 0 or not shutil.which("setpriv"), reason="needs root and setpriv")
def test_an_account_without_root_says_what_it_could_not_read(tmp_path):
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="nomad-storage-"))
    try:
        base.chmod(0o755)
        home = _tree(base)
        for x in [base, *base.rglob("*")]:
            if x.is_dir():
                x.chmod(0o755)
            else:
                x.chmod(0o644)
        (home / "jsmith" / "private").mkdir(mode=0o700)
        (home / "jsmith" / "private" / "z").write_text("z")
        out = base / "out"
        out.mkdir(mode=0o750)
        (out / ".nomad-storage").touch()
        os.chown(out, 65534, 65534)
        os.chown(out / ".nomad-storage", 65534, 65534)
        script = base / "nomad-storage.sh"
        script.write_text(storage.script_text())
        script.chmod(0o644)
        r = subprocess.run(["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", SH, str(script),
                            "-d", str(out), "scan", str(home)], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        rep = storage.parse((out / "scan.txt").read_text())
        assert rep.run_uid == 65534
        (js,) = [t for t in rep.tops if t.name == "jsmith"]
        assert js.denied >= 1 and js.complete
        # A path it can't list at all says so.
        closed = base / "closed"
        closed.mkdir(mode=0o700)
        (closed / "x").mkdir()
        r = subprocess.run(["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", SH, str(script),
                            "-d", str(out), "scan", str(home), str(closed)], capture_output=True, text=True,
                           timeout=120)
        assert r.returncode == 0, r.stderr
        rep = storage.parse((out / "scan.txt").read_text())
        assert rep.unreadable == [str(closed)] and str(closed) not in rep.present
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_the_script_refuses_what_it_does_not_understand(tmp_path):
    r = _run(tmp_path, "-d", "relative", "usage", "/", check=False)
    assert r.returncode == 2 and "absolute" in r.stderr
    r = _run(tmp_path, "-d", str(tmp_path / "o"), "-a", "x", "scan", "/", check=False)
    assert r.returncode == 2
    r = _run(tmp_path, "-d", str(tmp_path / "o"), "delete", "/", check=False)
    assert r.returncode == 2 and "usage or scan" in r.stderr
    r = _run(tmp_path, "-d", str(tmp_path / "o\tx"), "usage", "/", check=False)
    assert r.returncode == 2 and "tab" in r.stderr


def test_parse_refuses_other_files_and_files_cut_short():
    with pytest.raises(storage.ReportError, match="not a nomad-storage"):
        storage.parse("hello\n")
    with pytest.raises(storage.ReportError, match="cut short"):
        storage.parse("#nomad-storage\t1\tusage\n#started\t1\n")
    with pytest.raises(storage.ReportError, match="newer"):
        storage.parse("#nomad-storage\t99\tusage\n#finished\t2\n")


# --- the collector -------------------------------------------------------------------

def _collector(tmp_path, out, **cfg):
    db = tmp_path / "site.db"
    src = {"server": "fs1", "dir": str(out)}
    src.update(cfg.pop("source", {}))
    c = su.StorageUseCollector({"sources": [src], "every_minutes": 0.001, **cfg}, db)
    return c, db


def test_the_collector_stores_each_file_once(tmp_path):
    home = _tree(tmp_path)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "usage", str(home))
    _run(tmp_path, "-d", str(out), "scan", str(home))
    c, db = _collector(tmp_path, out)
    c.store(c.collect())
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM storage_reports").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(DISTINCT top) FROM storage_folders").fetchone()[0] == 2
    assert set(dict(conn.execute("SELECT top, top_owner FROM storage_tops"))) == {"jsmith", "rjones"}
    time.sleep(0.1)
    c.store(c.collect())                                   # the same files: nothing new
    assert conn.execute("SELECT COUNT(*) FROM storage_reports").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM storage_tops").fetchone()[0] == 2


def test_a_path_that_lists_nothing_keeps_its_figures(tmp_path):
    # Not mounted that night, say: no news of any folder.
    home = _tree(tmp_path)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "scan", str(home))
    c, db = _collector(tmp_path, out)
    c.store(c.collect())
    aside = tmp_path / "aside"
    home.rename(aside)
    home.mkdir()
    time.sleep(1.1)
    _run(tmp_path, "-d", str(out), "scan", str(home))
    c.store(c.collect())
    conn = sqlite3.connect(db)
    assert {t for (t,) in conn.execute("SELECT DISTINCT top FROM storage_tops")} == {"jsmith", "rjones"}


def test_usage_and_scan_in_folders_of_their_own(tmp_path, monkeypatch):
    home = _tree(tmp_path)
    _run(tmp_path, "-d", str(tmp_path / "u"), "usage", str(home))
    _run(tmp_path, "-d", str(tmp_path / "s"), "scan", str(home))
    c, db = _collector(tmp_path, tmp_path / "s", source={"usage_dir": str(tmp_path / "u")})
    c.store(c.collect())
    modes = {m for (m,) in sqlite3.connect(db).execute("SELECT mode FROM storage_reports")}
    assert modes == {"usage", "scan"}
    cmd = su.fetch_command({"usage": "/a", "scan": "/b"})
    assert "/a/usage.txt" in cmd and "/b/scan.txt" in cmd


def test_a_top_folder_gone_from_the_path_goes_from_the_tables(tmp_path):
    home = _tree(tmp_path)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "scan", str(home))
    c, db = _collector(tmp_path, out)
    c.store(c.collect())
    shutil.rmtree(home / "rjones")
    time.sleep(1.1)
    _run(tmp_path, "-d", str(out), "scan", str(home))
    c.store(c.collect())
    conn = sqlite3.connect(db)
    for table in ("storage_tops", "storage_folders"):
        assert {t for (t,) in conn.execute(f"SELECT DISTINCT top FROM {table}")} == {"jsmith"}


def test_a_file_that_cannot_be_stored_does_not_hold_up_the_others(tmp_path, monkeypatch):
    home = _tree(tmp_path)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "usage", str(home))
    _run(tmp_path, "-d", str(out), "scan", str(home))
    c, db = _collector(tmp_path, out)
    real = c._store_report

    def flaky(conn, server, read_at, rep, copy_of=None):
        if rep.mode == "scan":
            conn.execute("INSERT INTO storage_tops (scanned_at, server, path, top) VALUES ('x', 'fs1', '/', 't')")
            raise OverflowError("Python int too large to convert to SQLite INTEGER")
        return real(conn, server, read_at, rep, copy_of)
    monkeypatch.setattr(c, "_store_report", flaky)
    c.store(c.collect())
    conn = sqlite3.connect(db)
    assert [m for (m,) in conn.execute("SELECT mode FROM storage_reports")] == ["usage"]
    assert conn.execute("SELECT COUNT(*) FROM storage_tops").fetchone()[0] == 0     # rolled back
    (note,) = conn.execute("SELECT note FROM storage_reads").fetchone()
    assert "scan.txt not stored" in note


def test_a_scan_stopped_at_the_budget_keeps_the_last_complete_one(tmp_path):
    home = _tree(tmp_path)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "scan", str(home))
    c, db = _collector(tmp_path, out)
    c.store(c.collect())
    time.sleep(1.1)
    _scan(tmp_path, out, home, _with(_shim(tmp_path, "timeout", SLOW.replace("WORD", "home"))))
    c.store(c.collect())
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT MIN(complete) FROM storage_folders").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM storage_tops WHERE complete = 0").fetchone()[0] == 2
    # What is shown is the finished scan's, not the stopped one's.
    shown = views.folders_by_owner(conn, "fs1", str(home))
    assert all(r["complete"] and r["bytes"] > 0 for r in shown.values())
    assert views.coverage(conn, "fs1", str(home))["unfinished"] == 0


def test_the_collector_reads_over_ssh(tmp_path, monkeypatch):
    home = _tree(tmp_path)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "usage", str(home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # An ssh that runs the command here, as the server would.
    (bindir / "ssh").write_text('#!/bin/sh\nfor a; do last="$a"; done\nexec sh -c "$last"\n')
    (bindir / "ssh").chmod(0o755)
    monkeypatch.setattr(su, "find_tool", lambda name: str(bindir / name))
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": [{"server": "fs1", "host": "fs1", "dir": str(out)}]}, db)
    data = c.collect()
    assert any(d["type"] == "storage_report" for d in data)
    c.store(data)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM storage_paths").fetchone()[0] == 1


def test_sources_that_would_be_read_otherwise_are_left_out():
    ok, problems = su.sources_from({"sources": [
        {"server": "fs1", "host": "fs1", "dir": "/var/lib/nomad-storage", "copy_of": "fs0"},
        {"server": "fs2", "dir": "/x'; rm -rf /"},
        {"server": "fs3", "host": "a b"},
        {"server": "fs5", "copy_of": "a;b"},
        {"server": "fs1"},
        "fs4"]})
    assert [(s["server"], s["copy_of"]) for s in ok] == [("fs1", "fs0")]
    assert len(problems) == 5


def _readings(conn, day, hours, rows):
    for h, extra in hours:
        ts = day.replace(hour=h, minute=17, second=0, microsecond=0).isoformat()
        for name, dataset, used in rows + extra:
            conn.execute("INSERT INTO storage_owners (timestamp, server, path, dataset, kind, name, used_bytes) "
                         "VALUES (?, 'fs1', '/home', ?, 'user', ?, ?)", (ts, dataset, name, used))


def test_old_readings_are_thinned_to_one_whole_reading_a_day(tmp_path):
    db = tmp_path / "site.db"
    c = su.StorageUseCollector({"sources": [], "keep_days": 30}, db)
    conn = sqlite3.connect(db)
    conn.executescript(su.SCHEMA)
    day = datetime.now() - timedelta(days=31)
    same = [("alice", "tank/a", 10**12), ("alice", "tank/b", 10**12), ("alice", "tank/c", 10**12)]
    # bob first appears at noon: the day's first reading has no bob.
    _readings(conn, day, [(0, []), (6, []), (12, [("bob", "tank/a", 5)]), (18, [("bob", "tank/a", 5)])], same)
    _readings(conn, datetime.now() - timedelta(hours=1), [(datetime.now().hour - 1 if datetime.now().hour else 0,
                                                             [("bob", "tank/a", 5)])], same)
    c._thin(conn)
    conn.commit()
    ts = {t for (t,) in conn.execute("SELECT DISTINCT timestamp FROM storage_owners")}
    assert len(ts) == 2
    rows = {r["name"]: r for r in views.owners(conn, "fs1", "/home")}
    assert rows["alice"]["change_bytes"] == 0                 # all three datasets kept
    assert rows["alice"]["was_bytes"] == 3 * 10**12
    assert rows["bob"]["change_bytes"] is None                # wasn't there then


def test_the_migration_and_the_plan_know_it(tmp_path):
    from nomad.collectors.plan import NAMES
    from nomad.db.migrations import MigrationManager
    assert "storage_use" in NAMES
    db = tmp_path / "fresh.db"
    with MigrationManager(db) as m:
        m.migrate()
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"storage_owners", "storage_folders", "storage_tops", "storage_paths", "storage_reports"} <= names
    assert not views.available(conn)                     # tables, but nothing in them


# --- what it says --------------------------------------------------------------------

def _hub(tmp_path, sites=("s1",)):
    db = tmp_path / "hub.db"
    conn = sqlite3.connect(db)
    for stmt in su.SCHEMA.split(";"):
        if stmt.strip().upper().startswith("CREATE TABLE"):
            conn.execute(stmt.rstrip().rstrip(")") + ", source_site TEXT)")
    now = datetime(2026, 10, 21, 12, 0)
    then = now - timedelta(days=31)
    for i, site in enumerate(sites):
        for t, js, rj in ((then, 10e12, 1e12), (now, 12e12, 1e12)):
            ts = (t + timedelta(minutes=i)).isoformat()
            conn.execute("INSERT INTO storage_reports (server, mode, started, source_site) "
                         "VALUES ('fs1', 'usage', ?, ?)", (ts, site))
            conn.execute("INSERT INTO storage_paths (timestamp, server, path, fs, total_bytes, used_bytes, "
                         "accounting, source_site) VALUES (?, 'fs1', '/home', 'xfs', 20e12, ?, 1, ?)",
                         (ts, js + rj, site))
            for name, used in (("jsmith", js), ("rjones", rj)):
                conn.execute("INSERT INTO storage_owners (timestamp, server, path, kind, name, used_bytes, "
                             "source_site) VALUES (?, 'fs1', '/home', 'user', ?, ?, ?)", (ts, name, used, site))
        conn.execute("INSERT INTO storage_tops (scanned_at, server, path, top, top_owner, bytes, files, newest, "
                     "older1_bytes, older2_bytes, age1_days, age2_days, complete, source_site) VALUES "
                     "(?, 'fs1', '/home', 'rjones', 'rjones', 1e12, 10, '2025-01-01T00:00:00', 1e12, 9e11, 180, "
                     "365, 1, ?)", (now.isoformat(), site))
    conn.execute("CREATE TABLE jobs (user_name TEXT, submit_time TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('jsmith', '2026-10-20T10:00:00')")
    conn.commit()
    return db, conn, now


def test_who_holds_what_and_how_it_changed(tmp_path):
    db, conn, now = _hub(tmp_path)
    (p,) = views.paths(conn)
    assert p["server"] == "fs1" and p["used_bytes"] == 13e12 and p["copy_of"] is None
    rows = views.owners(conn, "fs1", "/home")
    assert [r["name"] for r in rows] == ["jsmith", "rjones"]
    assert rows[0]["change_bytes"] == 2e12 and rows[0]["last_job"].startswith("2026-10-20")
    assert rows[1]["last_job"] is None and rows[1]["scan"]["older2_bytes"] == 9e11
    cov = views.coverage(conn, "fs1", "/home", now=now)
    assert cov["tops"] == 1 and cov["recent"] == 1 and cov["unfinished"] == 0
    mine = views.person(conn, "rjones")
    assert [(x["server"], x["used_bytes"]) for x in mine] == [("fs1", 1e12)]


def test_an_owner_without_a_name_is_never_counted_under_the_folders_name(tmp_path):
    db, conn, now = _hub(tmp_path)
    conn.execute("INSERT INTO storage_tops (scanned_at, server, path, top, top_uid, top_owner, bytes, files, "
                 "complete, source_site) VALUES (?, 'fs1', '/home', 'jsmith', 4242, NULL, 5e12, 1, 1, 's1')",
                 (now.isoformat(),))
    by = views.folders_by_owner(conn, "fs1", "/home")
    assert "jsmith" not in by and by["uid 4242"]["bytes"] == 5e12
    (js,) = [r for r in views.owners(conn, "fs1", "/home") if r["name"] == "jsmith"]
    assert js["scan"] is None


def test_a_server_read_by_two_sites_is_counted_once(tmp_path):
    db, conn, now = _hub(tmp_path, sites=("s1", "s2"))
    (p,) = views.paths(conn)
    assert p["used_bytes"] == 13e12
    rows = {r["name"]: r for r in views.owners(conn, "fs1", "/home")}
    assert rows["jsmith"]["used_bytes"] == 12e12 and rows["jsmith"]["change_bytes"] == 2e12
    assert views.folders_by_owner(conn, "fs1", "/home")["rjones"]["bytes"] == 1e12
    (r,) = views.reads(conn)
    assert r["sites"] == ["s1", "s2"] and r["site"] == "s2"
    out = CliRunner().invoke(cli_main(), ["storage", "status", "--db", str(db)]).output
    assert "read by 2 sites" in out


def cli_main():
    from nomad.cli import cli
    return cli


def test_storage_use_and_status_and_mask(tmp_path):
    db, conn, _ = _hub(tmp_path)
    r = CliRunner().invoke(cli_main(), ["storage", "use", "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "fs1:/home" in r.output and "jsmith" in r.output and "+2.0 TB" in r.output
    assert "owners as of 2026-10-21 12:00; the change is since 2026-09-20 12:00" in r.output
    r = CliRunner().invoke(cli_main(), ["storage", "use", "--db", str(db), "--days", "7"])
    assert "no reading 7 days back yet" not in r.output and "7 days" in r.output
    r = CliRunner().invoke(cli_main(), ["storage", "use", "--db", str(db), "--mask"])
    assert r.exit_code == 0 and "jsmith" not in r.output and "rjones" not in r.output
    r = CliRunner().invoke(cli_main(), ["storage", "status", "--db", str(db)])
    assert r.exit_code == 0


def test_a_path_with_only_the_scan_shows_the_scans_owners(tmp_path):
    home = _tree(tmp_path)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "scan", str(home))
    c, db = _collector(tmp_path, out, source={"copy_of": "fs0"})
    c.store(c.collect())
    r = CliRunner().invoke(cli_main(), ["storage", "use", "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "no usage file read yet" in r.output and "scanned" in r.output and "holds copies of fs0" in r.output
    conn = sqlite3.connect(db)
    assert views.paths(conn)[0]["copy_of"] == "fs0"
    long_path = CliRunner().invoke(cli_main(), ["storage", "use", "--db", str(db), "--mask"]).output
    assert str(home) not in long_path


def test_storage_script_and_cron(tmp_path):
    r = CliRunner().invoke(cli_main(), ["storage", "script"])
    assert r.exit_code == 0 and r.output.startswith("#!/bin/sh") and "NOMAD_STORAGE_VERSION=1" in r.output
    r = CliRunner().invoke(cli_main(), ["storage", "cron", "/home", "/scratch", "--group", "nomadread",
                                        "--server", "fs1"])
    assert r.exit_code == 0
    assert ("17 * * * * /bin/sh /usr/local/sbin/nomad-storage.sh -d /var/lib/nomad-storage -g nomadread "
            "usage /home /scratch") in r.output
    assert "-s 10800 scan /home /scratch" in r.output and 'server = "fs1"' in r.output
    assert CliRunner().invoke(cli_main(), ["storage", "cron", "home"]).exit_code != 0


def test_cron_through_sudo_allows_only_those_command_lines():
    r = CliRunner().invoke(cli_main(), ["storage", "cron", "/mnt/data", "/a,b:c=d", "--run-as", "sudo",
                                        "--user", "ops", "--group", "ops"])
    assert r.exit_code == 0, r.output
    assert ("ops ALL=(root) NOPASSWD: /bin/sh /usr/local/sbin/nomad-storage.sh -d /var/lib/nomad-storage "
            "-g ops usage /mnt/data /a\\,b\\:c\\=d") in r.output
    assert "17 * * * * sudo -n /bin/sh /usr/local/sbin/nomad-storage.sh" in r.output
    assert CliRunner().invoke(cli_main(), ["storage", "cron", "/x", "--run-as", "sudo"]).exit_code != 0
    # Characters sudoers, cron or systemd would read otherwise are refused.
    for bad in ("/mnt/d*", "/mnt/lab#2", "/mnt/x[1]", "/mnt/50%", "/mnt/$HOME", "/mnt/a b"):
        r = CliRunner().invoke(cli_main(), ["storage", "cron", bad, "--run-as", "sudo", "--user", "ops"])
        assert r.exit_code != 0 and "letters, digits" in r.output, bad


def test_cron_as_a_user_with_only_the_right_to_read():
    r = CliRunner().invoke(cli_main(), ["storage", "cron", "/scratch", "--run-as", "user", "--user", "ops"])
    assert r.exit_code == 0, r.output
    assert "User=ops" in r.output and "AmbientCapabilities=CAP_DAC_READ_SEARCH" in r.output
    assert "ReadWritePaths=/var/lib/nomad-storage" in r.output and "xfs_quota" in r.output


def test_nomad_collect_runs_it_from_nomad_toml(tmp_path, monkeypatch):
    import nomad.alerts.dispatcher as dispatcher_mod
    from nomad.collectors import base
    monkeypatch.setattr(dispatcher_mod, "_dispatcher", None)
    monkeypatch.setattr(base.registry, "_config", {}, raising=False)
    home = _tree(tmp_path)
    out = tmp_path / "out"
    _run(tmp_path, "-d", str(out), "usage", str(home))
    _run(tmp_path, "-d", str(out), "scan", str(home))
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f"""
[general]
data_dir = "{tmp_path}"

[collectors.storage_use]
enabled = true

[[collectors.storage_use.sources]]
server = "fs1"
dir = "{out}"
""")
    db = tmp_path / "site.db"
    cli = cli_main()
    r = CliRunner().invoke(cli, ["-c", str(cfg), "collect", "--once", "-C", "storage_use", "--db", str(db)])
    assert r.exit_code == 0, r.output
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM storage_reports").fetchone()[0] == 2
    r = CliRunner().invoke(cli, ["-c", str(cfg), "storage", "status", "--db", str(db)])
    assert r.exit_code == 0 and "fs1: read" in r.output and "scan: run of" in r.output and ", by " in r.output
    r = CliRunner().invoke(cli, ["-c", str(cfg), "storage", "use", "--db", str(db)])
    assert r.exit_code == 0 and "fs1:" in r.output and "no per-owner counts" in r.output


# --- -S: as an ordinary account, sudo only for xfs_quota, zfs and du ----------------

SUDO_SHIM = '[ "$1" = -n ] && shift\nexec "$@"\n'


def test_sudo_readers_scan_gives_the_same_folders_as_find(tmp_path):
    home = _tree(tmp_path)
    (home / "jsmith" / "proj" / "line\nbreak.txt").write_bytes(os.urandom(4096))
    env = _with(_shim(tmp_path, "sudo", SUDO_SHIM))
    a = _scan(tmp_path, tmp_path / "a", home)
    b = _scan(tmp_path, tmp_path / "b", home, env, "-S")
    assert b.run_by.endswith("reading through sudo")
    fa = {(t.name, f.name): f for t in a.tops for f in t.folders}
    fb = {(t.name, f.name): f for t in b.tops for f in t.folders}
    assert all(t.complete for t in b.tops)
    for key in [("jsmith", "old"), ("jsmith", "."), ("rjones", "my data")]:
        assert (fb[key].bytes, fb[key].files, fb[key].newest, fb[key].older1_bytes, fb[key].older2_bytes) == \
            (fa[key].bytes, fa[key].files, fa[key].newest, fa[key].older1_bytes, fa[key].older2_bytes), key
    # The file with a line break: find counts it apart ("/"), du in its own folder.
    assert fb[("jsmith", "proj")].bytes == fa[("jsmith", "proj")].bytes + fa[("jsmith", views.LINE_BREAKS)].bytes
    assert fb[("jsmith", "proj")].files == 2                 # the hard link once, and the odd name
    assert fb[("jsmith", "old")].older2_bytes == fb[("jsmith", "old")].bytes > 0
    assert ("jsmith", views.LINE_BREAKS) not in fb           # under its own folder, as '?'


def test_sudo_readers_scan_stopped_at_the_budget(tmp_path):
    home = _tree(tmp_path)
    env = _with(_shim(tmp_path, "sudo", SUDO_SHIM))
    env = _with(_shim(tmp_path, "timeout", SLOW.replace("WORD", "jsmith")), env)
    rep = _scan(tmp_path, tmp_path / "o", home, env, "-S")
    tops = {t.name: t for t in rep.tops}
    assert tops["jsmith"].rc == 124 and not tops["jsmith"].complete and tops["rjones"].complete


def test_sudo_readers_usage_asks_sudo_for_xfs_quota(tmp_path):
    home = _tree(tmp_path)
    d = _shims(tmp_path)
    (d / "stat").write_text(f"""#!/bin/sh
if [ "$1" = "-f" ] && [ "$2" = "-c" ] && [ "$3" = "%T" ]; then echo xfs; exit 0; fi
exec {GNU_STAT} "$@"
""")
    # xfs_quota answers only when sudo ran it.
    (d / "xfs_quota").write_text("""#!/bin/sh
[ "$VIA_SUDO" = 1 ] || { echo "permission denied" >&2; exit 1; }
case "$3" in "report -u -b -N") printf 'jsmith      2048      0      0     00 [--------]\\n' ;; *) exit 0 ;; esac
""")
    (d / "sudo").write_text('#!/bin/sh\n[ "$1" = -n ] && shift\nVIA_SUDO=1 exec "$@"\n')
    for f in ("stat", "xfs_quota", "sudo"):
        (d / f).chmod(0o755)
    env = {"PATH": f"{d}{os.pathsep}{os.environ['PATH']}"}
    _run(tmp_path, "-S", "-d", str(tmp_path / "o"), "usage", str(home), env=env)
    rep = storage.parse((tmp_path / "o" / "usage.txt").read_text())
    assert rep.paths[0].accounting and [o.name for o in rep.owners] == ["jsmith"]


def test_cron_for_sudo_readers():
    r = CliRunner().invoke(cli_main(), ["storage", "cron", "/home", "--run-as", "sudo-readers", "--user", "ops",
                                        "--dir", "/home/ops/nomad-storage", "--script", "/home/ops/bin/n.sh"])
    assert r.exit_code == 0, r.output
    assert "17 * * * * /bin/sh /home/ops/bin/n.sh -S -d /home/ops/nomad-storage usage /home" in r.output
    assert "ops ALL=(root) NOPASSWD: /usr/sbin/xfs_quota, /usr/bin/du" in r.output
    r = CliRunner().invoke(cli_main(), ["storage", "cron", "/home", "--run-as", "sudo-readers", "--user", "ops"])
    assert r.exit_code != 0 and "give --dir" in r.output
