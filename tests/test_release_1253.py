# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.25.3: a job's scores hold against the person only what they could have
asked less of. One core is the least a job can ask, and a memory request
already as small as nomad would suggest can't be smaller: neither is "Needs
Work", and no suggestion is the value already asked. A job that ran under
five minutes isn't judged on its CPU use or its time limit (one that ran out
of time still is). A job with nothing to judge has no overall score (None in
JSON, "Not scored"), isn't stored as a score, and isn't told "all good"."""
import json
import sqlite3

from nomad.edu import explain
from nomad.edu.scoring import SHORT_JOB_SECONDS, score_cpu, score_job, score_memory, score_time

# Job 86315 on spydur: a one-minute test sent from the Console with the script's own values
TEST_JOB = {"job_id": "86315", "user_name": "jtonini", "state": "COMPLETED", "partition": "basic",
            "req_cpus": 1, "req_mem_mb": 1024, "req_time_seconds": 2400, "runtime_seconds": 61}
TEST_SUMMARY = {"avg_cpu_percent": 1.0, "peak_memory_gb": 0.012}


def test_the_one_minute_test_job_has_nothing_held_against_it():
    fp = score_job(TEST_JOB, TEST_SUMMARY)
    for name in ("cpu", "memory", "time"):
        d = fp.dimensions[name]
        assert not d.applicable and d.level == "Unknown" and d.suggestion is None, name
    assert "least a job can ask" in fp.dimensions["cpu"].detail
    assert "already as little as nomad would suggest" in fp.dimensions["memory"].detail
    assert "too short to judge" in fp.dimensions["time"].detail
    assert not fp.scored and fp.overall_level == "Not scored" and fp.needs_work == []


def test_what_could_have_been_asked_less_of_is_still_judged():
    # 16 cores at 6% for two hours: Needs Work, and a smaller ask
    d = score_cpu({"req_cpus": 16, "runtime_seconds": 7200}, {"avg_cpu_percent": 6})
    assert d.applicable and d.level == "Needs Work" and d.suggestion.suggested_value < 16
    # one core kept busy: still scored (well)
    d = score_cpu({"req_cpus": 1, "runtime_seconds": 7200}, {"avg_cpu_percent": 97})
    assert d.applicable and d.score > 85
    # 32 GB asked, 1 GB used: Needs Work, suggest 2G
    d = score_memory({"req_mem_mb": 32768}, {"peak_memory_gb": 1.0})
    assert d.applicable and d.level == "Needs Work" and d.suggestion.suggested_value < 32768
    # 2 GB asked, 600 MB used: nomad would suggest 2G itself -- not held against it
    d = score_memory({"req_mem_mb": 2048}, {"peak_memory_gb": 0.6})
    assert not d.applicable and d.suggestion is None
    # 40 minutes asked, 30 seconds used by a longer job's standard: a 10-minute job asking 4 h is judged
    d = score_time({"runtime_seconds": 600, "req_time_seconds": 4 * 3600, "state": "COMPLETED"}, {})
    assert d.applicable and d.level == "Needs Work"


def test_short_jobs_and_their_limits():
    assert SHORT_JOB_SECONDS == 300
    short = {"req_cpus": 8, "runtime_seconds": 120, "req_time_seconds": 3600, "state": "COMPLETED"}
    assert not score_cpu(short, {"avg_cpu_percent": 10}).applicable
    assert not score_time(short, {}).applicable
    # ran out of time after two minutes: the limit was too short, and that is still said
    t = score_time({**short, "state": "TIMEOUT", "req_time_seconds": 120}, {})
    assert t.applicable and t.level == "Needs Work" and t.suggestion.suggested_value > 120


def test_no_suggestion_is_what_was_asked():
    for cpus, util in ((2, 30), (2, 50), (3, 5), (4, 20), (6, 70)):
        d = score_cpu({"req_cpus": cpus, "runtime_seconds": 3600}, {"avg_cpu_percent": util})
        assert d.suggestion is None or d.suggestion.suggested_value < cpus, (cpus, util)


def _db(tmp_path):
    db = tmp_path / "nomad.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (job_id TEXT, user_name TEXT, state TEXT, partition TEXT, req_cpus INTEGER, "
                 "req_mem_mb INTEGER, req_time_seconds INTEGER, runtime_seconds INTEGER, end_time TEXT, "
                 "node_list TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('86315','jtonini','COMPLETED','basic',1,1024,2400,61,"
                 "'2026-11-02T14:06:11','spdr01')")
    conn.execute("CREATE TABLE job_summary (job_id TEXT, avg_cpu_percent REAL, peak_memory_gb REAL)")
    conn.execute("INSERT INTO job_summary VALUES ('86315', 1.0, 0.012)")
    conn.commit()
    conn.close()
    return str(db)


def test_explained_without_a_score(tmp_path):
    db = _db(tmp_path)
    doc = json.loads(explain.explain_job("86315", db, output_format="json", show_progress=False))
    assert doc["overall_score"] is None and doc["overall_level"] == "Not scored"
    assert all(not d["applicable"] for k, d in doc["dimensions"].items() if k in ("cpu", "memory", "time"))
    text = explain.explain_job("86315", db, output_format="terminal", show_progress=False)
    assert "not scored: nothing in this job could be judged" in text and "nice work" not in text
    assert "not judged: One core" in text
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT name FROM sqlite_master WHERE name = 'proficiency_scores'").fetchone()
    assert stored is None or conn.execute("SELECT COUNT(*) FROM proficiency_scores").fetchone()[0] == 0


# ── save: a job script written as the person (gate) ─────────────────────────

import hashlib  # noqa: E402
import io  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

from nomad.submit import gate, remote  # noqa: E402


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def me(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "_uid", lambda: os.getuid())
    monkeypatch.setattr(gate, "_account", lambda: "jdoe")
    monkeypatch.setattr(gate, "_home", lambda: str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    w = tmp_path / "run"
    w.mkdir()
    return w


OLD = "#!/bin/bash\n#SBATCH -c 2\necho old\n"
NEW = "#!/bin/bash\n#SBATCH -c 4\necho new\n"


def test_saved_over_the_very_text_opened_the_one_before_kept(me):
    p = me / "job.sh"
    p.write_text(OLD)
    os.chmod(p, 0o640)
    d = gate.save(str(p), NEW, body_sha=_h(NEW), old_sha=_h(OLD))
    assert d["ok"] and d["sha256"] == _h(NEW) and d["kept"] and not d["new"]
    assert p.read_text() == NEW and stat.S_IMODE(p.stat().st_mode) == 0o640       # its mode kept
    assert [x for x in os.listdir(me) if x.startswith(".")] == []                    # nothing left beside it
    (v,) = gate.versions(str(p))
    assert v["id"] == d["kept"] and gate.recall(str(p), v["id"]) == OLD
    folder = os.path.join(gate._home(), *gate._versions_parts(str(p)))
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(os.path.dirname(folder)).st_mode) == 0o700


def test_versions_never_through_a_link_nor_someone_elses_folder(me, tmp_path):
    p = me / "job.sh"
    p.write_text(OLD)
    # ~/.local/share a link (someone could put one there where ~/.local is open): nothing kept through it
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "home" / ".local").mkdir()
    os.symlink(elsewhere, tmp_path / "home" / ".local" / "share")
    d = gate.save(str(p), NEW, body_sha=_h(NEW), old_sha=_h(OLD))
    assert d["ok"] and d["kept"] is None and any("couldn't be kept" in n for n in d["notes"])
    assert list(elsewhere.iterdir()) == [] and p.read_text() == NEW
    assert gate.versions(str(p)) == []
    with pytest.raises(gate.Refused, match="no version"):
        gate.recall(str(p), "20200101T000000-0123456789ab")
    if os.getuid() == 0:                                   # a folder of someone else's on the way
        os.unlink(tmp_path / "home" / ".local" / "share")
        (tmp_path / "home" / ".local" / "share").mkdir()
        os.chown(tmp_path / "home" / ".local" / "share", 4321, -1)
        d = gate.save(str(p), OLD, body_sha=_h(OLD), old_sha=_h(NEW))
        assert d["ok"] and d["kept"] is None


def test_the_folder_as_it_really_is_and_only_over_a_job_script(me, tmp_path):
    hidden = tmp_path / "home" / ".ssh"
    hidden.mkdir()
    os.symlink(hidden, me / "keys")                       # no part hidden as given; really ~/.ssh
    with pytest.raises(gate.Refused, match="not a hidden folder"):
        gate.save(str(me / "keys" / "authorized_keys"), NEW, body_sha=_h(NEW), new=True)
    assert list(hidden.iterdir()) == []
    (me / "notes.txt").write_text("not a script\n")
    with pytest.raises(gate.Refused, match="isn't a job script"):
        gate.save(str(me / "notes.txt"), NEW, body_sha=_h(NEW), old_sha=_h("not a script\n"))
    assert (me / "notes.txt").read_text() == "not a script\n"


def _acl(entries):
    import struct
    out = struct.pack("<I", 2)
    for tag, perm, uid in entries:
        out += struct.pack("<HHI", tag, perm, uid)
    return out


_ANY = 0xFFFFFFFF


def test_review_group_and_access_lists_as_before(me):
    """A setgid folder's group and default access list don't stay on a
    script that had neither (review 1, 2)."""
    shared = me / "lab"
    shared.mkdir()
    os.chmod(shared, 0o2770)
    if os.getuid() == 0:
        os.chown(shared, -1, 4321)
    os.setxattr(shared, "system.posix_acl_default",
                _acl([(1, 6, _ANY), (2, 6, 1000), (4, 0, _ANY), (0x10, 6, _ANY), (0x20, 0, _ANY)]))
    p = shared / "job.sh"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o660)
    os.write(fd, OLD.encode())
    os.close(fd)
    os.removexattr(p, "system.posix_acl_access")                # the person took it off
    os.chown(p, -1, os.getegid())                               # and gave it their own group
    os.chmod(p, 0o660)
    d = gate.save(str(p), NEW, body_sha=_h(NEW), old_sha=_h(OLD))
    assert d["ok"] and p.read_text() == NEW
    st = p.stat()
    assert st.st_gid == os.getegid() and stat.S_IMODE(st.st_mode) == 0o660
    with pytest.raises(OSError):
        os.getxattr(p, "system.posix_acl_access")
    # one that had its own list keeps it
    own = _acl([(1, 6, _ANY), (2, 4, 2000), (4, 4, _ANY), (0x10, 4, _ANY), (0x20, 0, _ANY)])
    os.setxattr(p, "system.posix_acl_access", own)
    d = gate.save(str(p), OLD, body_sha=_h(OLD), old_sha=_h(NEW))
    assert d["ok"] and os.getxattr(p, "system.posix_acl_access") == own


def test_review_not_utf8_never_saved_over(me):
    """A script that isn't UTF-8 would be changed where it wasn't edited
    (review 3); show says so."""
    p = me / "old.sh"
    raw = "#!/bin/bash\n# autor: João\necho hi\n".encode("latin-1")
    p.write_bytes(raw)
    shown = raw.decode("utf-8", errors="replace")
    with pytest.raises(gate.Refused, match="isn't UTF-8"):
        gate.save(str(p), shown, body_sha=_h(shown), old_sha=hashlib.sha256(raw).hexdigest())
    assert p.read_bytes() == raw
    assert gate.is_utf8(OLD.encode()) and not gate.is_utf8(raw)


def test_review_a_failed_new_file_leaves_nothing_and_is_said(me, monkeypatch):
    """No hard links, then the disk full: nothing half written, and a
    refusal, not a traceback (review 6)."""
    p = me / "new.sh"
    monkeypatch.setattr(gate.os, "link", lambda *a, **k: (_ for _ in ()).throw(PermissionError(1, "no links")))
    real_fsync = os.fsync
    full = {"on": True}

    def fsync(fd):
        if full["on"] and os.readlink(f"/proc/self/fd/{fd}").endswith("/new.sh"):   # the in-place write
            raise OSError(122, "Disk quota exceeded")
        return real_fsync(fd)
    monkeypatch.setattr(gate.os, "fsync", fsync)
    with pytest.raises(gate.Refused, match="not saved|quota"):
        gate.save(str(p), NEW, body_sha=_h(NEW), new=True)
    assert not p.exists() and [x for x in os.listdir(me) if x.startswith(".")] == []
    full["on"] = False
    d = gate.save(str(p), NEW, body_sha=_h(NEW), new=True)            # made in place, once it can be
    assert d["ok"] and p.read_text() == NEW and stat.S_IMODE(p.stat().st_mode) == 0o600


def test_review_kept_before_the_last_look(me, monkeypatch):
    """The copy is kept before the last check; an edit landing meanwhile
    stops the save and the copy is taken back (review 7)."""
    p = me / "job.sh"
    p.write_text(OLD)
    real_keep = gate._keep

    def keep_then_edited(path, data):
        vid = real_keep(path, data)
        p.write_text("#!/bin/bash\necho edited on the cluster\n")
        return vid
    monkeypatch.setattr(gate, "_keep", keep_then_edited)
    with pytest.raises(gate.Refused, match="changed since you opened it"):
        gate.save(str(p), NEW, body_sha=_h(NEW), old_sha=_h(OLD))
    assert "edited on the cluster" in p.read_text() and gate.versions(str(p)) == []


def test_review_version_ids_in_utc(me, monkeypatch):
    import time as _t
    p = me / "job.sh"
    p.write_text(OLD)
    d = gate.save(str(p), NEW, body_sha=_h(NEW), old_sha=_h(OLD))
    assert d["kept"][:8] == _t.strftime("%Y%m%d", _t.gmtime()) or d["kept"][:8] == _t.strftime(
        "%Y%m%d", _t.gmtime(_t.time() - 5))


def test_saving_is_the_clusters_say_too(me, monkeypatch):
    """serve refuses save unless sshd runs it as `nomad-gate serve --edit`."""
    monkeypatch.setattr(gate, "person", lambda name: type("P", (), {"pw_name": "jdoe"})())
    monkeypatch.setattr(gate, "_sudo", lambda: "/usr/bin/sudo")
    monkeypatch.setattr(gate, "_log", lambda m: None)
    calls = []

    def run_(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}\n', "")
    env = {"SSH_ORIGINAL_COMMAND": f"nomad-gate save --user jdoe --script /x/j.sh --new --body-sha256 {_h(NEW)}"}
    code = gate.serve("/g/nomad-gate", env=env, run_=run_, slot_=lambda: os.open(os.devnull, os.O_RDONLY),
                      check_=lambda me: None, stdin=io.BytesIO(NEW.encode()))
    assert code == 2 and calls == []
    out = []
    monkeypatch.setattr(gate, "answer", lambda doc, code=0: out.append(doc) or code)
    for edit in (False, True):
        gate.serve("/g/nomad-gate", env={"SSH_ORIGINAL_COMMAND": "nomad-gate version"}, run_=run_,
                   slot_=lambda: os.open(os.devnull, os.O_RDONLY), check_=lambda me: None, edit=edit)
    assert [d["edit"] for d in out] == [False, True]
    seen = []
    monkeypatch.setattr(gate, "serve", lambda me, **kw: seen.append(kw) or 0)
    assert gate.main(["serve", "--edit"], me="/g/nomad-gate") == 0 and seen == [{"edit": True}]
    assert gate.main(["serve"], me="/g/nomad-gate") == 0 and seen[-1] == {"edit": False}
    assert gate.main(["serve", "--other"], me="/g/nomad-gate") == 2
    assert remote.forced_command("/usr/local/sw/nomad/bin/nomad-gate", edit=True).endswith("serve --edit")
    assert remote.forced_command("/usr/local/sw/nomad/bin/nomad-gate").endswith("serve")


def test_not_over_a_text_changed_since(me):
    p = me / "job.sh"
    p.write_text(OLD)
    with pytest.raises(gate.Refused, match="changed since you opened it"):
        gate.save(str(p), NEW, body_sha=_h(NEW), old_sha=_h("#!/bin/bash\nsomething else\n"))
    assert p.read_text() == OLD and [x for x in os.listdir(me) if x.startswith(".")] == []
    with pytest.raises(gate.Refused, match="didn't arrive whole"):
        gate.save(str(p), NEW, body_sha=_h(NEW + "x"), old_sha=_h(OLD))


def test_a_new_script_never_over_one_there(me):
    p = me / "new.sh"
    d = gate.save(str(p), NEW, body_sha=_h(NEW), new=True)
    assert d["ok"] and d["new"] and d["kept"] is None and p.read_text() == NEW
    assert stat.S_IMODE(p.stat().st_mode) == 0o600                  # theirs alone (the gate can't know their umask)
    assert any("yours alone" in n for n in d["notes"])
    with pytest.raises(gate.Refused, match="is there already"):
        gate.save(str(p), OLD, body_sha=_h(OLD), new=True)
    assert p.read_text() == NEW


def test_what_is_never_written(me, tmp_path):
    (me / "real.sh").write_text(OLD)
    os.symlink(me / "real.sh", me / "link.sh")
    with pytest.raises(gate.Refused, match="is a link"):
        gate.save(str(me / "link.sh"), NEW, body_sha=_h(NEW), old_sha=_h(OLD))
    for path, why in ((str(tmp_path / "home" / ".bashrc"), "hidden"), (str(me / ".x" / "j.sh"), "hidden"),
                      ("relative/j.sh", "full path")):
        with pytest.raises(gate.Refused, match=why):
            gate.save(path, NEW, body_sha=_h(NEW), new=True)
    for text, why in (("echo no shell line\n", "begins with #!"), ("#!/bin/bash\n\x00\n", "NUL")):
        with pytest.raises(gate.Refused, match=why):
            gate.save(str(me / "j.sh"), text, body_sha=_h(text), new=True)
    big = "#!/bin/bash\n" + "#" * (gate.MAX_SCRIPT + 1)
    with pytest.raises(gate.Refused, match="over 256 KB"):
        gate.save(str(me / "big.sh"), big, body_sha=_h(big), new=True)
    if os.getuid() == 0:
        (me / "theirs.sh").write_text(OLD)
        os.chown(me / "theirs.sh", 4321, -1)
        with pytest.raises(gate.Refused, match="isn't yours"):
            gate.save(str(me / "theirs.sh"), NEW, body_sha=_h(NEW), old_sha=_h(OLD))
    assert (me / "real.sh").read_text() == OLD


def test_the_last_versions_are_kept(me, monkeypatch):
    monkeypatch.setattr(gate, "KEEP_VERSIONS", 3)
    p = me / "job.sh"
    p.write_text(OLD)
    text = OLD
    for i in range(5):
        new = f"#!/bin/bash\necho {i}\n"
        d = gate.save(str(p), new, body_sha=_h(new), old_sha=_h(text))
        assert d["ok"]
        text = new
        import time as _t
        _t.sleep(1.01)                                   # one id per second
    kept = gate.versions(str(p))
    assert len(kept) == 3 and gate.recall(str(p), kept[0]["id"]) == "#!/bin/bash\necho 3\n"
    with pytest.raises(gate.Refused, match="no version"):
        gate.recall(str(p), "20200101T000000-0123456789ab")


def test_save_through_serve_and_the_hub(me, monkeypatch):
    assert gate.parse(["save", "--script", "/x/j.sh", "--new", "--body-sha256", "a" * 64])[0] == "save"
    for words, why in ((["save", "--script", "/x/j.sh", "--body-sha256", "a" * 64], "--sha256"),
                       (["save", "--script", "/x/j.sh", "--new", "--sha256", "b" * 64, "--body-sha256", "a" * 64],
                        "--sha256")):
        with pytest.raises(gate.Refused, match=why):
            gate.parse(words)
    # serve reads the body (at most 256 KB of UTF-8) and hands it to the person's run
    seen = {}

    def run_(argv, **kw):
        seen.setdefault("calls", []).append((argv, kw))
        if argv[1:3] == ["-n", "-l"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, '{"ok": true, "sha256": "%s", "kept": null}\n' % ("c" * 64), "")
    monkeypatch.setattr(gate, "person", lambda name: type("P", (), {"pw_name": "jdoe"})())
    monkeypatch.setattr(gate, "_sudo", lambda: "/usr/bin/sudo")
    logged = []
    monkeypatch.setattr(gate, "_log", logged.append)
    env = {"SSH_ORIGINAL_COMMAND": f"nomad-gate save --user jdoe --script /x/j.sh --new --body-sha256 {_h(NEW)}"}
    code = gate.serve("/g/nomad-gate", env=env, run_=run_, slot_=lambda: os.open(os.devnull, os.O_RDONLY),
                      check_=lambda me: None, stdin=io.BytesIO(NEW.encode()), edit=True)
    assert code == 0
    argv, kw = seen["calls"][-1]
    assert argv[-6:] == ["save", "--script", "/x/j.sh", "--new", "--body-sha256", _h(NEW)] and kw["input"] == NEW
    assert any("save" in m and "=> cccccccccccc" in m for m in logged)
    big = gate.serve("/g/nomad-gate", env=env, run_=run_, slot_=lambda: os.open(os.devnull, os.O_RDONLY),
                     check_=lambda me: None, stdin=io.BytesIO(b"#" * (gate.MAX_SCRIPT + 1)), edit=True)
    assert big == 2
    # the hub: the body on the connection's standard input, its sha256 with it
    sent = {}

    def ssh(argv, **kw):
        sent.update(argv=argv, kw=kw)
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}\n', "")
    monkeypatch.setattr(remote, "settings", lambda site, hub=None: {"name": "c", "key": "/k", "user": "installer",
                                                                     "host": "c.example.edu"})
    d = remote.call({"name": "c"}, "save", "jdoe", run_=ssh, script="/x/j.sh", new=True, body=NEW)
    assert d["ok"] and "-n" not in sent["argv"] and sent["kw"]["input"] == NEW
    assert f"--body-sha256 {_h(NEW)}" in sent["argv"][-1]
    d = remote.call({"name": "c"}, "jobs", "jdoe", run_=ssh)
    assert "-n" in sent["argv"] and sent["kw"]["stdin"] == subprocess.DEVNULL
    d = remote.ask({"name": "c"}, ["jobs", "--user", "jdoe"], run_=ssh, body="x")
    assert not d["ok"] and "save alone" in d["error"]
    # editing is off unless the site says edit = true
    assert not remote.editing({"name": "c", "submit": "gate"})
    assert not remote.editing({"name": "c", "submit": "gate", "edit": "yes"})
    assert not remote.editing({"name": "c", "edit": True})                     # no gate
    assert remote.editing({"name": "c", "submit": "gate", "edit": True})


# ── pressure stall information (PSI) ────────────────────────────────────────

from nomad.collectors import pressure  # noqa: E402

CPU = "some avg10=1.53 avg60=0.87 avg300=0.40 total=4318923\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
MEM = "some avg10=12.00 avg60=8.50 avg300=3.25 total=99\nfull avg10=4.00 avg60=2.00 avg300=1.00 total=42\n"


def test_pressure_is_read_where_it_is_on(tmp_path):
    p = pressure.parse(CPU)
    assert p["some"] == {"avg10": 1.53, "avg60": 0.87, "avg300": 0.40, "total": 4318923}
    assert pressure.parse("some avg10=x\nnonsense\n") == {}
    root = tmp_path / "pressure"
    root.mkdir()
    (root / "cpu").write_text(CPU)
    (root / "memory").write_text(MEM)
    found = pressure.read(str(root))                       # no io file: the others still
    assert set(found) == {"cpu", "memory"} and found["memory"]["full"]["avg60"] == 2.0
    c = pressure.PressureCollector({"root": str(root)}, str(tmp_path / "nomad.db"))
    data = c.collect()
    assert data[0]["type"] == "pressure_status" and data[0]["available"] == 1 and c.note is None
    c.store(data)
    conn = sqlite3.connect(tmp_path / "nomad.db")
    got = conn.execute("SELECT resource, some_avg10, full_avg60 FROM pressure ORDER BY resource").fetchall()
    assert got == [("cpu", 1.53, 0.0), ("memory", 12.0, 2.0)]
    assert conn.execute("SELECT available FROM pressure_status").fetchone() == (1,)


def test_pressure_off_says_so_and_how_to_turn_it_on(tmp_path):
    c = pressure.PressureCollector({"root": str(tmp_path / "nowhere")}, str(tmp_path / "nomad.db"))
    data = c.collect()
    assert len(data) == 1 and data[0]["available"] == 0 and "psi=1" in c.note
    c.store(data)
    c.store(data)                                           # one status per host, kept current
    conn = sqlite3.connect(tmp_path / "nomad.db")
    assert conn.execute("SELECT COUNT(*), MAX(why) FROM pressure_status").fetchone() == (1, pressure.OFF)
    assert conn.execute("SELECT COUNT(*) FROM pressure").fetchone() == (0,)
    from nomad.collectors import plan
    assert "pressure" in plan.NAMES
