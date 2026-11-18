# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.55.0: moving files from the browser. The gate takes a file of the
person's in pieces, as them, beside its place until whole, checks it whole,
and puts it in place (never over a file unless asked); and gives back a
file they can read in pieces. The hub sends each piece as the bytes it is."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import stat
import subprocess
import time

import pytest

from nomad.submit import gate, remote

UP = "0123456789abcdef0123"


def _h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _out(capsys):
    lines = [x for x in capsys.readouterr().out.splitlines() if x.strip()]
    assert len(lines) == 1, lines
    return json.loads(lines[0])


@pytest.fixture
def me(monkeypatch, tmp_path):
    """Run as whoever runs the tests (root here: MIN_UID out of the way)."""
    monkeypatch.setattr(gate, "MIN_UID", 0)
    monkeypatch.setattr(gate, "_home", lambda: str(tmp_path))
    d = tmp_path / "data"
    d.mkdir(mode=0o750)
    os.chmod(d, 0o750)

    def call(*words, data: bytes | None = None):
        if data is not None:
            monkeypatch.setattr(gate.sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))
        return gate.run(list(words), str(tmp_path / "bin" / "nomad-gate"))
    return call, d


def _send(call, capsys, path, blob, piece=5, upload=UP, replace=False, start=0):
    """Send ``blob`` as ``path`` in pieces of ``piece`` bytes (from ``start``); the last answer."""
    at, doc = start, None
    while True:
        part = blob[at:at + piece]
        last = at + len(part) == len(blob)
        words = ["put", "--path", path, "--upload", upload, "--offset", str(at), "--total", str(len(blob)),
                 "--piece-sha256", _h(part)] + (["--sha256", _h(blob)] if last else []) + \
            (["--replace"] if replace else [])
        call(*words, data=part)
        doc = _out(capsys)
        if not doc["ok"] or last:
            return doc
        at += len(part)


def test_the_requests_are_checked():
    ok = ["put", "--path", "/home/kim/a.dat", "--upload", UP, "--offset", "0", "--total", "10",
          "--piece-sha256", "a" * 64]
    assert gate.parse(ok)[0] == "put"
    for words, why in ((ok[:-2], "needs --piece-sha256"),
                       (ok[:3] + ["--upload", "XYZ"] + ok[5:], "an upload's id"),
                       (ok[:5] + ["--offset", "11"] + ok[7:], "within --total"),
                       (ok[:5] + ["--offset", "01"] + ok[7:], "a number of bytes"),
                       (ok[:7] + ["--total", str(gate.MAX_FILE + 1)] + ok[9:], "within --total"),
                       (["get", "--path", "/home/kim/a.dat", "--length", str(gate.MAX_PIECE + 1)], "at most"),
                       (["get"], "needs --path"),
                       (["put-status", "--path", "/home/kim/a.dat"], "needs --upload")):
        with pytest.raises(gate.Refused, match=why):
            gate.parse(words)
    assert {"put", "put-status", "put-cancel", "get"} <= gate.EDITS            # the cluster's say: serve --edit
    assert gate.WITH_BYTES == {"put"} and "put" not in gate.WITH_BODY
    assert remote.request("put", "kim", path="/home/kim/a.dat", upload=UP, offset=0, total=10,
                          piece_sha256="a" * 64, replace=True)[-3:] == ["--piece-sha256", "a" * 64, "--replace"]


def test_a_file_in_pieces(me, capsys):
    call, d = me
    blob = os.urandom(23)
    doc = _send(call, capsys, str(d / "run1.dat"), blob)
    assert doc["ok"] and doc["done"] and doc["size"] == 23 and doc["sha256"] == _h(blob)
    assert (d / "run1.dat").read_bytes() == blob
    assert not [p for p in os.listdir(d) if "nomad-upload" in p]               # nothing left beside it
    # shared as its folder is: the folder lets its group in, not others; nobody else writes it
    assert doc["mode"] == "0640" and doc["reads"] == "group"
    assert stat.S_IMODE((d / "run1.dat").stat().st_mode) == 0o640
    os.chmod(d, 0o700)
    assert _send(call, capsys, str(d / "mine.dat"), b"x" * 7)["reads"] == "you"
    empty = _send(call, capsys, str(d / "empty.dat"), b"")
    assert empty["done"] and (d / "empty.dat").read_bytes() == b""


