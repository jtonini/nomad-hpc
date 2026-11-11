# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.40.0: the gate through a host the hub jumps through (gate_jump; nomad
gate jump-key), and the hub's sync timer from [hub] sync_interval_minutes
(nomad sync timer)."""
from __future__ import annotations

import shlex
import subprocess

import pytest
from click.testing import CliRunner

from nomad import sync_timer as T
from nomad.submit import gate, remote

PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBqLm5Kcl1V0Hn5dGx0rP6yE8Wq8l9Q5Yk2n5yT0aVbC nomad-gate@hub"


def _site(tmp_path, **kw):
    key = tmp_path / "id_gate"
    key.write_text("x")
    return {"name": "vadur", "host": "vadur", "ssh_user": "nomad", "submit": "gate", "gate_key": str(key), **kw}


# -- the gate through a jump host -----------------------------------------------------------------

def test_a_gate_reached_through_another_host(tmp_path):
    s = _site(tmp_path, gate_jump="zeus@jonimitchell")
    cfg = remote.settings(s)
    assert cfg["jump"] == {"user": "zeus", "host": "jonimitchell", "port": None}
    argv = remote.ssh_argv(cfg, ["jobs", "--user", "jdoe"])
    assert argv[-4:-1] == ["nomad", "--", "vadur"]               # the cluster itself, as before
    proxy = [a for a in argv if a.startswith("ProxyCommand=")]
    assert len(proxy) == 1 and argv[argv.index(proxy[0]) - 1] == "-o"
    inner = shlex.split(proxy[0][len("ProxyCommand="):])
    assert inner[:3] == ["ssh", "-F", "/dev/null"] and inner[inner.index("-i") + 1] == cfg["key"]
    for o in ("IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes", "StrictHostKeyChecking=yes",
              "ControlPath=none", "ForwardAgent=no", "PasswordAuthentication=no"):
        assert o in inner
    assert inner[-5:] == ["zeus", "-W", "%h:%p", "--", "jonimitchell"] and "-l" in inner
    assert shlex.split(argv[-1]) == [gate.GATE, "jobs", "--user", "jdoe"]
    # a port, and a key whose path has a % (ssh's own tokens in ProxyCommand)
    odd = tmp_path / "k%h"
    odd.write_text("x")
    cfg = remote.settings({**s, "gate_jump": "zeus@10.0.0.5:2222", "gate_key": str(odd)})
    inner = shlex.split(remote.proxy_command(cfg))
    assert inner[inner.index("-p") + 1] == "2222" and inner[inner.index("-i") + 1].endswith("k%%h")
    # none: as before, no proxy at all; nothing of the account's ssh settings either way
    plain = remote.ssh_argv(remote.settings(_site(tmp_path)), ["version"])
    assert not any(a.startswith("ProxyCommand") for a in plain) and "jump" not in remote.settings(_site(tmp_path))
    for bad in ("jonimitchell", "zeus@", "Zeus Smith@x", "zeus@-oProxyCommand=x", "zeus@h:0", "zeus@h:99999", 5):
        with pytest.raises(remote.GateError, match="gate_jump"):
            remote.settings({**s, "gate_jump": bad})


def test_the_jump_hosts_key_line(tmp_path):
    line = remote.jump_line(PUB, ["vadur:22"], "141.166.186.112")
    opts, kind, blob, comment = line.split(" ", 3)
    assert opts.split(",") == ["restrict", "port-forwarding", 'permitopen="vadur:22"', 'command="/bin/false"',
                               'from="141.166.186.112"']
    assert (kind, comment) == ("ssh-ed25519", "nomad-gate@hub")
    two = remote.jump_line(PUB, ["vadur:22", "10.1.2.3:2222"], None)
    assert 'permitopen="vadur:22",permitopen="10.1.2.3:2222"' in two and "from=" not in two
    for bad in ([], ["vadur"], ["vadur:0"], ["-x:22"], ['vadur:22",x']):
        with pytest.raises(ValueError):
            remote.jump_line(PUB, bad, None)
    with pytest.raises(ValueError):
        remote.jump_line(PUB, ["vadur:22"], 'x" ,command="sh')
    # written once, then present; a line with the same key and other options is replaced
    ak = tmp_path / ".ssh" / "authorized_keys"
    ak.parent.mkdir()
    ak.write_text("ssh-rsa AAAAother someone\n" + PUB + "\n")
    status, msg = remote.install_jump_key(PUB, to=["vadur:22"], origins="141.166.186.112", path=ak)
    assert status == "would-change" and ak.read_text().count(PUB.split()[1]) == 1 and "restrict" not in ak.read_text()
    status, _ = remote.install_jump_key(PUB, to=["vadur:22"], origins="141.166.186.112", path=ak, apply=True)
    assert status == "changed"
    got = ak.read_text().splitlines()
    assert got[0] == "ssh-rsa AAAAother someone" and got[1] == line and len(got) == 2
    assert remote.install_jump_key(PUB, to=["vadur:22"], origins="141.166.186.112", path=ak)[0] == "present"


def test_nomad_gate_jump_key(tmp_path, monkeypatch):
    from nomad.cli import cli
    monkeypatch.setenv("HOME", str(tmp_path))
    r = CliRunner().invoke(cli, ["gate", "jump-key", PUB, "--to", "vadur:22", "--from", "141.166.186.112"])
    assert r.exit_code == 0, r.output
    assert 'permitopen="vadur:22"' in r.output and "Nothing written (dry run)" in r.output
    assert not (tmp_path / ".ssh" / "authorized_keys").exists()
    r = CliRunner().invoke(cli, ["gate", "jump-key", PUB, "--to", "vadur:22", "--from", "141.166.186.112", "--apply"])
    assert r.exit_code == 0 and "Written." in r.output
    assert CliRunner().invoke(cli, ["gate", "jump-key", PUB, "--to", "vadur:22"]).exit_code != 0     # no --from
    assert CliRunner().invoke(cli, ["gate", "jump-key", PUB, "--to", "vadur", "--from-anywhere"]).exit_code != 0


def test_a_gate_through_a_jump_is_asked_as_any_other(tmp_path):
    s = _site(tmp_path, gate_jump="zeus@jonimitchell")
    seen = {}

    def run_(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        return subprocess.CompletedProcess(argv, 0, '{"ok": true, "jobs": []}\n', "")
    assert remote.call(s, "jobs", "jdoe", run_=run_) == {"ok": True, "jobs": []}
    assert any(a.startswith("ProxyCommand=") for a in seen["argv"])
    assert "SHELL" not in seen["env"]                # so ssh runs the ProxyCommand with /bin/sh

    def unknown(argv, **kw):
        return subprocess.CompletedProcess(argv, 255, "", "No ED25519 host key is known for vadur and you have "
                                           "requested strict checking.\nHost key verification failed.\n"
                                           "Connection closed by UNKNOWN port 65535\n")
    doc = remote.call(s, "jobs", "jdoe", run_=unknown)
    assert not doc["ok"] and doc["where"] == "ssh"
    assert "through zeus@jonimitchell: No ED25519 host key is known for vadur" in doc["error"]
    assert "jonimitchell's and vadur's" in doc["error"]


# -- the hub's sync timer ---------------------------------------------------------------------------

def test_how_often_the_hub_reads_its_sites(tmp_path):
    assert [T.on_calendar(n) for n in (2, 3, 5, 10, 20, 60)] == [
        "*-*-* *:00/2:00", "*-*-* *:02/3:00", "*-*-* *:02/5:00", "*-*-* *:02/10:00", "*-*-* *:02/20:00",
        "*-*-* *:02:00"]
    assert T.interval({"hub": {"sync_interval_minutes": 5}}) == 5
    for bad in (7, 0, 90, "5", True, 5.0):
        assert T.interval({"hub": {"sync_interval_minutes": bad}}) is None
        assert T.config_problems({"hub": {"sync_interval_minutes": bad}})
    assert T.config_problems({"hub": {}}) == [] and T.config_problems({}) == []
    path = tmp_path / "nomad-sync.timer.d" / "interval.conf"
    p = T.plan({"hub": {"sync_interval_minutes": 5}}, "/etc/nomad/nomad.toml", path=path)
    assert p["every"] == 5 and not p["same"] and "OnCalendar=\nOnCalendar=*-*-* *:02/5:00\n" in p["text"]
    # what hub_sync_every.py (the bridge before 1.40.0) wrote is the same setting
    path.parent.mkdir()
    path.write_text("# From [hub] sync_interval_minutes = 5 in /etc/nomad/nomad.toml (hub_sync_every.py)\n"
                    "[Timer]\nOnCalendar=\nOnCalendar=*-*-* *:02/5:00\n")
    assert T.plan({"hub": {"sync_interval_minutes": 5}}, "x", path=path)["same"]
    assert not T.plan({"hub": {"sync_interval_minutes": 10}}, "x", path=path)["same"]
    assert "no [hub] sync_interval_minutes" in T.plan({"hub": {}}, "x", path=path)["problem"]
    assert "not a hub" in T.plan({}, "x", path=path)["problem"]
    # applied: the drop-in, then systemd reloaded and the timer restarted
    calls = []

    def run_(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "{ OnCalendar=*-*-* *:02/10:00 }\n" if "show" in argv else "", "")
    p = T.plan({"hub": {"sync_interval_minutes": 10}}, "x", path=path)
    assert T.apply(p, run_=run_) == "{ OnCalendar=*-*-* *:02/10:00 }"
    assert "*:02/10:00" in path.read_text() and [c[1] for c in calls] == ["cat", "daemon-reload", "restart", "show"]

    def no_unit(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "No files found")
    with pytest.raises(RuntimeError, match="no nomad-sync.timer here"):
        T.apply(p, run_=no_unit)


def test_nomad_sync_timer(tmp_path, monkeypatch):
    from nomad.cli import cli
    path = tmp_path / "interval.conf"
    monkeypatch.setattr(T, "dropin_path", lambda unit=T.UNIT: path)
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[hub]\nsync_interval_minutes = 5\noutput_db = "/x/combined.db"\n')
    r = CliRunner().invoke(cli, ["-c", str(cfg), "sync", "timer"])
    assert r.exit_code == 0, r.output
    assert "Would write" in r.output and "OnCalendar=*-*-* *:02/5:00" in r.output and not path.exists()
    path.write_text(T.dropin_text(5, str(cfg)))
    assert "nothing to change" in CliRunner().invoke(cli, ["-c", str(cfg), "sync", "timer"]).output
    cfg.write_text('[hub]\nsync_interval_minutes = 7\n')
    r = CliRunner().invoke(cli, ["-c", str(cfg), "sync", "timer"])
    assert r.exit_code != 0 and "divides an hour" in r.output
    assert "divides an hour" in CliRunner().invoke(cli, ["-c", str(cfg), "config", "check"]).output
    assert CliRunner().invoke(cli, ["-c", str(cfg), "sync", "timer", "--unit", "x;y"]).exit_code != 0
