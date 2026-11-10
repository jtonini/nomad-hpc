# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.31.0: draining a node and resuming it (`nomad node`, actions node.drain/node.resume);
the lab share's wording."""
from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad import drain
from nomad.actions import spec, runner

LINE = ("NodeName=node02 Arch=x86_64 CoresPerSocket=16 CPUAlloc=12 CPUEfctv=64 CPUTot=64 CPULoad=11.95 "
        "AvailableFeatures=(null) ActiveFeatures=(null) Gres=gpu:a40:2(S:0-1) NodeAddr=node02 "
        "NodeHostName=node02 Version=23.02.7 OS=Linux 4.18.0-513.el8.x86_64 #1 SMP PREEMPT_DYNAMIC "
        "Fri Sep 29 05:21:10 EDT 2023 RealMemory=256000 AllocMem=48000 FreeMem=200000 Sockets=2 Boards=1 "
        "State=MIXED+DRAIN ThreadsPerCore=2 TmpDisk=0 Weight=1 Owner=N/A MCS_label=N/A Partitions=basic,parish "
        "BootTime=2026-10-01T10:00:00 SlurmdStartTime=2026-10-01T10:01:00 LastBusyTime=2026-11-06T09:00:00 "
        "ResumeAfterTime=None CfgTRES=cpu=64,mem=250G,billing=64 AllocTRES=cpu=12,mem=48000M CapWatts=n/a "
        "CurrentWatts=0 AveWatts=0 ExtSensorsJoules=n/s ExtSensorsWatts=0 ExtSensorsTemp=n/s "
        "Reason=disk errors = bad (jt via nomad) [slurm@2026-11-06T10:15:02]")


# --- the catalog ------------------------------------------------------------------------

def test_text_kind():
    p = spec.text_problem
    assert p("disk errors on sda") is None
    assert p("São Paulo's node, 3 of 4 fans") is None
    assert "empty" in p("   ")
    assert "200" in p("x" * 201) and p("x" * 200) is None
    assert "one line" in p("a\nb") and "one line" in p("a\tb") and "one line" in p("a‮b")
    assert "can't hold" in p('say "no"') and "can't hold" in p("a\\b")


def test_node_actions_in_the_catalog():
    d, r = spec.get("node.drain"), spec.get("node.resume")
    assert d.where == r.where == frozenset({spec.SITE})
    s = d.schema()
    assert s["read_only"] is False and s["apply"] == "apply" and s["names"] is True
    assert [p["kind"] for p in s["params"]] == ["name", "text", "caller", "flag"]
    assert all(spec.get(a["name"]).schema()["read_only"] for a in spec.catalog()
               if a["name"] not in ("node.drain", "node.resume", "maintenance.start", "maintenance.end"))
    assert spec.check(d, {"node": "node02", "reason": " disk "}) == {"node": "node02", "reason": "disk",
                                                                     "apply": False}
    assert spec.check(d, {"node": "n1", "reason": "x", "apply": "true", "by": "jt"})["apply"] is True
    assert spec.check(r, {"node": "n1"}) == {"node": "n1", "apply": False}
    for bad in ({"node": "n1"},                                    # drain needs a reason
                {"reason": "x"},                                   # and a node
                {"node": "n[1-3]", "reason": "x"}, {"node": "-n1", "reason": "x"},
                {"node": "n1", "reason": 'a"b'}, {"node": "n1", "reason": "x", "apply": "maybe"},
                {"node": "n1", "reason": "x", "apply": 1.5}, {"node": "n1", "reason": "x", "by": "a b"},
                {"node": "n1", "reason": "x", "state": "DOWN"}):
        with pytest.raises(spec.ActionError):
            spec.check(d, bad)


def test_argv_flag_and_one_word_reason():
    d = spec.get("node.drain")
    argv = runner.build_argv(d, spec.check(d, {"node": "node02", "reason": "--apply", "by": "jt"}))
    assert argv[3:] == ["node", "drain", "--json", "--reason=--apply", "--by", "jt", "--", "node02"]
    argv = runner.build_argv(d, spec.check(d, {"node": "node02", "reason": "disk", "apply": True}))
    assert argv[3:] == ["node", "drain", "--json", "--reason=disk", "--apply", "--", "node02"]
    # what the command makes of it: a reason that looks like --apply is a reason, not --apply
    from nomad.cli import cli
    seen = {}

    def fake(node, what, **kw):
        seen.update(node=node, what=what, **kw)
        return {"ok": True}
    import nomad.drain as D
    old = D.change
    D.change = fake
    try:
        argv = runner.build_argv(d, spec.check(d, {"node": "node02", "reason": "--apply"}))
        res = CliRunner().invoke(cli, ["-c", "/nonexistent.toml", *argv[3:]])
    finally:
        D.change = old
    assert res.exit_code == 0, res.output
    assert seen["reason"] == "--apply" and seen["apply"] is False and seen["node"] == "node02"