def test_never_over_a_file_unless_asked(me, capsys):
    call, d = me
    (d / "in.dat").write_bytes(b"old")
    os.chmod(d / "in.dat", 0o604)
    doc = _send(call, capsys, str(d / "in.dat"), b"new text here")
    assert not doc["ok"] and doc["exists"] is True and (d / "in.dat").read_bytes() == b"old"
    doc = _send(call, capsys, str(d / "in.dat"), b"new text here", replace=True)
    assert doc["ok"] and doc["replaced"] and (d / "in.dat").read_bytes() == b"new text here"
    assert stat.S_IMODE((d / "in.dat").stat().st_mode) == 0o604                   # its permissions kept
    os.symlink(d / "in.dat", d / "link.dat")
    doc = _send(call, capsys, str(d / "link.dat"), b"zz", replace=True)
    assert not doc["ok"] and "is a link" in doc["error"]
    (d / "sub").mkdir()
    doc = _send(call, capsys, str(d / "sub"), b"zz", replace=True)
    assert not doc["ok"] and "is a folder" in doc["error"]


def test_a_file_appearing_meanwhile_is_kept_and_asked_about(me, capsys):
    call, d = me
    blob = b"abcdefghij"
    call("put", "--path", str(d / "late.dat"), "--upload", UP, "--offset", "0", "--total", "10",
         "--piece-sha256", _h(blob[:6]), data=blob[:6])
    assert _out(capsys)["have"] == 6
    (d / "late.dat").write_bytes(b"theirs")                                         # made meanwhile
    doc = _send(call, capsys, str(d / "late.dat"), blob, piece=6, start=6)
    assert not doc["ok"] and doc["exists"] and doc["have"] == 10 and (d / "late.dat").read_bytes() == b"theirs"
    doc = _send(call, capsys, str(d / "late.dat"), blob, piece=6, start=6, replace=True)   # Replace: the last again
    assert doc["ok"] and (d / "late.dat").read_bytes() == blob


def test_a_break_costs_nothing(me, capsys):
    call, d = me
    blob = os.urandom(40)
    path = str(d / "big.dat")
    for at in (0, 10):
        part = blob[at:at + 10]
        call("put", "--path", path, "--upload", UP, "--offset", str(at), "--total", "40",
             "--piece-sha256", _h(part), data=part)
        assert _out(capsys)["have"] == at + 10
    call("put-status", "--path", path, "--upload", UP)
    st = _out(capsys)
    assert st["have"] == 20 and st["started"] and st["exists"] is False and st["there"] is None
    # a piece sent again (its answer lost) is written again; one past what is here is refused
    part = blob[10:20]
    call("put", "--path", path, "--upload", UP, "--offset", "10", "--total", "40", "--piece-sha256", _h(part),
         data=part)
    assert _out(capsys)["have"] == 20
    part = blob[30:40]
    call("put", "--path", path, "--upload", UP, "--offset", "30", "--total", "40", "--piece-sha256", _h(part),
         "--sha256", _h(blob), data=part)
    doc = _out(capsys)
    assert not doc["ok"] and doc["have"] == 20
    assert _send(call, capsys, path, blob, piece=10, start=20)["done"]
    assert (d / "big.dat").read_bytes() == blob


def test_what_didnt_arrive_whole(me, capsys):
    call, d = me
    path = str(d / "x.dat")
    call("put", "--path", path, "--upload", UP, "--offset", "0", "--total", "4", "--piece-sha256", _h(b"abcd"),
         "--sha256", _h(b"abcd"), data=b"abce")
    doc = _out(capsys)
    assert not doc["ok"] and "piece didn't arrive whole" in doc["error"]
    call("put", "--path", path, "--upload", UP, "--offset", "0", "--total", "4", "--piece-sha256", _h(b"abcd"),
         "--sha256", _h(b"zzzz"), data=b"abcd")
    doc = _out(capsys)
    assert not doc["ok"] and doc["have"] == 0 and "file didn't arrive whole" in doc["error"]
    assert os.listdir(d) == []                                               # removed: sent again from the start
    call("put", "--path", path, "--upload", UP, "--offset", "0", "--total", "4", "--piece-sha256", _h(b"ab"),
         data=b"ab")
    _out(capsys)
    call("put-cancel", "--path", path, "--upload", UP)
    assert _out(capsys)["removed"] is True and os.listdir(d) == []
    call("put-cancel", "--path", path, "--upload", UP)
    assert _out(capsys)["removed"] is False


