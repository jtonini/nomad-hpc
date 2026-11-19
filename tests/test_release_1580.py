# SPDX-License-Identifier: AGPL-3.0-or-later
"""1.58.0: copying a file or a whole folder from one machine to another, by
the hub, through each machine's gate as the person (nomad.submit.copies);
the gate's tree; lab workstations as machines with the gate alone."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from nomad.submit import copies as C
from nomad.submit import files as F
from nomad.submit import gate, remote

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def gates(monkeypatch, tmp_path):
    """Two machines, each a folder here, asked as the gate is asked (the
    gate's own code, in this process, as whoever runs the tests)."""
    monkeypatch.setattr(gate, "MIN_UID", 0)
    monkeypatch.setattr(gate, "_home", lambda: str(tmp_path))
    a, b = tmp_path / "one", tmp_path / "two"
    a.mkdir()
    b.mkdir()
    asked = []

    def machine(name):
        def call(op, data=None, **options):
            if data is not None:
                options["piece_sha256"] = gate._sha(data)          # as remote.call adds it
            words = remote.request(op, None, **options)
            if data is not None:
                monkeypatch.setattr(gate.sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                gate.run(words, str(tmp_path / "bin" / "nomad-gate"))
            asked.append((name, op, options.get("offset")))
            return json.loads(out.getvalue().strip().splitlines()[-1])
        return call
    return machine("one"), machine("two"), a, b, asked


def _store(tmp_path):
    return C.Store(str(tmp_path / "copies.db"))


def _run(store, cid, frm, to, **kw):
    kw.setdefault("max_bytes", 10 ** 12)
    kw.setdefault("max_files", 20000)
    return C.Copier(store, cid, frm, to, sleep=lambda s: None, **kw).run()


def _tree(top: Path) -> dict:
    return {str(p.relative_to(top)): (p.read_bytes() if p.is_file() else None) for p in sorted(top.rglob("*"))}


def _project(a: Path) -> Path:
    p = a / "project"
    (p / "inputs" / "deep").mkdir(parents=True)
    (p / "results").mkdir()
    (p / "run.sh").write_text("#!/bin/bash\necho hi\n")
    (p / "empty.txt").write_bytes(b"")
    (p / "inputs" / "big.bin").write_bytes(os.urandom(20 * 1024 * 1024 + 123))     # two puts, three gets
    (p / "inputs" / "deep" / "x.csv").write_text("a,b\n1,2\n")
    (p / ".hidden").write_text("not copied")
    (p / "link").symlink_to("run.sh")
    return p


# ── the gate's tree, and its mkdir saying what is there ─────────────────────

def test_tree_lists_a_folder_as_the_person_sees_it(gates, tmp_path):
    one, _two, a, _b, _ = gates
    _project(a)
    doc = one("tree", dir=str(a / "project"))
    assert doc["ok"]
    paths = [e["path"] for e in doc["entries"]]
    # a folder before what is in it; hidden files and links left out, and counted
    assert paths.index("inputs") < paths.index("inputs/deep") < paths.index("inputs/deep/x.csv")
    assert ".hidden" not in paths and "link" not in paths
    assert doc["skipped"]["hidden"] == 1 and doc["skipped"]["links"] == 1
    assert doc["files"] == 4 and doc["folders"] == 3 and not doc["more"]
    assert doc["bytes"] == sum((a / "project" / p).stat().st_size for p in paths if (a / "project" / p).is_file())
    few = one("tree", dir=str(a / "project"), most=2)
    assert few["more"] and len(few["entries"]) == 2
    assert not one("tree", dir=str(a / "project" / "run.sh"))["ok"]
    assert "tree" in gate.EDITS                      # served only with serve --edit, as moving files is


def test_mkdir_says_a_folder_is_there_already(gates):
    one, _two, a, _b, _ = gates
    (a / "f").mkdir()
    (a / "g").write_text("x")
    doc = one("mkdir", dir=str(a / "f"))
    assert not doc["ok"] and doc["exists"] and doc["folder"]
    doc = one("mkdir", dir=str(a / "g"))
    assert not doc["ok"] and doc["exists"] and not doc["folder"]


# ── a copy ─────────────────────────────────────────────────────────────────

def test_a_folder_copied_whole(gates, tmp_path):
    one, two, a, b, asked = gates
    p = _project(a)
    s = _store(tmp_path)
    cid = s.add("kim", "one", str(p), "folder", "two", str(b))
    c = _run(s, cid, one, two)
    assert c["state"] == "done", c
    want = {k: v for k, v in _tree(p).items() if k not in (".hidden", "link")}
    assert _tree(b / "project") == want
    assert c["files_total"] == 4 and c["files_done"] == 4 and c["folders_total"] == 4
    assert c["bytes_done"] == c["bytes_total"] == sum(len(v) for v in want.values() if v is not None)
    assert c["left_out"] == {"hidden": 1, "links": 1}
    # nothing of the copy's left beside the files
    assert not [x for x in (b / "project").rglob(".*nomad-upload-*")]
    # the big file: 16 MiB a put (two gets each), not a put an 8 MiB piece
    puts = [x for x in asked if x[0] == "two" and x[1] == "put"]
    assert len(puts) == 4 + 1                    # run.sh, empty.txt, x.csv, and big.bin's two


def test_a_file_copied(gates, tmp_path):
    one, two, a, b, _ = gates
    (a / "notes.txt").write_text("hello")
    s = _store(tmp_path)
    c = _run(s, s.add("kim", "one", str(a / "notes.txt"), "file", "two", str(b)), one, two)
    assert c["state"] == "done" and (b / "notes.txt").read_text() == "hello"


@pytest.mark.parametrize("how, there, kept", [
    ("skip", {"notes.txt": "old"}, {"notes.txt": "old"}),
    ("replace", {"notes.txt": "old"}, {"notes.txt": "new"}),
    ("keep", {"notes.txt": "old", "notes (2).txt": "older"}, {"notes.txt": "old", "notes (2).txt": "older",
                                                                "notes (3).txt": "new"}),
])
def test_a_file_there_already(gates, tmp_path, how, there, kept):
    one, two, a, b, _ = gates
    (a / "notes.txt").write_text("new")
    for k, v in there.items():
        (b / k).write_text(v)
    s = _store(tmp_path)
    c = _run(s, s.add("kim", "one", str(a / "notes.txt"), "file", "two", str(b), how), one, two)
    assert c["state"] == "done"
    assert {p.name: p.read_text() for p in b.iterdir()} == kept
    assert c["files_skipped"] == (1 if how == "skip" else 0)


def test_a_folder_there_already_is_gone_into_or_kept_beside(gates, tmp_path):
    one, two, a, b, _ = gates
    p = _project(a)
    (b / "project").mkdir()
    (b / "project" / "run.sh").write_text("mine")
    s = _store(tmp_path)
    c = _run(s, s.add("kim", "one", str(p), "folder", "two", str(b), "skip"), one, two)
    assert c["state"] == "done" and (b / "project" / "run.sh").read_text() == "mine"
    assert (b / "project" / "inputs" / "deep" / "x.csv").exists() and c["files_skipped"] == 1
    c = _run(s, s.add("kim", "one", str(p), "folder", "two", str(b), "keep"), one, two)
    assert c["state"] == "done" and c["to_name"] == "project (2)"
    assert (b / "project (2)" / "run.sh").read_text().startswith("#!/bin/bash")
    # a file by the folder's name: only "keep both" copies it
    (b / "data").write_text("x")
    (a / "data").mkdir()
    (a / "data" / "f").write_text("y")
    c = _run(s, s.add("kim", "one", str(a / "data"), "folder", "two", str(b), "skip"), one, two)
    assert c["state"] == "failed" and "isn't a folder" in c["error"]
    c = _run(s, s.add("kim", "one", str(a / "data"), "folder", "two", str(b), "keep"), one, two)
    assert c["state"] == "done" and (b / "data (2)" / "f").read_text() == "y"


def test_limits_and_room(gates, tmp_path, monkeypatch):
    one, two, a, b, _ = gates
    p = _project(a)
    s = _store(tmp_path)
    c = _run(s, s.add("kim", "one", str(p), "folder", "two", str(b)), one, two, max_files=3)
    assert c["state"] == "failed" and "rsync" in c["error"] and "more than 3" in c["error"]
    c = _run(s, s.add("kim", "one", str(p), "folder", "two", str(b)), one, two, max_bytes=1000)
    assert c["state"] == "failed" and "rsync" in c["error"]
    monkeypatch.setattr(gate, "_room", lambda dfd: 5000)
    c = _run(s, s.add("kim", "one", str(p), "folder", "two", str(b)), one, two)
    assert c["state"] == "failed" and "not enough room" in c["error"]
    assert not (b / "project").exists()          # nothing started


def test_a_busy_machine_is_waited_for(gates, tmp_path):
    one, two, a, b, _ = gates
    (a / "n.txt").write_text("x")
    busy = {"left": 2}

    def two_busy(op, data=None, **o):
        if op == "put" and busy["left"]:
            busy["left"] -= 1
            return {"ok": False, "busy": True, "error": "busy: 2 files are moving here"}
        return two(op, data=data, **o)
    s = _store(tmp_path)
    c = _run(s, s.add("kim", "one", str(a / "n.txt"), "file", "two", str(b)), one, two_busy)
    assert c["state"] == "done" and busy["left"] == 0


def test_a_machine_that_goes_away_and_the_copy_started_again(gates, tmp_path):
    one, two, a, b, asked = gates
    p = _project(a)
    down = {"after": 3, "puts": 0}

    def two_goes(op, data=None, **o):
        if op == "put":
            down["puts"] += 1
            if down["puts"] > down["after"]:
                return {"ok": False, "error": "couldn't reach nomad@two: Connection timed out", "where": "ssh"}
        return two(op, data=data, **o)
    s = _store(tmp_path)
    cid = s.add("kim", "one", str(p), "folder", "two", str(b))
    waits = []
    c = C.Copier(s, cid, one, two_goes, max_bytes=10 ** 12, max_files=100, sleep=waits.append).run()
    assert c["state"] == "failed" and "doesn't answer" in c["error"] and "goes on from where it stopped" in c["error"]
    assert waits == list(C.AGAIN_WAITS)
    done_before = c["files_done"]
    assert 0 < done_before < 4
    assert s.again(cid, "kim") and not s.again(cid, "pat")       # theirs alone
    asked.clear()
    c = _run(s, cid, one, two)
    assert c["state"] == "done" and c["files_done"] == 4
    assert _tree(b / "project") == {k: v for k, v in _tree(p).items() if k not in (".hidden", "link")}
    # what was copied wasn't copied again
    assert len([x for x in asked if x[1] == "put"]) < 5


def test_cancelled_between_pieces(gates, tmp_path):
    one, two, a, b, _ = gates
    (a / "big.bin").write_bytes(os.urandom(40 * 1024 * 1024))
    s = _store(tmp_path)
    cid = s.add("kim", "one", str(a / "big.bin"), "file", "two", str(b))

    def two_then_stop(op, data=None, **o):
        doc = two(op, data=data, **o)
        if op == "put":
            assert s.cancel(cid, "kim") == "cancelling"
        return doc
    c = _run(s, cid, one, two_then_stop)
    assert c["state"] == "cancelled"
    assert list(b.iterdir()) == []                    # what was sent of it removed
    assert s.cancel(cid, "kim") is None              # ended
    queued = s.add("kim", "one", str(a / "big.bin"), "file", "two", str(b))
    assert s.cancel(queued, "pat") is None and s.cancel(queued, "kim") == "cancelled"


def test_after_a_restart_it_goes_on_without_a_second_copy(gates, tmp_path):
    one, two, a, b, _ = gates
    (a / "n.txt").write_text("x" * 100)
    s = _store(tmp_path)
    cid = s.add("kim", "one", str(a / "n.txt"), "file", "two", str(b), "keep")
    assert _run(s, cid, one, two)["state"] == "done"
    # as if the Console stopped after the file was in place, before it was marked
    s.set(cid, state="copying", ended=None)
    s.item(cid, 0, state="copying")
    assert s.after_restart() == 1 and s.get(cid)["state"] == "queued"
    c = _run(s, cid, one, two)
    assert c["state"] == "done" and sorted(p.name for p in b.iterdir()) == ["n.txt"]


def test_a_file_that_changes_meanwhile(gates, tmp_path):
    one, two, a, b, _ = gates
    f = a / "grows.bin"
    f.write_bytes(os.urandom(10 * 1024 * 1024))

    def one_changing(op, **o):
        doc = one(op, **o)
        if op == "get" and o.get("offset") == 0 and o.get("length"):
            with open(f, "ab") as fh:
                fh.write(b"more")
        return doc
    s = _store(tmp_path)
    c = _run(s, s.add("kim", "one", str(f), "file", "two", str(b)), one_changing, two)
    assert c["state"] == "done" and c["files_failed"] == 1
    assert s.problems(c["id"])[0]["why"].startswith("it changed")
    assert list(b.iterdir()) == []


def test_names_kept_beside():
    assert C.other_name("data.csv", 2) == "data (2).csv"
    assert C.other_name("run", 3) == "run (3)"
    assert C.other_name("out.tar.gz", 2) == "out (2).tar.gz"
    assert C.other_name("Makefile", 2) == "Makefile (2)"


# ── the runner ─────────────────────────────────────────────────────────────

def test_the_runner_one_a_person_and_says_when_one_ends(gates, tmp_path):
    one, two, a, b, _ = gates
    for n in ("a", "b", "c"):
        (a / f"{n}.txt").write_text(n)
    s = _store(tmp_path)
    ids = [s.add("kim", "one", str(a / "a.txt"), "file", "two", str(b)),
           s.add("kim", "one", str(a / "b.txt"), "file", "two", str(b)),
           s.add("pat", "one", str(a / "c.txt"), "file", "two", str(b))]
    ended = []
    r = C.Runner(s, lambda c: (one, two), lambda: (10 ** 12, 100), at_once=2, ended=ended.append,
                 sleep=lambda x: None)
    first = r.step()
    assert first == [ids[0], ids[2]]                 # kim's second waits for kim's first
    r.wait()
    assert sorted(c["id"] for c in ended) == sorted(ids)
    assert all(s.get(i)["state"] == "done" for i in ids)
    assert {p.name for p in b.iterdir()} == {"a.txt", "b.txt", "c.txt"}


def test_the_runner_a_machine_no_longer_there(tmp_path):
    s = _store(tmp_path)
    cid = s.add("kim", "gone", "/x/y", "file", "two", "/z")

    def calls(c):
        raise C.CopyError("gone isn't one of the machines any more")
    r = C.Runner(s, calls, lambda: (1, 1))
    r.step()
    r.wait()
    assert s.get(cid)["state"] == "failed" and "any more" in s.get(cid)["error"]


def test_ended_copies_are_kept_a_month(tmp_path):
    s = _store(tmp_path)
    old = s.add("kim", "one", "/a/b", "file", "two", "/c")
    s.set(old, state="done", ended="2020-01-01T00:00:00")
    new = s.add("kim", "one", "/a/b", "file", "two", "/c")
    assert s.tidy() == 1 and s.get(old) is None and s.get(new)["state"] == "queued"
    assert s.going("kim") == 1 and [c["id"] for c in s.mine("kim")] == [new]


# ── machines with the gate alone ───────────────────────────────────────────

def test_machines_in_the_settings():
    s = F.settings({"console": {"files": {
        "copy_max_gb": 50, "copies_at_once": 3,
        "machines": [
            {"name": "ws-kim", "host": "ws-kim.example.edu", "folders": ["~", "/scratch/{user}", "/shared"]},
            {"name": "open-box", "host": "ob.example.edu", "groups": ["*"], "gate_jump": "zed@jump.example.edu"},
            {"name": "pat-box", "host": "pb", "groups": "pat$", "gate_user": "gatekeeper", "workstation": "pb-01"},
            {"name": "bad name", "host": "x"},
            {"name": "ws-kim", "host": "again"},
            {"name": "odd", "host": "o", "colour": "blue"},
        ]}}})
    names = [m["name"] for m in s.machines]
    assert names == ["ws-kim", "open-box", "pat-box", "odd"]
    kim, ob, pb, _odd = s.machines
    assert kim["submit"] == "gate" and kim["edit"] is True and kim["gate_user"] == "nomad"
    assert kim["groups"] is None and kim["workstation"] == "ws-kim"
    assert remote.mode(kim) == "gate" and remote.editing(kim)
    assert ob["gate_jump"] == "zed@jump.example.edu" and ob["groups"] == ("*",)
    assert pb["groups"] == ("pat$",) and pb["gate_user"] == "gatekeeper" and pb["workstation"] == "pb-01"
    assert s.sites["ws-kim"]["folders"] == ("~", "/scratch/{user}", "/shared")
    assert F.folders_for(s, "ws-kim", "kim", "/home/kim")[1:] == ["/scratch/kim", "/shared"] or \
        "/scratch/kim" in str(F.folders_for(s, "ws-kim", "kim", "/home/kim"))
    assert s.copy_max_bytes == 50 * 1000 ** 3 and s.copies_at_once == 3 and s.copy is True
    assert any("bad name" not in p and "#4" in p for p in s.problems)
    assert any("twice" in p for p in s.problems) and any("colour" in p for p in s.problems)


def test_who_may_use_a_machine():
    s = F.settings({"console": {"files": {"machines": [
        {"name": "kimlab", "host": "ws-a.example.edu"},
        {"name": "open-box", "host": "o1", "groups": ["*"]},
        {"name": "patbox", "host": "p1", "groups": ["pat$"]},
        {"name": "unknown", "host": "zz"}]}}})
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE workstation_state (hostname, department, timestamp)")
    db.executemany("INSERT INTO workstation_state VALUES (?, ?, ?)",
                   [("ws-a", "old$", "2026-01-01T00:00:00"), ("ws-a", "kim$", "2026-11-01T00:00:00")])
    labs = F.machine_labs(db, s.machines)
    assert labs == {"ws-a": "kim$"}                  # the lab nomad last saw it in
    see = lambda groups, staff=False: [m["name"] for m in F.machines_open_to(s, groups, staff, labs)]  # noqa: E731
    assert see(["kim$"]) == ["kimlab", "open-box"]
    assert see(["pat$"]) == ["open-box", "patbox"]
    assert see(["old$"]) == see([]) == ["open-box"]
    assert see([], staff=True) == ["kimlab", "open-box", "patbox", "unknown"]
    assert F.machine_labs(sqlite3.connect(":memory:"), s.machines) == {}     # no workstations collected


def test_the_gate_alone_checks_itself_without_nomad_beside(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    me = bin_ / "nomad-gate"
    me.write_text("#!/bin/sh\n")
    seen = []
    monkeypatch.setattr(gate, "unsafe", lambda p, owners, private=frozenset(): seen.append(p))
    monkeypatch.setattr(gate, "tree_unsafe", lambda top, owners: None, raising=False)
    gate.self_check(str(me))
    assert str(bin_ / "nomad") not in seen
    (bin_ / "nomad").write_text("#!/bin/sh\n")
    seen.clear()
    gate.self_check(str(me))
    assert str(bin_ / "nomad") in seen


def test_the_install_for_the_gate_alone():
    text = (ROOT / "scripts" / "install_nomad_for_users.sh").read_text()
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / "install_nomad_for_users.sh")]).returncode == 0
    assert "NOMAD_GATE_ONLY" in text and "${GATE_ONLY:+--no-deps}" in text
    assert "import nomad.submit.gate" in text
    assert "the gate alone comes with nomad-hpc 1.58.0 and later" in text