# --- what Slurm says -------------------------------------------------------------------

def test_parse_node():
    n = drain.parse_node(LINE)
    assert n["node"] == "node02" and n["state"] == "MIXED+DRAIN" and n["base"] == "MIXED"
    assert n["flags"] == ["DRAIN"] and drain.drained(n) and drain.resumable(n)
    assert n["reason"] == "disk errors = bad (jt via nomad)"
    assert n["reason_user"] == "slurm" and n["reason_time"] == "2026-11-06T10:15:02"
    assert (n["cpus_alloc"], n["cpus_total"], n["partitions"]) == (12, 64, "basic,parish")
    assert "draining" in drain.described(n)
    m = drain.parse_node(LINE.replace("State=MIXED+DRAIN", "State=IDLE").split(" Reason=")[0])
    assert m["reason"] is None and not drain.drained(m) and not drain.resumable(m)
    m = drain.parse_node(LINE + " Comment=on loan Extra=x")
    assert m["reason"] == "disk errors = bad (jt via nomad)"
    m = drain.parse_node(LINE.replace("State=MIXED+DRAIN", "State=DOWN+NOT_RESPONDING")
                         .replace(" [slurm@2026-11-06T10:15:02]", ""))
    assert m["base"] == "DOWN" and drain.resumable(m) and m["reason_user"] is None
    assert "not responding" in drain.described(m)
    assert drain.parse_node("Node node09 not found") is None
    j = drain.parse_jobs("101|alice|basic|RUNNING|1:00:00|2:00:00|4|1\nbad line\n102|bob|gpu|RUNNING|5:00|UNLIMITED|8|2\n")
    assert [x["job"] for x in j] == ["101", "102"] and j[1]["nodes"] == 2 and j[0]["cpus"] == 4


# --- the change, with Slurm and sudo pretended -------------------------------------------

class Slurm:
    """scontrol, squeue and sudo as the code calls them."""

    def __init__(self, state="MIXED", reason=None, sudo_ok=True, update_rc=0, takes=True, rc_after=0,
                 responding=True):
        self.state, self.reason, self.sudo_ok, self.update_rc, self.takes = state, reason, sudo_ok, update_rc, takes
        self.rc_after, self.responding = rc_after, responding     # rc_after: done, yet this exit (a time-out)
        self.calls = []

    def line(self):
        out = f"NodeName=node02 CPUAlloc=12 CPUTot=64 OS=Linux 4.18 #1 SMP State={self.state} Partitions=basic"
        return out + (f" Reason={self.reason} [slurm@2026-11-06T10:15:02]" if self.reason else "")

    def __call__(self, argv, timeout=20):
        self.calls.append(argv)
        a = list(argv)
        if a[0] == "/usr/bin/sudo":
            if a[1:3] == ["-n", "-l"]:
                return (0, " ".join(a[5:]) + "\n", "") if self.sudo_ok else (1, "", "sudo: a password is required\n")
            a = a[4:]
        if a[:3] == ["/usr/bin/scontrol", "show", "node"]:
            return (0, self.line() + "\n", "") if a[4] == "node02" else (1, "", f"Node {a[4]} not found\n")
        if a[0] == "/usr/bin/squeue":
            return 0, "101|alice|basic|RUNNING|1:00:00|2:00:00|4|1\n", ""
        if a[:2] == ["/usr/bin/scontrol", "update"]:
            if self.update_rc:
                return self.update_rc, "", "slurm_update error: Invalid node state specified\n"
            if self.takes:
                kv = dict(w.split("=", 1) for w in a[2:])
                base = self.state.split("+")[0]
                if kv["State"] == "DRAIN":
                    self.state, self.reason = base + "+DRAIN", kv["Reason"]
                elif base == "DOWN":
                    self.state, self.reason = "IDLE" if self.responding else "IDLE+NOT_RESPONDING", None
                else:
                    self.state, self.reason = base, None
            return self.rc_after, "", ""
        raise AssertionError(argv)