def test_never_hidden(me, capsys, tmp_path):
    call, d = me
    (tmp_path / ".ssh").mkdir()
    for path in (str(tmp_path / ".ssh" / "authorized_keys"), str(d / ".bashrc"), "data/rel.dat", str(d) + "/"):
        doc = _send(call, capsys, path, b"x")
        assert not doc["ok"], path
    os.symlink(tmp_path / ".ssh", d / "keys")
    doc = _send(call, capsys, str(d / "keys" / "authorized_keys"), b"x")
    assert not doc["ok"] and "not a hidden folder" in doc["error"]
    assert os.listdir(tmp_path / ".ssh") == []


def test_left_a_week_is_tidied(me, capsys):
    call, d = me
    old = d / f".gone.dat.nomad-upload-{'f' * 16}"
    old.write_bytes(b"part")
    week = time.time() - 8 * 86400
    os.utime(old, (week, week))
    young = d / f".young.dat.nomad-upload-{'e' * 16}"
    young.write_bytes(b"part")
    other = d / ".notes"
    other.write_bytes(b"theirs")
    os.utime(other, (week, week))
    assert _send(call, capsys, str(d / "new.dat"), b"hello")["done"]
    assert not old.exists() and young.exists() and other.exists()


def test_taking_a_file_back(me, capsys, tmp_path):
    call, d = me
    blob = os.urandom(30)
    (d / "out.dat").write_bytes(blob)
    call("get", "--path", str(d / "out.dat"), "--length", "0")
    facts = _out(capsys)
    assert facts["size"] == 30 and facts["length"] == 0 and facts["data"] == "" and facts["mine"]
    got, at = b"", 0
    while at < 30:
        call("get", "--path", str(d / "out.dat"), "--offset", str(at), "--length", "8")
        doc = _out(capsys)
        piece = base64.b64decode(doc["data"])
        assert doc["sha256"] == _h(piece) and doc["version"] == facts["version"]
        got, at = got + piece, at + len(piece)
    assert got == blob
    call("get", "--path", str(d / "out.dat"), "--offset", "40")
    assert _out(capsys)["length"] == 0                                        # past its end: nothing
    (tmp_path / ".secret").write_bytes(b"key")
    os.symlink(tmp_path / ".secret", d / "innocent.dat")
    call("get", "--path", str(d / "innocent.dat"))
    assert "not a hidden file" in _out(capsys)["error"]
    os.mkfifo(d / "pipe")
    call("get", "--path", str(d / "pipe"))
    assert "isn't a file" in _out(capsys)["error"]


def _serving(monkeypatch, capsys):
    monkeypatch.setattr(gate, "person", lambda name: type("P", (), {"pw_name": "kim"})())
    monkeypatch.setattr(gate, "_sudo", lambda: "/usr/bin/sudo")
    logged = []
    monkeypatch.setattr(gate, "_log", logged.append)
    return logged


def test_serve_hands_the_piece_on_as_bytes(monkeypatch, capsys):
    logged = _serving(monkeypatch, capsys)
    seen = []

    def run_(argv, **kw):
        seen.append((argv, kw))
        if argv[1:3] == ["-n", "-l"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, b'{"ok": true, "have": 5, "done": false}\n', b"")
    piece = bytes(range(256)) * 2
    words = f"nomad-gate put --user kim --path /home/kim/a.dat --upload {UP} --offset 5 --total 900 " \
            f"--piece-sha256 {_h(piece)}"
    slot = lambda: os.open(os.devnull, os.O_RDONLY)            # noqa: E731
    code = gate.serve("/g/nomad-gate", env={"SSH_ORIGINAL_COMMAND": words}, run_=run_, slot_=slot,
                      check_=lambda me: None, stdin=io.BytesIO(piece), edit=True)
    assert code == 0 and _out(capsys)["have"] == 5
    argv, kw = seen[-1]
    assert kw["input"] == piece and "text" not in kw and argv[-2:] == ["--piece-sha256", _h(piece)]
    assert logged == []                                        # a piece in the middle: not logged
    code = gate.serve("/g/nomad-gate", env={"SSH_ORIGINAL_COMMAND": words.replace("--offset 5", "--offset 0")},
                      run_=run_, slot_=slot, check_=lambda me: None, stdin=io.BytesIO(piece), edit=True)
    assert code == 0 and len(logged) == 1 and "put" in logged[0]
    _out(capsys)
    # without serve --edit: refused, nothing run
    seen.clear()
    assert gate.serve("/g/nomad-gate", env={"SSH_ORIGINAL_COMMAND": words}, run_=run_, slot_=slot,
                      check_=lambda me: None, stdin=io.BytesIO(piece)) == 2 and seen == []
    assert "isn't turned on" in _out(capsys)["error"]
    # more than a piece: refused before anything runs
    assert gate.serve("/g/nomad-gate", env={"SSH_ORIGINAL_COMMAND": words}, run_=run_, slot_=slot,
                      check_=lambda me: None, stdin=io.BytesIO(b"x" * (gate.MAX_PIECE + 1)), edit=True) == 2
    assert "at most 16 MB" in _out(capsys)["error"] and seen == []