def test_the_gate_imports_with_nothing_but_the_standard_library():
    """The gate alone is installed without nomad's other libraries (--no-deps)."""
    # -S: no site-packages, so none of nomad's other libraries; -I: nothing of this environment's
    code = f"import sys; sys.path.insert(0, {str(ROOT)!r}); import nomad.submit.gate as g; print(g._version()['ok'])"
    r = subprocess.run([os.sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "True", r.stderr[-2000:]


def test_the_gate_alone_lets_the_hub_key_in_itself(tmp_path, monkeypatch, capsys):
    """No bin/nomad on a machine with the gate alone: nomad-gate install-key does it."""
    if subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode:
        pytest.skip("no ssh-keygen here")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "nomad-gate", "-f", str(tmp_path / "k")],
                   check=True)
    pub = (tmp_path / "k.pub").read_text().strip()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    d = tmp_path / "opt" / "nomad-gate" / "bin"
    d.mkdir(parents=True)
    me = d / "nomad-gate"
    me.write_text("#!/bin/sh\n")
    me.chmod(0o755)
    assert gate.main(["install-key", pub, "--from", "192.0.2.7", "--edit"], me=str(me)) == 0
    assert "dry run" in capsys.readouterr().out and not (home / ".ssh" / "authorized_keys").exists()
    assert gate.main(["install-key", pub, "--from", "192.0.2.7", "--edit", "--apply"], me=str(me)) == 0
    line = (home / ".ssh" / "authorized_keys").read_text().strip()
    assert 'from="192.0.2.7"' in line and f'command="{me} serve --edit"' in line and "restrict" in line
    assert gate.main(["install-key", pub, "--from", "192.0.2.7", "--edit", "--apply"], me=str(me)) == 0
    assert "already has this key" in capsys.readouterr().out
    assert gate.main(["install-key", "not a key", "--from", "192.0.2.7"], me=str(me)) == 1