def finder(name):
    return f"/usr/bin/{name}"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(drain, "_install_checked", lambda: None)     # its own tests below
    monkeypatch.setattr(drain.shutil, "which", lambda n, path=None: f"/usr/bin/{n}" if n == "sudo" else None)
    return tmp_path


CFG = {"actions": {"slurm_as": "slurm"}}


def test_refused_before_anything(home):
    s = Slurm()
    for kw in ({"node": "n[1-3]"}, {"node": "-x"}, {"node": "node02", "reason": 'a "b"'},
               {"node": "node02", "reason": "x", "by": "a b"}, {"node": "node02"}):
        kw.setdefault("reason", "disk")
        if kw.get("node") == "node02" and "reason" in kw and kw["reason"] == "disk" and len(kw) == 2:
            kw["reason"] = None                     # drain without a reason
        with pytest.raises(drain.NodeError):
            drain.change(kw.pop("node"), drain.DRAIN, config=CFG, run=s, finder=finder, **kw)
    with pytest.raises(drain.NodeError, match="no node called node09"):
        drain.change("node09", drain.DRAIN, reason="x", config=CFG, run=s, finder=finder)
    with pytest.raises(drain.NodeError, match="no scontrol"):
        drain.change("node02", drain.DRAIN, reason="x", config=CFG, run=s, finder=lambda n: None)
    assert not any("update" in c for c in s.calls)


def test_off_until_slurm_as(home):
    s = Slurm()
    r = drain.change("node02", drain.DRAIN, reason="disk", by="jt", config={}, run=s, finder=finder,
                     config_path="/home/zeus/.config/nomad/nomad.toml", apply=True)
    assert r["ok"] is False and r["applied"] is False
    assert "slurm_as is not set in /home/zeus/.config/nomad/nomad.toml" in r["problems"][0]
    assert not any("update" in c for c in s.calls)
    r = drain.change("node02", drain.DRAIN, reason="disk", config={"actions": {"slurm_as": "-u root"}},
                     run=s, finder=finder)
    assert r["ok"] is False and "neither" in r["problems"][0]


def test_drain_dry_run_then_apply(home):
    s = Slurm()
    r = drain.change("node02", drain.DRAIN, reason="disk errors", by="jt", config=CFG, run=s, finder=finder)
    assert r["ok"] is True and r["applied"] is False and r["runs_as"] == "slurm"
    assert r["command"] == ["/usr/bin/sudo", "-n", "-u", "slurm", "/usr/bin/scontrol", "update",
                            "NodeName=node02", "State=DRAIN", "Reason=disk errors (jt via nomad)"]
    assert ["/usr/bin/sudo", "-n", "-l", "-u", "slurm", *r["command"][4:]] in s.calls
    assert r["jobs_running"] == 1 and r["jobs"][0]["user"] == "alice"
    assert not any("update" in c and "-l" not in c for c in s.calls)
    text = "\n".join(drain.lines(r))
    assert "Would run: /usr/bin/sudo -n -u slurm /usr/bin/scontrol update NodeName=node02 State=DRAIN " \
           "'Reason=disk errors (jt via nomad)'" in text
    assert "Nothing changed (dry run)" in text and "run to their end" in text
    assert not (home / ".local/share/nomad/nodes.log").exists()

    r = drain.change("node02", drain.DRAIN, reason="disk errors", by="jt", config=CFG, run=s, finder=finder,
                     apply=True)
    assert r["ok"] is True and r["applied"] is True and r["after"]["state"] == "MIXED+DRAIN"
    assert r["after"]["reason"] == "disk errors (jt via nomad)" and "now draining" in r["said"]
    log = [json.loads(x) for x in (home / ".local/share/nomad/nodes.log").read_text().splitlines()]
    assert log[-1]["ok"] is True and log[-1]["before"] == "MIXED" and log[-1]["after"] == "MIXED+DRAIN"
    assert log[-1]["by"] == "jt" and log[-1]["reason"] == "disk errors"
    assert stat.S_IMODE(os.stat(home / ".local/share/nomad/nodes.log").st_mode) == 0o600
    assert "Done: node02 is now draining" in "\n".join(drain.lines(r))


def test_sudo_not_allowed(home):
    s = Slurm(sudo_ok=False)
    r = drain.change("node02", drain.DRAIN, reason="disk", config=CFG, run=s, finder=finder, apply=True)
    assert r["ok"] is False and r["applied"] is False
    who = drain._account()
    assert f"{who} ALL=(slurm) NOPASSWD: /usr/bin/scontrol" in r["problems"][0]
    assert not any(c[:2] == ["/usr/bin/sudo", "-n"] and c[2] == "-u" for c in s.calls)
    log = [json.loads(x) for x in (home / ".local/share/nomad/nodes.log").read_text().splitlines()]
    assert log[-1]["ok"] is False and "sudo would not" in log[-1]["said"]