def test_files_move_a_few_at_once(monkeypatch, capsys, tmp_path):
    _serving(monkeypatch, capsys)
    monkeypatch.setattr(gate, "WAIT_FOR_FILES", 0)
    monkeypatch.setattr(gate, "state_dir", lambda: str(tmp_path))
    held = [gate._slot(str(tmp_path), kind="files", n=gate.FILES_HERE, wait=0) for _ in range(gate.FILES_HERE)]
    assert all(h is not None for h in held)
    try:
        code = gate.serve("/g/nomad-gate", env={"SSH_ORIGINAL_COMMAND": "nomad-gate get --user kim --path /h/a"},
                          run_=lambda *a, **k: pytest.fail("ran"), check_=lambda me: None, edit=True)
        doc = _out(capsys)
        assert code == 4 and doc["busy"] is True
    finally:
        for h in held:
            os.close(h)


def test_the_hub_sends_a_piece_as_bytes(monkeypatch):
    sent = {}

    def ssh(argv, **kw):
        sent.update(argv=argv, kw=kw)
        return subprocess.CompletedProcess(argv, 0, b'{"ok": true, "have": 3}\n', b"")
    monkeypatch.setattr(remote, "settings", lambda site, hub=None: {"name": "c", "key": "/k", "user": "installer",
                                                                     "host": "c.example.edu"})
    d = remote.call({"name": "c"}, "put", "kim", run_=ssh, data=b"\x00\xff\x10", path="/home/kim/a.dat", upload=UP,
                    offset=0, total=3, sha256=_h(b"\x00\xff\x10"))
    assert d == {"ok": True, "have": 3}
    assert sent["kw"]["input"] == b"\x00\xff\x10" and "text" not in sent["kw"] and "-n" not in sent["argv"]
    assert f"--piece-sha256 {_h(bytes([0, 255, 16]))}" in sent["argv"][-1]
    assert not remote.ask({"name": "c"}, ["put", "--path", "/a/b", "--upload", UP, "--offset", "0", "--total", "1",
                                          "--piece-sha256", "a" * 64])["ok"]          # a put without its piece
    assert "goes with put alone" in remote.ask({"name": "c"}, ["jobs"], data=b"x")["error"]


# ── the hub's side: nomad.submit.files ──────────────────────────────────────

def test_the_settings_are_the_sites():
    from nomad.submit import files as F
    s = F.settings({})
    assert s.open == "submit" and s.max_mb == 2048 and s.piece == 8 * 1024 * 1024 and s.folders == ("~",)
    assert not s.problems and not s.only_in_folders
    s = F.settings({"console": {"files": {"open": "everyone", "max_mb": 500, "folders": ["~", "/scratch/{user}"],
                                          "sites": {"c1": {"folders": ["/work/{user}"], "login": "c1.example.edu"}}}}})
    assert s.max_bytes == 500 * 1024 * 1024 and s.open == "everyone"
    assert F.folders_for(s, "c2", "kim", "/home/kim") == ["/home/kim", "/scratch/kim"]
    assert F.folders_for(s, "c1", "kim", "/home/kim") == ["/work/kim"]
    assert F.login_of(s, {"name": "c1", "host": "gate.example.edu"}) == "c1.example.edu"
    assert F.login_of(s, {"name": "c2", "host": "gate.example.edu"}) == "gate.example.edu"
    bad = F.settings({"console": {"files": {"open": "maybe", "max_mb": 0, "piece_mb": 99, "folders": ["rel", "/a/.ssh"],
                                            "only_in_folders": "yes", "sites": {"c1": {"login": "not a host"}}}}})
    assert len(bad.problems) == 7 and bad.open == "submit" and bad.max_mb == 2048 and bad.folders == ("~",)
    assert F.opens_to(F.settings({}), "viewer", "staff") is False
    assert F.opens_to(F.settings({}), "viewer", "everyone") is True and F.opens_to(F.settings({}), "operator", "staff")
    assert F.opens_to(F.settings({"console": {"files": {"open": "off"}}}), "admin", "everyone") is False