def test_gate_test_on_the_lab_machines(tmp_path, monkeypatch):
    """nomad gate test --machines: each machine with the gate alone, as the person; no jobs asked there."""
    from click.testing import CliRunner

    from nomad.cli import cli
    toml = tmp_path / "nomad.toml"
    toml.write_text(f'[hub]\ngate_key = "{tmp_path}/id_gate"\n'
                    '[[hub.sites]]\nname = "c1"\nhost = "c1.example.edu"\nsubmit = "gate"\nedit = true\n'
                    '[[console.files.machines]]\nname = "ws-one"\nhost = "ws-one"\n'
                    '[[console.files.machines]]\nname = "ws-two"\nhost = "ws-two"\n')
    (tmp_path / "id_gate").write_text("k")
    asked = []

    def ask(site, words, hub=None, **kw):
        asked.append((site["name"], words[0]))
        if site["name"] == "ws-two":
            return {"ok": False, "error": "couldn't reach nomad@ws-two: Connection refused", "where": "ssh"}
        if words[0] == "version":
            user = words[2] if len(words) > 2 else None
            return {"ok": True, "gate": 1, "nomad": "1.58.0", "account": user or "nomad", "edit": True,
                    "home": "/home/kim"}
        if words[0] == "put-status":
            return {"ok": True, "have": 2}
        if words[0] == "put-cancel":
            return {"ok": True, "removed": True}
        if words[0] == "tree":
            return {"ok": True, "entries": [], "more": False}
        return {"ok": True}
    monkeypatch.setattr(remote, "ask", ask)
    r = CliRunner().invoke(cli, ["-c", str(toml), "gate", "test", "--site", "ws-one", "--user", "kim"])
    assert r.exit_code == 0, r.output
    assert "jobs" not in [op for _s, op in asked] and "tree" in [op for _s, op in asked]
    assert "a machine of [[console.files.machines]]" in r.output
    asked.clear()
    r = CliRunner().invoke(cli, ["-c", str(toml), "gate", "test", "--machines", "--user", "kim"])
    assert r.exit_code == 1 and "1 of 2 machine(s) ok; not: ws-two" in r.output
    assert {s for s, _op in asked} == {"ws-one", "ws-two"}
    r = CliRunner().invoke(cli, ["-c", str(toml), "gate", "test", "--user", "kim"])
    assert r.exit_code != 0 and "--machines" in r.output
    r = CliRunner().invoke(cli, ["-c", str(toml), "gate", "test", "--site", "nope"])
    assert r.exit_code != 0 and "console.files.machines" in r.output


def test_a_machines_lab_whatever_the_case_of_its_name():
    s = F.settings({"console": {"files": {"machines": [{"name": "Box", "host": "Box"}]}}})
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE workstation_state (hostname, department, timestamp)")
    db.execute("INSERT INTO workstation_state VALUES ('box', 'kim$', '2026-11-01T00:00:00')")
    assert F.machine_labs(db, s.machines) == {"Box": "kim$"}