def test_scontrol_fails_or_slurm_shows_otherwise(home):
    r = drain.change("node02", drain.DRAIN, reason="disk", config=CFG, run=Slurm(update_rc=1), finder=finder,
                     apply=True)
    assert r["ok"] is False and r["applied"] is True and "exit 1" in r["said"] and "Invalid node" in r["said"]
    r = drain.change("node02", drain.DRAIN, reason="disk", config=CFG, run=Slurm(takes=False), finder=finder,
                     apply=True)
    assert r["ok"] is False and "but node02 is mixed" in r["said"]


def test_resume(home):
    s = Slurm(state="IDLE")
    r = drain.change("node02", drain.RESUME, config=CFG, run=s, finder=finder, apply=True)
    assert r["ok"] is False and "nothing to resume" in r["problems"][0] and not r["applied"]
    s = Slurm(state="IDLE+DRAIN", reason="disk (jt via nomad)")
    r = drain.change("node02", drain.RESUME, reason="disk replaced", by="jt", config=CFG, run=s, finder=finder)
    assert r["ok"] and r["command"][-1] == "State=RESUME" and r["slurm_reason"] is None
    assert "its reason, 'disk (jt via nomad)', goes" in "\n".join(drain.lines(r))
    r = drain.change("node02", drain.RESUME, reason="disk replaced", by="jt", config=CFG, run=s, finder=finder,
                     apply=True)
    assert r["ok"] and r["after"]["state"] == "IDLE" and r["after"]["reason"] is None
    s = Slurm(state="DOWN+NOT_RESPONDING")
    r = drain.change("node02", drain.RESUME, config=CFG, run=s, finder=finder)
    assert r["ok"] and not r["problems"]


def test_drain_again_and_self(home):
    s = Slurm(state="IDLE+DRAIN", reason="old")
    r = drain.change("node02", drain.DRAIN, reason="new", config={"actions": {"slurm_as": "self"}}, run=s,
                     finder=finder)
    assert r["ok"] and r["command"][0] == "/usr/bin/scontrol" and "replaces the reason" in r["notes"][0]
    assert any("Slurm administrator" in n for n in r["notes"])
    assert not any(c[0] == "/usr/bin/sudo" for c in s.calls)