def test_paths_as_the_gate_takes_them():
    from nomad.submit import files as F
    s = F.settings({"console": {"files": {"folders": ["~", "/scratch/{user}"], "only_in_folders": True}}})
    assert F.check_path(s, "c", "kim", "/home/kim", "/home/kim/run/a.dat") == "/home/kim/run/a.dat"
    assert F.check_path(s, "c", "kim", "/home/kim", "/scratch/kim", folder=True) == "/scratch/kim"
    for bad, why in (("/home/kim/.ssh/id", "hidden"), ("/home/kim/../pat/a", "without . or .."), ("rel", "full path"),
                     ("/home/pat/a.dat", "only in /home/kim, /scratch/kim"), ("/home/kimberly/a", "only in")):
        with pytest.raises(F.FilesError, match=why):
            F.check_path(s, "c", "kim", "/home/kim", bad)
    assert F.check_path(F.settings({}), "c", "kim", "/home/kim", "/home/pat/shared/a.dat")   # their permissions say


def test_what_to_type_for_big_files():
    from nomad.submit import files as F
    s = F.settings({"console": {"files": {"sites": {"c1": {"login": "c1.example.edu"}}}}})
    got = F.commands(s, {"name": "c1"}, "kim", "/home/kim/run 1/", "my data.csv")
    assert got["put_rsync"] == "rsync -av --partial --progress 'my data.csv' 'kim@c1.example.edu:/home/kim/run 1/'"
    assert got["get_scp"] == "scp 'kim@c1.example.edu:/home/kim/run 1/my data.csv' ."
    plain = F.commands(s, {"name": "c1"}, "kim", "/home/kim", "-x.dat")
    assert plain["put_scp"] == "scp ./-x.dat kim@c1.example.edu:/home/kim/" and "get_rsync" in plain


def test_taking_back_in_pieces_checked():
    from nomad.submit import files as F
    blob = os.urandom(20)
    version = ["1:2:20:3"]

    def call(op, path, offset=0, length=0):
        data = blob[offset:offset + length]
        return {"ok": True, "size": 20, "version": version[0], "length": len(data), "sha256": _h(data),
                "data": base64.b64encode(data).decode()}
    f = F.facts(call, "/home/kim/a.dat")
    assert f["size"] == 20
    assert b"".join(F.pieces(call, "/home/kim/a.dat", 20, f["version"], 8)) == blob
    it = F.pieces(call, "/home/kim/a.dat", 20, f["version"], 8)
    next(it)
    version[0] = "1:2:21:4"                                          # changed meanwhile
    with pytest.raises(F.Changed):
        next(it)
    with pytest.raises(F.FilesError, match="Permission denied"):
        F.facts(lambda op, **k: {"ok": False, "error": "/x: Permission denied"}, "/x")
    assert F.upload_id("kim", "c1", "/home/kim/a.dat", 20, 1700000000000) == hashlib.sha256(
        b"kim|c1|/home/kim/a.dat|20|1700000000000").hexdigest()[:24]


def test_the_gate_test_moves_a_file_leaving_nothing(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from nomad.cli import cli
    toml = tmp_path / "nomad.toml"
    toml.write_text(f'[hub]\ngate_key = "{tmp_path}/id_gate"\n[[hub.sites]]\nname = "c1"\n'
                    'host = "c1.example.edu"\nssh_user = "installer"\nsubmit = "gate"\nedit = true\n')
    (tmp_path / "id_gate").write_text("k")
    asked = []

    def ask(site, words, hub=None, data=None, **kw):
        asked.append(words[0])
        if words[0] == "version":
            return {"ok": True, "gate": 1, "nomad": "1.55.0", "edit": True, "account": "kim" if len(words) > 1 else
                    "installer", "home": "/home/kim"}
        if words[0] == "put":
            assert data == b"ok" and words[words.index("--path") + 1].startswith("/home/kim/nomad-gate-test-")
            return {"ok": True, "have": 2, "done": False}
        if words[0] == "put-status":
            return {"ok": True, "have": 2}
        if words[0] == "put-cancel":
            return {"ok": True, "removed": True}
        if words[0] == "get":
            return {"ok": True, "length": 16}
        return {"ok": True, "jobs": []}
    monkeypatch.setattr(remote, "ask", ask)
    r = CliRunner().invoke(cli, ["-c", str(toml), "gate", "test", "--site", "c1", "--user", "kim"])
    assert r.exit_code == 0, r.output
    assert "ok   moving files as them" in r.output and "moving files from the Console: the cluster on" in r.output
    assert asked == ["version", "version", "jobs", "put", "put-status", "put-cancel", "get"]
