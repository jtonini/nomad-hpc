# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.20: the policy watch for AI-agent misuse. A process that reaches root
outside the usual path (escalation); AI agent programs listed, not alerted;
and, from a root helper (off by default), failed privilege attempts and new
setuid files. All read-only, kept as counts and short names."""
import json
import os
import subprocess
import sys
import tempfile

import pytest
from click.testing import CliRunner

from nomad.collectors import policy_watch as pw
from nomad.collectors import watch_probe as probe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "nomad", "collectors", "watch_probe.py")
HELPER = os.path.join(ROOT, "scripts", "watch_root_probe.py")


# ── escalation: a process that reached root outside the usual path ──────────

def tab(*rows):
    """A process table {pid: (uid, ppid, comm)} from (pid, uid, ppid, comm)."""
    return {str(p): (u, str(pp), c) for p, u, pp, c in rows}


def test_root_under_a_user_is_flagged_the_sanctioned_path_is_not():
    t = tab((1, 0, 0, "systemd"), (100, 1500, 1, "bash"),
            (101, 0, 100, "bash"),                       # a root shell straight under the user's shell
            (103, 1500, 100, "sudo"), (102, 0, 103, "bash"),   # sudo -> root: the sanctioned way up
            (200, 0, 1, "sshd"), (201, 0, 200, "sshd"))        # a daemon tree: root under root
    rows = probe._escalations(t, 1000)
    assert [(r["program"], r["detail"], r["uid"]) for r in rows] == [("bash", "bash", 1500)]
    assert rows[0]["kind"] == "escalation"


@pytest.mark.parametrize("parent_comm, flagged", [
    ("python3", True), ("myexploit", True), ("perl", True),
    ("sudo", False), ("su", False), ("pkexec", False), ("sshd", False), ("slurmstepd", False),
    ("systemd", False), ("runuser", False)])
def test_sanctioned_parents(parent_comm, flagged):
    t = tab((50, 1500, 1, parent_comm), (51, 0, 50, "sh"))
    assert bool(probe._escalations(t, 1000)) is flagged


def test_a_system_accounts_root_child_is_not_a_person():
    # parent owned by a service account below min_uid: an ordinary service tree, not a person escalating
    t = tab((60, 400, 1, "daemon"), (61, 0, 60, "sh"))
    assert probe._escalations(t, 1000) == []


def test_ignore_programs_leaves_out_a_sanctioned_root_tool():
    t = tab((70, 1500, 1, "automount-helper"), (71, 0, 70, "mount.nfs"))
    assert probe._escalations(t, 1000)
    assert probe._escalations(t, 1000, ignore_programs=["mount.nfs"]) == []
    assert probe._escalations(t, 1000, ignore_programs=["automount-helper"]) == []


def test_escalations_reads_proc(tmp_path):
    # the real uid comes from /proc/<pid>/status, not the directory owner, so no root is needed,
    # and a setuid helper (whose /proc dir is root-owned but real uid is the user) is not flagged
    p = tmp_path / "proc"
    p.mkdir()
    def mkproc(pid, ruid, ppid, comm):
        (p / str(pid)).mkdir()
        (p / str(pid) / "status").write_text(f"Name:\t{comm}\nUid:\t{ruid}\t{ruid}\t{ruid}\t{ruid}\nPPid:\t{ppid}\n")
    mkproc(1, 0, 0, "systemd")
    mkproc(100, 1500, 1, "bash")
    mkproc(101, 0, 100, "python3")               # a true root process under the user's shell
    mkproc(102, 1500, 100, "ping")               # a setuid helper: real uid stays the user -> not flagged
    rows = probe.escalations(str(p), 1000)
    assert [(r["program"], r["detail"]) for r in rows] == [("python3", "bash")]


# ── AI agents: listed, not alerted ──────────────────────────────────────────

@pytest.mark.parametrize("argv, comm, program", [
    (["aider", "--model", "x"], "aider", "aider"),
    (["claude"], "claude", "claude"),
    (["/opt/bin/codex", "exec"], "codex", "codex"),
    (["python3", "-m", "openhands"], "python3", "openhands"),       # a module
    (["node", "/x/cursor-agent/bin.js"], "node", None),             # not by that path alone
    (["python3", "train.py"], "python3", None)])
def test_agents_are_recognised(argv, comm, program):
    got = [(k, p) for k, p, _d, _s, _a in probe._classify(argv, comm)]
    assert got == ([] if program is None else [("agent", program)])


def test_the_site_adds_agent_names():
    assert [(k, p) for k, p, *_ in probe._classify(["myorg-agent"], "myorg-agent")] == []
    got = [(k, p) for k, p, *_ in probe._classify(["myorg-agent"], "myorg-agent", agents=probe.AGENTS + ("myorg-agent",))]
    assert got == [("agent", "myorg-agent")]


def test_an_agent_is_info_not_alerted():
    a = {"findings": [{"uid": 1500, "user": "amy", "kind": "agent", "program": "aider", "detail": "",
                       "aka": ["aider"], "processes": 1}], "listeners": []}
    [f] = pw.findings_of(a, "spdr05", "node")
    assert f["kind"] == "agent" and f["severity"] == "info" and pw.LABELS["agent"] == "an AI agent"


# ── the root helper's signals, read back by the probe ───────────────────────

def test_escalation_and_root_signals_become_findings():
    a = {"findings": [{"uid": 1500, "user": "amy", "kind": "escalation", "program": "bash",
                       "detail": "python3", "aka": ["bash"], "processes": 1}],
         "listeners": [],
         "root": [{"kind": "root_report", "generated": "2026-10-29T21:00", "stale": False},
                  {"kind": "auth_failures", "uid": 1500, "user": "amy", "program": "", "detail": "9",
                   "processes": 9},
                  {"kind": "setuid_new", "uid": 1500, "user": "amy", "program": "rootme", "detail": "",
                   "processes": 1}]}
    by = {f["kind"]: f for f in pw.findings_of(a, "spdr05", "node")}
    assert by["escalation"]["severity"] == "critical"
    assert by["escalation"]["note"] == "its parent is a python3"
    assert by["auth_failures"]["severity"] == "warning" and by["auth_failures"]["detail"] == "9"
    assert by["setuid_new"]["severity"] == "warning" and by["setuid_new"]["program"] == "rootme"
    assert "root_report" not in by                          # the meta row is not a finding


def test_root_signals_reads_a_file_and_ignores_a_bad_one(tmp_path):
    good = tmp_path / "r.json"
    good.write_text(json.dumps({"generated": "t", "auth_failures": [{"uid": 1500, "user": "amy", "count": 4}],
                                "setuid_new": [{"uid": 1500, "user": "amy", "name": "x"}]}))
    kinds = [r["kind"] for r in probe.root_signals(str(good))]
    assert kinds == ["root_report", "auth_failures", "setuid_new"]
    assert probe.root_signals(str(tmp_path / "missing.json")) == []
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert probe.root_signals(str(bad)) == []
    assert probe.root_signals("") == []
    # a wild count or a non-dict entry is dropped, not trusted
    weird = tmp_path / "w.json"
    weird.write_text(json.dumps({"auth_failures": [{"uid": 1, "user": "a", "count": -5}, "nope", {}]}))
    rows = [r for r in probe.root_signals(str(weird)) if r["kind"] == "auth_failures"]
    assert len(rows) == 1 and rows[0]["detail"] == "0"


# ── the probe end to end, with a root-report file and extra agents ──────────

def test_probe_end_to_end_under_minus_I(tmp_path):
    rep = tmp_path / "root_signals.json"
    rep.write_text(json.dumps({"generated": "2026-10-29T21:00",
                               "auth_failures": [{"uid": 1500, "user": "amy", "count": 3}],
                               "setuid_new": []}))
    src = open(PROBE).read()
    r = subprocess.run([sys.executable, "-I", "-S", "-", "--min-uid", "100000", "--proc", str(tmp_path),
                        "--root-report", str(rep), "--agents", "myorg-agent"],
                       input=src, capture_output=True, text=True, timeout=60)
    d = json.loads(r.stdout)
    assert r.returncode == 0
    assert [x["kind"] for x in d["root"]] == ["root_report", "auth_failures"]
    assert d["root"][1]["user"] == "amy" and d["root"][1]["detail"] == "3"


def test_probe_is_still_python_36():
    import ast
    tree = ast.parse(open(PROBE).read())
    for node in ast.walk(tree):
        assert not isinstance(node, getattr(ast, "NamedExpr", ())), "walrus: 3.8"
    for later in ("removeprefix", "capture_output", "text=True"):
        assert later not in open(PROBE).read(), later


# ── config and CLI ──────────────────────────────────────────────────────────

def test_config_accepts_agents_and_root_report_and_rejects_an_unsafe_path():
    probs, _info = pw.config_problems({"enabled": True, "agents": ["myagent"],
                                       "root_report": "/var/lib/nomad-watch/root_signals.json",
                                       "listeners_on_nodes": True})
    assert probs == []
    c = pw.PolicyWatchCollector({"agents": ["myagent"], "root_report": "/var/lib/nomad-watch/r.json"}, ":memory:")
    args = c._args()
    assert args[args.index("--agents") + 1] == "myagent"
    assert args[args.index("--root-report") + 1] == "/var/lib/nomad-watch/r.json"
    assert pw.PolicyWatchCollector({"root_report": "/etc/x; rm -rf ~"}, ":memory:").root_report == ""
    assert "--root-report" not in pw.PolicyWatchCollector({}, ":memory:")._args()
    # config check flags an unsafe root_report and a non-list agents
    probs2, _ = pw.config_problems({"root_report": "/etc/x; rm -rf ~"})
    assert any("root_report" in p for p in probs2)
    probs3, _ = pw.config_problems({"agents": "notalist"})
    assert probs3 == []        # a single string is allowed (like the other list settings)


def test_new_kinds_have_severity_and_label():
    for k in ("escalation", "agent", "auth_failures", "setuid_new"):
        assert k in pw.SEVERITY and k in pw.LABELS
    assert pw.SEVERITY["escalation"] == "critical" and pw.SEVERITY["agent"] == "info"


def test_watch_list_shows_an_escalation_and_folds_agents_under_all(tmp_path, monkeypatch):
    from nomad.watch.cli import watch_group
    import sqlite3
    from datetime import datetime
    db = str(tmp_path / "site.db")
    with sqlite3.connect(db) as conn:
        pw.ensure_schema(conn)
        now = datetime.now().isoformat(timespec="seconds")
        for kind, prog, detail, note, sev in (
                ("escalation", "bash", "python3", "its parent is a python3", "critical"),
                ("agent", "aider", "", None, "info"),
                ("auth_failures", "", "7", None, "warning")):
            conn.execute("INSERT INTO policy_findings (host, role, username, uid, kind, program, detail, reach, "
                         "note, first_seen, last_seen, sweeps, severity) "
                         "VALUES ('spdr05','node','amy',1500,?,?,?,'',?,?,?,1,?)",
                         (kind, prog, detail, note, now, now, sev))
        conn.commit()
    r = CliRunner().invoke(watch_group, ["list", "--db", db])
    assert r.exit_code == 0, r.output
    assert "reached root outside the usual path bash" in r.output
    assert "its parent is a python3" in r.output
    assert "repeated failed privilege attempts (7 attempts)" in r.output
    assert "aider" not in r.output                              # agent is info: only with --all
    r = CliRunner().invoke(watch_group, ["list", "--db", db, "--all"])
    assert "an AI agent aider" in r.output


def test_documented_in_toml_and_docs():
    toml = open(os.path.join(ROOT, "nomad.toml.example")).read()
    assert "root_report" in toml and "agents = []" in toml and "listeners_on_nodes = true" in toml
    docs = open(os.path.join(ROOT, "docs", "policy_watch.md")).read()
    assert "escalation" in docs and "watch_root_probe" in docs