def test_find_refuses_a_program_others_could_change(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    p = d / "scontrol"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    assert drain.find("scontrol", {"PATH": str(d)}) == str(p)
    d.chmod(0o777)
    try:
        with pytest.raises(drain.NodeError, match="someone else could have put it there"):
            drain.find("scontrol", {"PATH": str(d)})
    finally:
        d.chmod(0o755)


def test_what_slurm_shows_decides(home):
    """1.31.0 review: Slurm's state after the change decides, whatever scontrol's exit said."""
    # a reason holding what looks like the next field, and dates with a space
    s = Slurm()
    r = drain.change("node02", drain.DRAIN, reason="bad DIMM, see Comment=rack 4", by="jt", config=CFG, run=s,
                     finder=finder, apply=True)
    assert r["ok"] and r["after"]["reason"] == "bad DIMM, see"            # shown cut; kept whole
    assert drain.has_reason({"reason_raw": "x (jt via nomad) [slurm@2026-11-06 12:00:00]"}, "x (jt via nomad)")
    assert not drain.has_reason({"reason_raw": "x (jt via nomad) more"}, "x (jt via nomad)")
    assert not drain.has_reason({"reason_raw": None}, "x")
    # scontrol took too long, and did it: done; took too long, and didn't (yet): not known
    r = drain.change("node02", drain.DRAIN, reason="fans", config=CFG, run=Slurm(rc_after=124), finder=finder,
                     apply=True)
    assert r["ok"] and "took too long" in r["said"]
    r = drain.change("node02", drain.DRAIN, reason="fans", config=CFG, run=Slurm(rc_after=124, takes=False),
                     finder=finder, apply=True)
    assert not r["ok"] and r["said"].startswith("not known yet") and "node02 is mixed" in r["said"]
    # drained already, but not with this reason
    r = drain.change("node02", drain.DRAIN, reason="fans", config=CFG, run=Slurm(state="IDLE+DRAIN", reason="old",
                     takes=False), finder=finder, apply=True)
    assert not r["ok"] and "but its reason is 'old'" in r["said"]
    # resumed: not while it is still down; not responding yet is how a down node comes back
    r = drain.change("node02", drain.RESUME, config=CFG, run=Slurm(state="DOWN", takes=False), finder=finder,
                     apply=True)
    assert not r["ok"] and "node02 is down" in r["said"]
    r = drain.change("node02", drain.RESUME, config=CFG, run=Slurm(state="DOWN", responding=False), finder=finder,
                     apply=True)
    assert r["ok"] and "sets it down again" in r["said"]


def test_all_refused_and_refusals_logged(home):
    with pytest.raises(drain.NodeError, match="not ALL"):
        drain.change("all", drain.DRAIN, reason="x", config=CFG, run=Slurm(), finder=finder, apply=True)
    with pytest.raises(drain.NodeError):
        drain.change("node09", drain.RESUME, config=CFG, run=Slurm(), finder=finder)      # a look: not logged
    log = [json.loads(x) for x in (home / ".local/share/nomad/nodes.log").read_text().splitlines()]
    assert len(log) == 1 and log[0]["node"] == "all" and log[0]["said"].startswith("refused: ")


def test_slurm_programs_share_one_environment(home, monkeypatch):
    monkeypatch.setenv("SLURM_CONF", "/elsewhere/slurm.conf")
    monkeypatch.setenv("SLURM_CLUSTERS", "other")
    monkeypatch.setenv("SLURM_TIME_FORMAT", "%F %T")
    env = drain._env()
    assert "SLURM_CONF" not in env and "SLURM_CLUSTERS" not in env and env["SLURM_TIME_FORMAT"] == "standard"
    with pytest.raises(drain.NodeError, match="SLURM_CLUSTERS, SLURM_CONF set here is not used"):
        drain.change("node09", drain.DRAIN, reason="x", config=CFG, run=Slurm(), finder=finder)


def test_the_sudo_line_lets_these_two_only():
    line = drain.sudo_line("zeus", "slurm", "/usr/bin/scontrol")
    assert line == ("zeus ALL=(slurm) NOPASSWD: /usr/bin/scontrol update NodeName\\=* State\\=DRAIN Reason\\=*, "
                    "/usr/bin/scontrol update NodeName\\=* State\\=RESUME")


def test_a_nomad_others_could_change_runs_nothing(home):
    s = Slurm()
    r = drain.change("node02", drain.DRAIN, reason="x", config=CFG, run=s, finder=finder, apply=True,
                     checker=lambda: "/opt/nomad/lib/site.py can be changed by others (mode 666)")
    assert not r["ok"] and not r["applied"] and "someone else could change the nomad" in r["problems"][0]
    assert not any("update" in c and "-l" not in c for c in s.calls)
    r = drain.change("node02", drain.DRAIN, reason="x", config={}, run=s, finder=finder,
                     checker=lambda: "never asked")                           # off: nothing to check
    assert not any("never asked" in p for p in r["problems"])


@pytest.fixture
def open_tmp():
    """A folder others can pass through (pytest's own is 700: it would shield all below, 1.31.1)."""
    import shutil as sh
    import tempfile
    d = Path(tempfile.mkdtemp(dir="/tmp"))
    d.chmod(0o755)
    yield d
    sh.rmtree(d, ignore_errors=True)


def test_install_problem(open_tmp, monkeypatch):
    tmp_path = open_tmp
    import nomad
    lib, pkg = tmp_path / "lib", tmp_path / "lib" / "nomad"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (lib / "six.py").write_text("")
    monkeypatch.setattr(nomad, "__file__", str(pkg / "__init__.py"))
    monkeypatch.setattr(drain.sys, "path", [str(lib), str(tmp_path / "missing" / "py.zip")])
    monkeypatch.chdir(tmp_path)
    assert drain.install_problem() is None
    os.chmod(lib / "six.py", 0o666)
    assert "six.py can be changed by others" in drain.install_problem()
    os.chmod(lib / "six.py", 0o644)
    os.chown(lib / "six.py", 4321, -1)
    assert "six.py belongs to" in drain.install_problem()
    os.chown(lib / "six.py", 0, -1)
    bad = tmp_path / "open"
    bad.mkdir()
    bad.chmod(0o777)
    (lib / "link").symlink_to(bad)
    assert "link leads to" in drain.install_problem()
    (lib / "link").unlink()
    (tmp_path / "missing").mkdir(mode=0o777)
    os.chmod(tmp_path / "missing", 0o777)                        # where an import path could be made
    assert "missing can be changed by others" in drain.install_problem()


def test_a_folder_only_its_owner_passes_shields_what_is_below(open_tmp, monkeypatch):
    """1.31.1: arachne's ~/.local (700) over ~/.local/lib (775, group localuser: zeus's primary group,
    with five other members): no one else reaches it, whatever its mode."""
    import nomad
    tmp_path = open_tmp
    other_gid = 4321                                             # a group with others in it
    home = tmp_path / "home"
    site = home / ".local" / "lib" / "python3.12" / "site-packages"
    pkg = site / "nomad"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for p in (home / ".local" / "lib", site, pkg, pkg / "__init__.py"):
        os.chown(p, -1, other_gid)
        p.chmod(0o775 if p.is_dir() else 0o664)
    monkeypatch.setattr(nomad, "__file__", str(pkg / "__init__.py"))
    monkeypatch.setattr(drain.sys, "path", [str(site)])
    monkeypatch.chdir(tmp_path)
    (home / ".local").chmod(0o755)
    assert "lib can be changed by others (mode 775)" in drain.install_problem()
    (home / ".local").chmod(0o700)
    assert drain.install_problem() is None
    (home / ".local").chmod(0o710)                               # its group may pass: no shield
    assert "can be changed by others" in drain.install_problem()
    (home / ".local").chmod(0o700)
    home.chmod(0o777)                                            # the shield itself could be replaced
    assert "home can be changed by others (mode 777)" in drain.install_problem()
    home.chmod(0o755)
    os.chown(pkg / "__init__.py", 4321, -1)                      # another account's file, shield or not
    assert "__init__.py belongs to" in drain.install_problem()


def test_sudo_wanting_a_password_is_said(home):
    class Asks(Slurm):
        def __call__(self, argv, timeout=20):
            if argv[:4] == ["/usr/bin/sudo", "-n", "-u", "slurm"]:
                self.calls.append(argv)
                return 1, "", "sudo: a password is required\n"
            return super().__call__(argv, timeout)
    r = drain.change("node02", drain.DRAIN, reason="x", config=CFG, run=Asks(), finder=finder, apply=True)
    assert not r["ok"] and r["said"].startswith("sudo wanted a password for it, so nothing ran")
    assert "NOPASSWD: /usr/bin/scontrol update NodeName" in r["said"] and "node02 is mixed" in r["said"]


def test_sudo_insisting_on_a_terminal_is_said(home):
    """1.31.2: vadur (RHEL 7) has Defaults requiretty; its agent has no terminal."""
    TTY = "sudo: sorry, you must have a tty to run sudo\n"

    class NoTty(Slurm):
        def __call__(self, argv, timeout=20):
            if argv[0] == "/usr/bin/sudo":
                self.calls.append(argv)
                return 1, "", TTY
            return super().__call__(argv, timeout)
    s = NoTty()
    r = drain.change("node02", drain.DRAIN, reason="x", config=CFG, run=s, finder=finder, apply=True)
    who = drain._account()
    assert not r["ok"] and not r["applied"] and "insists on a terminal (requiretty)" in r["problems"][0]
    assert f"Defaults:{who} !requiretty" in r["problems"][0] and "NOPASSWD" in r["problems"][0]
    assert not any(c[:4] == ["/usr/bin/sudo", "-n", "-u", "slurm"] for c in s.calls)
    assert drain.needs_tty(TTY) and not drain.needs_tty("sudo: a password is required")

    # sudo -l let it through (a rule that lists), the change itself met requiretty
    class TtyOnRun(Slurm):
        def __call__(self, argv, timeout=20):
            if argv[:4] == ["/usr/bin/sudo", "-n", "-u", "slurm"]:
                return 1, "", TTY
            return super().__call__(argv, timeout)
    r = drain.change("node02", drain.DRAIN, reason="x", config=CFG, run=TtyOnRun(), finder=finder, apply=True)
    assert not r["ok"] and r["said"].startswith("sudo insisted on a terminal, so nothing ran")
    assert f"Defaults:{who} !requiretty" in r["said"]


def test_slurm_and_sudo_run_without_a_terminal():
    """1.31.2: in a session of their own, so a dry run in a terminal meets what the agent meets."""
    code, out, _ = drain._run([sys.executable, "-c", "import os; print(os.getsid(0) == os.getpid())"])
    assert code == 0 and out.strip() == "True"


def test_the_install_check_is_kept_a_while(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    walks = []
    monkeypatch.setattr(drain, "install_problem", lambda: walks.append(1))
    assert drain._install_checked() is None and drain._install_checked() is None and len(walks) == 1
    mark = tmp_path / ".local/share/nomad/install.ok"
    assert stat.S_IMODE(mark.stat().st_mode) == 0o600
    mark.chmod(0o666)                                            # a mark others could write is no mark
    drain._install_checked()
    assert len(walks) == 2
    os.utime(mark, (0, 0))                                       # nor an old one
    drain._install_checked()
    assert len(walks) == 3
    monkeypatch.setattr(drain, "install_problem", lambda: "x belongs to bob")
    mark.unlink()
    assert drain._install_checked() == "x belongs to bob" and not mark.exists()


# --- end to end: the command, the action, the agent ----------------------------------------

FAKE = r'''#!{py}
import json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
f = os.path.join(d, "node.json")
st = json.load(open(f))
a = sys.argv[1:]
name = os.path.basename(sys.argv[0])
if name == "squeue":
    print("101|alice|basic|RUNNING|1:00:00|2:00:00|4|1")
    sys.exit(0)
if a[:3] == ["show", "node", "-o"]:
    if a[3] != "node02":
        print("Node %s not found" % a[3], file=sys.stderr)
        sys.exit(1)
    line = "NodeName=node02 CPUAlloc=4 CPUTot=8 OS=Linux 4.18 #1 SMP State=%s Partitions=basic" % st["state"]
    if st.get("reason"):
        line += " Reason=%s [zeus@2026-11-06T10:15:02]" % st["reason"]
    print(line)
    sys.exit(0)
if a[0] == "update":
    open(os.path.join(d, "calls"), "a").write(json.dumps(a) + "\n")
    kv = dict(w.split("=", 1) for w in a[1:])
    if kv["State"] == "DRAIN":
        st["state"], st["reason"] = "MIXED+DRAIN", kv["Reason"]
    else:
        st["state"], st["reason"] = "MIXED", None
    json.dump(st, open(f, "w"))
    sys.exit(0)
sys.exit(2)
'''


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    b = tmp_path / "slurm"
    b.mkdir()
    for n in ("scontrol", "squeue"):
        (b / n).write_text(FAKE.replace("{py}", sys.executable))
        (b / n).chmod(0o755)
    (b / "node.json").write_text(json.dumps({"state": "MIXED"}))
    home = tmp_path / "home"
    (home / ".config/nomad").mkdir(parents=True)
    (home / ".config/nomad/nomad.toml").write_text('[actions]\nslurm_as = "self"\n')
    # this test tree's nomad, taken as checked (a test home's folders are not this account's)
    (home / ".local/share/nomad").mkdir(parents=True)
    (home / ".local/share/nomad/install.ok").write_text(drain._install_key())
    (home / ".local/share/nomad/install.ok").chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    return b, home


def test_the_command(cluster):
    b, home = cluster
    from nomad.cli import cli
    cfg = str(home / ".config/nomad/nomad.toml")
    res = CliRunner().invoke(cli, ["-c", cfg, "node", "drain", "node02", "--reason", "fans", "--by", "jt"])
    assert res.exit_code == 0, res.output
    assert "1 job running there" in res.output and "Nothing changed (dry run)" in res.output
    assert not (b / "calls").exists()
    res = CliRunner().invoke(cli, ["-c", cfg, "node", "drain", "node02", "--reason", "fans", "--by", "jt",
                                   "--apply", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["ok"] and out["after"]["reason"] == "fans (jt via nomad)"
    assert json.loads((b / "calls").read_text()) == ["update", "NodeName=node02", "State=DRAIN",
                                                     "Reason=fans (jt via nomad)"]
    res = CliRunner().invoke(cli, ["-c", cfg, "node", "resume", "node09", "--json"])
    assert res.exit_code == 1 and "no node called node09" in json.loads(res.output)["error"]


def test_the_action_and_the_agent(cluster, monkeypatch):
    b, home = cluster
    from nomad.actions import agent
    real, repo = runner.run_argv, str(Path(__file__).resolve().parent.parent)

    def this_nomad(argv, timeout, **kw):            # the child runs this tree's nomad, not one installed
        assert argv[1:3] == ["-m", "nomad.cli"]
        boot = f"import sys; sys.path.insert(0, {repo!r}); from nomad.cli import cli; cli()"
        return real([argv[0], "-c", boot, *argv[3:]], timeout, **kw)
    monkeypatch.setattr(runner, "run_argv", this_nomad)
    req = {"v": 1, "action": "node.drain", "params": {"node": "node02", "reason": "fans", "by": "jt"}}
    out = io.StringIO()
    assert agent.serve(io.BytesIO(json.dumps(req).encode()), out) == 0
    reply = json.loads(out.getvalue().split(agent.MARKER, 1)[1])
    assert reply["ok"] and json.loads(reply["output"])["ok"] and not (b / "calls").exists(), reply
    req["params"]["apply"] = True
    out = io.StringIO()
    assert agent.serve(io.BytesIO(json.dumps(req).encode()), out) == 0
    reply = json.loads(out.getvalue().split(agent.MARKER, 1)[1])
    got = json.loads(reply["output"])
    assert reply["ok"] and got["applied"] and got["after"]["state"] == "MIXED+DRAIN"
    log = [json.loads(x) for x in (home / ".local/share/nomad/agent.log").read_text().splitlines()]
    assert log[-1]["action"] == "node.drain" and log[-1]["params"]["apply"] is True
    # the agent checks again: what the catalog refuses never runs
    for params in ({"node": "node02", "reason": 'x" State=DOWN'}, {"node": "node02 node03", "reason": "x"},
                   {"node": "node02", "reason": "x", "argv": ["--apply"]}):
        out = io.StringIO()
        assert agent.serve(io.BytesIO(json.dumps({**req, "params": params}).encode()), out) == 2
    assert len((b / "calls").read_text().splitlines()) == 1


# --- the lab share's wording -------------------------------------------------------------

def test_the_labs_part_of_everyones_use_is_unknown_when_every_day_of_theirs_is_left_out(tmp_path):
    from tests.test_release_1300 import _run, t
    jobs = [("1", "s1", "basic", "n01", t(3), t(3), t(3, 8), "COMPLETED", 8),
            ("2", "loner", "basic", "n02", t(20), t(20), t(20, 4), "COMPLETED", 4)]
    _d, c = _run(tmp_path, jobs)
    tot = c["hpc1"]["total"]
    assert tot["others_core_hours"] == 0 and tot["others_left_out_days"] == 1
    assert tot["lab_of_used_pct"] is None              # was 100: "at most 100% of everyone's"
    _d, c = _run(tmp_path, jobs, min_others=0)
    assert c["hpc1"]["total"]["lab_of_used_pct"] == round(100 * 64 / (64 + 16), 1)
    # some of their days shown: at most the lab's part of what can be shown
    jobs += [(f"o{i}", u, "basic", "n02", t(10), t(10), t(10, 2), "COMPLETED", 4)
             for i, u in enumerate(["a", "b", "c"])]
    _d, c = _run(tmp_path, jobs)
    tot = c["hpc1"]["total"]
    assert tot["others_left_out_days"] == 1 and tot["lab_of_used_pct"] == round(100 * 64 / (64 + 24), 1)


def test_what_others_held_is_said_of_the_days_that_can_be_shown(tmp_path):
    from nomad.cli import cli
    from tests.test_release_1300 import FOUR, NOW, REPORT, build, t
    jobs = FOUR + [("h", "holder", "basic", "n[01-02]", t(20), t(20), t(20, 5), "COMPLETED", 32),
                   ("8", "s1", "basic", "n01", t(20), t(20, 3), t(20, 4), "COMPLETED", 8, "Priority")]
    jobs += [(f"k{i}", u, "basic", "n01", t(25), t(25, 1), t(25, 5), "COMPLETED", 4)
             for i, u in enumerate(["a", "b", "c"])]
    jobs += [("9", "s1", "basic", "n02", t(25, 2), t(25, 3), t(25, 4), "COMPLETED", 8, "Priority")]
    build(tmp_path / "c.db", jobs).close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n')
    (tmp_path / "report.toml").write_text(REPORT)
    r = CliRunner().invoke(cli, ["-c", str(cfg), "facts", "share", "jsmith", "--db", str(tmp_path / "c.db"),
                                 "--report", str(tmp_path / "report.toml"), "--now", NOW.isoformat(),
                                 "--min-others", "3"])
    assert r.exit_code == 0, r.output
    assert "the lab's own: 2 jobs waited" in r.output
    assert "; on the days that can be shown, others held 100% of the cores in use then (1 day left out)" \
        in r.output
