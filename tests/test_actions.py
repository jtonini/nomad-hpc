# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Console's actions: the catalog, the runner, a site's agent and the
hub's side of the ssh connection (with a stand-in ssh)."""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad import actions
from nomad.actions import agent, remote, runner, spec
from nomad.actions.spec import ActionError

PUB = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMzWKLZUIRCZZhOLPhAKx0W+Iea9zEn860icnFHfzS6a nomad-actions@hub")


def _cli():
    # Imported when used, not when the tests are collected: other test
    # modules put stand-ins in sys.modules before nomad.cli first loads.
    from nomad.cli import cli
    return cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# --- the catalog -------------------------------------------------------------------------

def test_every_action_is_a_real_command_with_its_options():
    r = CliRunner()
    for a in spec.ACTIONS:
        words = []
        for w in a.argv:              # the command's words, up to its first option
            if w.startswith("--"):
                break
            words.append(w)
        res = r.invoke(_cli(), [*words, "--help"])
        assert res.exit_code == 0, (a.name, res.output)
        for w in [w for w in a.argv if w.startswith("--")] + [p.flag for p in a.params if p.flag]:
            assert w in res.output, (a.name, w)
        if a.hub_db:
            assert "--db" in res.output, a.name


def test_check_parameters():
    a = spec.get("collectors")
    assert spec.check(a, {}) == {"days": 7}
    assert spec.check(a, {"days": "30"}) == {"days": 30}
    for bad in ({"days": 0}, {"days": 91}, {"days": "7 --db /etc/passwd"}, {"days": True}, {"days": 1.5}):
        with pytest.raises(ActionError):
            spec.check(a, bad)
    with pytest.raises(ActionError, match="takes no parameter db"):
        spec.check(a, {"db": "/etc/shadow"})
    with pytest.raises(ActionError):
        spec.check(a, ["days", 7])
    roles = spec.get("console.roles")
    assert spec.check(roles, {"netid": "jdoe"}) == {"netid": "jdoe"}
    for bad in ("-x", "--db=x", "a b", "a;b", "../x", ".x", "x" * 65, "j\ndoe", "jdoe\n", "jdoe\r"):
        with pytest.raises(ActionError):
            spec.check(roles, {"netid": bad})
    assert spec.check(spec.get("lab.show"), {"lab": "pi1$"}) == {"lab": "pi1$"}
    with pytest.raises(ActionError):
        spec.check(spec.get("alerts"), {"severity": "all"})
    with pytest.raises(ActionError, match="no action"):
        spec.get("rm")


def test_check_sites_and_periods():
    ins = spec.get("insights.brief")
    with pytest.raises(ActionError, match="needs site"):
        spec.check(ins, {}, sites=["c1"])
    with pytest.raises(ActionError, match="not one of the hub's sites"):
        spec.check(ins, {"site": "c2"}, sites=["c1"])
    assert spec.check(ins, {"site": "c1"}, sites=["c1"]) == {"site": "c1", "hours": 24}
    rep = spec.get("usage.report")
    ok = {"from": "2025-10-01", "to": "2026-10-01", "cluster": "c1"}
    assert spec.check(rep, ok, sites=["c1"])["from"] == "2025-10-01"
    for bad in ({**ok, "from": "2026-10-01"}, {**ok, "to": "2026-02-30"}, {**ok, "from": "20251001"},
                {**ok, "from": "2015-01-01"},
                {**ok, "to": (date.today() + timedelta(days=5)).isoformat()}):
        with pytest.raises(ActionError):
            spec.check(rep, bad, sites=["c1"])


def test_catalog_is_read_only_json():
    cat = actions.catalog()
    assert json.loads(json.dumps(cat)) == cat
    assert all(a["read_only"] for a in cat)
    assert {a["name"] for a in cat} >= {"collectors", "usage.report", "syscheck"}


# --- building and running a command ------------------------------------------------------

def test_argv_positional_after_double_dash():
    a = spec.get("console.roles")
    argv = runner.build_argv(a, {"netid": "jdoe"}, hub_db=Path("/h/combined.db"))
    assert argv[:3] == [sys.executable, "-m", "nomad.cli"]
    assert argv[3:] == ["console", "roles", "--mask", "--db", "/h/combined.db", "--", "jdoe"]
    argv = runner.build_argv(spec.get("collectors"), {"days": 3})
    assert argv[3:] == ["collectors", "--days", "3"]          # no hub database on a site
    with pytest.raises(ValueError):
        runner.build_argv(spec.get("usage.report"), {"from": "2025-01-01", "to": "2025-02-01", "cluster": "c"})


def test_run_argv_time_limit_and_cancel(tmp_path):
    # The child and what it started are stopped together.
    code = ("import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)']); print('started', flush=True); time.sleep(30)")
    t = time.time()
    res = runner.run_argv([sys.executable, "-c", code], timeout=1.5)
    assert res["timed_out"] and time.time() - t < 15
    assert "started" in res["output"]
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    res = runner.run_argv([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60, cancel=cancel)
    assert res["cancelled"] and not res["timed_out"]


def test_run_argv_env_and_truncation(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/nowhere")
    monkeypatch.setattr(runner, "MAX_OUTPUT", 1000)
    res = runner.run_argv([sys.executable, "-c",
                           "import os; print(os.environ.get('PYTHONPATH')); print('x' * 5000)"], timeout=30)
    assert res["output"].startswith("None")
    assert res["truncated"] and len(res["output"]) == 1000


def test_run_action_version(home):
    res = runner.run_action(spec.get("version"), {})
    assert res["ok"] and "N" in res["output"] and res["action"] == "version"


def test_files_written_and_report_file(home, monkeypatch):
    def fake(argv, timeout, cancel=None, on_output=None, cwd=None):
        out = Path(argv[argv.index("--out") + 1])
        (out / "usage-c1.md").write_text("# report")
        return {"exit_code": 0, "output": "wrote usage-c1.md\n", "errors": "", "truncated": False,
                "timed_out": False, "cancelled": False}
    monkeypatch.setattr(runner, "run_argv", fake)
    (runner.reports_dir() / "old.md").write_text("earlier")
    res = runner.run_action(spec.get("usage.report"), {"from": "2025-01-01", "to": "2025-02-01", "cluster": "c1"})
    assert res["files"] == ["usage-c1.md"]
    assert stat.S_IMODE(runner.reports_dir().stat().st_mode) == 0o700
    assert runner.report_file("usage-c1.md").read_text() == "# report"
    (runner.reports_dir() / "link.md").symlink_to(home / "secret")
    (home / "secret").write_text("x")
    for bad in ("../secret", "a/b", ".hidden", "", "link.md", "missing.md", "x\x00y"):
        with pytest.raises(ValueError):
            runner.report_file(bad)


# --- a site's agent ----------------------------------------------------------------------

def serve(request, monkeypatch=None) -> tuple[int, dict]:
    out = io.StringIO()
    raw = request if isinstance(request, bytes) else json.dumps(request).encode()
    code = agent.serve(io.BytesIO(raw), out)
    text = out.getvalue()
    return code, json.loads(text[text.rindex(remote.MARKER) + len(remote.MARKER):])


def test_agent_runs_a_site_action(home, monkeypatch):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "nomad agent")
    monkeypatch.setenv("SSH_CLIENT", "10.0.0.9 5555 22")
    code, reply = serve({"v": 1, "action": "version"})
    assert code == 0 and reply["ok"] and reply["restricted"] is True and reply["v"] == 1
    log = (home / ".local/share/nomad/agent.log").read_text().splitlines()
    assert json.loads(log[-1])["from"] == "10.0.0.9" and json.loads(log[-1])["action"] == "version"
    assert stat.S_IMODE((home / ".local/share/nomad/agent.log").stat().st_mode) == 0o600


def test_agent_refuses(home, monkeypatch):
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    for req, why in (({"v": 1, "action": "usage.report", "params": {}}, "runs on the hub"),
                     ({"v": 1, "action": "bash"}, "no action"),
                     ({"v": 2, "action": "version"}, "version"),
                     ({"v": 1, "action": "collectors", "params": {"db": "/etc/shadow"}}, "no parameter"),
                     (b"not json", "not JSON"),
                     (b"x" * (agent.MAX_REQUEST + 1), "too large"),
                     ([1, 2], "version")):
        code, reply = serve(req)
        assert code == 2 and not reply["ok"] and why in reply["error"], (req, reply)
        assert reply["restricted"] is False


def test_agent_one_at_a_time(home, monkeypatch):
    import fcntl
    held = os.open(agent._data_dir() / "agent.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    monkeypatch.setattr(agent, "_slot", lambda wait=0.5, _f=agent._slot: _f(0.5))
    try:
        code, reply = serve({"v": 1, "action": "version"})
    finally:
        os.close(held)
    assert code == 2 and "busy" in reply["error"]


# --- installing the hub's key on a site --------------------------------------------------

def test_install_key(home):
    ak = home / ".ssh" / "authorized_keys"
    status, lines = agent.install_key(PUB)
    assert status == "would-change" and not ak.exists()
    status, _ = agent.install_key(PUB, apply=True)
    line = ak.read_text().strip()
    assert status == "changed" and line.startswith(
        'restrict,command="cd / && exec /usr/bin/env -u PYTHONHOME -u PYTHONPATH ')
    assert line.endswith(PUB) and f"{sys.executable} -m nomad.cli agent\"" in line
    assert stat.S_IMODE(ak.stat().st_mode) == 0o600
    assert agent.install_key(PUB, apply=True)[0] == "present"
    # The same key without the restriction (added by hand) is replaced, others kept.
    ak.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKeyOtherKeyOtherKeyOtherKeyOtherKey00 other\n" + PUB + "\n")
    status, lines = agent.install_key(PUB, apply=True, from_pattern="10.0.0.5")
    text = ak.read_text().splitlines()
    assert status == "changed" and len(text) == 2 and text[0].endswith(" other")
    assert ',from="10.0.0.5" ' in text[1]
    assert any(p.name.startswith("authorized_keys.bak-nomad-agent-") for p in ak.parent.iterdir())


def test_install_key_refuses(home):
    for bad in ("ssh-ed25519", "ssh-dss AAAA x", "ssh-ed25519 !!!notbase64 x",
                "ssh-rsa AAAAC3NzaC1lZDI1NTE5AAAAIMzWKLZUIRCZZhOLPhAKx0W+Iea9zEn860icnFHfzS6a x",
                PUB.replace("nomad-actions@hub", 'x"y'), PUB + " extra words"):
        with pytest.raises(ValueError):
            agent.key_line(bad)
    with pytest.raises(ValueError):
        agent.key_line(PUB, from_pattern='x" command="sh')
    ak = home / ".ssh" / "authorized_keys"
    ak.parent.mkdir()
    (home / "elsewhere").write_text("")
    ak.symlink_to(home / "elsewhere")
    with pytest.raises(ValueError, match="link"):
        agent.install_key(PUB, apply=True)


def test_install_key_cli(home):
    res = CliRunner().invoke(_cli(), ["agent", "install-key", PUB])
    assert res.exit_code == 0 and "dry run" in res.output


# --- the hub's side ----------------------------------------------------------------------

def test_ssh_argv_and_reply():
    argv = remote.ssh_argv({"host": "h1", "user": "svc", "port": 2222}, Path("/k"))
    for opt in ("IdentitiesOnly=yes", "BatchMode=yes", "ControlPath=none", "ControlMaster=no",
                "StrictHostKeyChecking=yes", "ClearAllForwardings=yes"):
        assert opt in argv
    assert argv[:2] == ["ssh", "-T"] and argv[-4:] == ["--", "svc@h1", "nomad", "agent"]
    assert "-p" in argv and "2222" in argv
    reply = remote.parse_reply("Welcome to h1!\n  moo\n" + remote.MARKER + '\n{"v": 1, "ok": true}\n')
    assert reply == {"v": 1, "ok": True}
    for bad in ("no marker", remote.MARKER + "\nnot json", remote.MARKER + '\n{"v": 9}'):
        with pytest.raises(ActionError):
            remote.parse_reply(bad)


@pytest.fixture
def hub(home, monkeypatch):
    """A hub with one site, reached through a stand-in ssh that runs this
    nomad's agent the way sshd's forced command would."""
    cfg = home / ".config" / "nomad"
    cfg.mkdir(parents=True)
    (cfg / "sync.toml").write_text('[[sites]]\nname = "c1"\nhost = "h1"\nuser = "svc"\ndb_path = "x"\n')
    bindir = home / "bin"
    bindir.mkdir()
    fake = bindir / "ssh"
    fake.write_text(f"#!/bin/sh\necho 'a banner'\nSSH_ORIGINAL_COMMAND='nomad agent' "
                    f"SSH_CLIENT='10.0.0.1 1 22' exec {sys.executable} -m nomad.cli agent\n")
    fake.chmod(0o755)
    keygen = bindir / "ssh-keygen"
    keygen.write_text("#!/bin/sh\nwhile [ $# -gt 1 ]; do shift; done\n"
                      f"echo key > \"$1\"; echo '{PUB}' > \"$1.pub\"\n")
    keygen.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return home


def test_round_trip_through_the_agent(hub):
    assert actions.targets() == {"hub": False, "sites": ["c1"]}
    res = actions.run("version", site="c1")
    assert res["ok"] and res["site"] == "c1" and res["restricted"] is True and "N" in res["output"]
    key = hub / ".config" / "nomad" / "agent" / "id_ed25519"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
    with pytest.raises(ActionError, match="not one of the hub's sites"):
        actions.run("version", site="c9")
    with pytest.raises(ActionError, match="runs on the hub"):
        actions.run("usage.report", {"from": "2025-01-01", "to": "2025-02-01", "cluster": "c1"}, site="c1")
    with pytest.raises(ActionError, match="isn't the hub"):
        actions.run("collectors")
    with pytest.raises(ActionError, match="runs on a site"):
        actions.run("syscheck")


def test_cli_run_on_a_site(hub):
    res = CliRunner().invoke(_cli(), ["actions", "run", "version", "--site", "c1"])
    assert res.exit_code == 0, res.output
    res = CliRunner().invoke(_cli(), ["actions", "run", "collectors", "--site", "c1", "-p", "days=0"])
    assert res.exit_code == 1 and "between 1 and 90" in res.output
    res = CliRunner().invoke(_cli(), ["actions", "list", "--json"])
    assert json.loads(res.output)[0]["name"] == "version"


def test_cancel_a_site_action(hub):
    fake = hub / "bin" / "ssh"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    t = time.time()
    res = actions.run("version", site="c1", cancel=cancel)
    assert res["cancelled"] and not res["ok"] and time.time() - t < 10


def test_site_without_the_agent(hub):
    (hub / "bin" / "ssh").write_text("#!/bin/sh\necho 'Permission denied (publickey).' >&2\nexit 255\n")
    res = actions.run("version", site="c1")
    assert not res["ok"] and "Permission denied" in res["error"]


def test_run_here(home):
    res = actions.run("version", here=True)
    assert res["ok"]
    with pytest.raises(ActionError, match="hub only"):
        actions.run("lab.show", here=True)


def test_no_shell_anywhere():
    src = "".join((Path(runner.__file__).parent / f).read_text()
                  for f in ("runner.py", "remote.py", "agent.py", "spec.py", "__init__.py", "cli.py"))
    assert "shell=True" not in src and "os.system" not in src and "os.popen" not in src
    assert subprocess.list2cmdline  # (imported for the check above only)


def test_forced_command_quotes_the_interpreter(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/opt/my python/bin/python3")
    assert agent.forced_command().endswith("'/opt/my python/bin/python3' -m nomad.cli agent")
    monkeypatch.setattr(sys, "executable", '/opt/x"y/python')
    with pytest.raises(ValueError):
        agent.forced_command()
    with pytest.raises(ValueError):
        agent.key_line(PUB, from_pattern="10.0.0.5\n")


def test_agent_queue_and_silent_connections(home, monkeypatch):
    import fcntl
    run = os.open(agent._data_dir() / "agent.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    queue = os.open(agent._data_dir() / "agent.queue", os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(run, fcntl.LOCK_EX)
    fcntl.flock(queue, fcntl.LOCK_EX)
    try:
        t = time.time()
        code, reply = serve({"v": 1, "action": "version"})
        assert code == 2 and "busy" in reply["error"] and time.time() - t < 2      # refused at once
    finally:
        os.close(run)
        os.close(queue)
    # The one waiting gets the slot when it frees.
    run = os.open(agent._data_dir() / "agent.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(run, fcntl.LOCK_EX)
    threading.Timer(1.0, os.close, (run,)).start()
    code, reply = serve({"v": 1, "action": "version"})
    assert code == 0 and reply["ok"]
    log = [json.loads(x) for x in (home / ".local/share/nomad/agent.log").read_text().splitlines()]
    assert [x["ok"] for x in log if x["action"] == "version"] == [False, True]    # the refusal is logged
    # A connection that never sends its request.
    monkeypatch.setattr(agent, "READ_SECONDS", 0.5)
    r, w = os.pipe()
    out = io.StringIO()
    try:
        code = agent.serve(os.fdopen(r, "rb"), out)
    finally:
        os.close(w)
    assert code == 2 and "no request within" in out.getvalue()
    code, reply = serve(b"[" * 30000 + b"]" * 30000)
    assert code == 2 and "not JSON" in reply["error"]


def test_reply_is_data(hub):
    fake = hub / "bin" / "ssh"

    def site_says(text):
        p = hub / "reply.txt"
        p.write_text(text)
        fake.write_text(f"#!/bin/sh\ncat > /dev/null\ncat '{p}'\n")

    site_says(remote.MARKER + "\n" + json.dumps({"v": 1, "ok": True, "output": 5, "files": ["../../x"],
                                                   "restricted": True, "exit_code": "0", "site": "evil"}))
    res = actions.run("version", site="c1")
    assert res["ok"] and res["output"] == "" and "files" not in res and res["exit_code"] is None
    assert res["site"] == "c1"
    # The marker inside the output doesn't confuse the reading.
    body = {"v": 1, "ok": True, "restricted": True, "output": "x\n" + remote.MARKER + "\n{}"}
    site_says("banner\n" + remote.MARKER + "\n" + json.dumps(body) + "\n")
    assert actions.run("version", site="c1")["output"].endswith("{}")
    # A key line without command=: the answer is not used.
    site_says(remote.MARKER + "\n" + json.dumps({"v": 1, "ok": True, "restricted": False, "output": "x"}))
    res = actions.run("version", site="c1")
    assert not res["ok"] and "more than `nomad agent`" in res["error"] and "output" not in res
    site_says(remote.MARKER + "\n" + "[" * 50000 + "]" * 50000)
    assert "could not be read" in actions.run("version", site="c1")["error"]
    site_says(remote.MARKER + "\n" + '{"v": 1, "ok": true, "restricted": true, "seconds": NaN}')
    assert "could not be read" in actions.run("version", site="c1")["error"]
    site_says(remote.MARKER + "\n" + '{"v": 1, "ok": true, "restricted": true, "seconds": 1' + "0" * 400
              + ', "output": "a\\ud800b", "exit_code": 99999}')
    res = actions.run("version", site="c1")
    assert res["seconds"] is None and res["output"] == "a?b" and res["exit_code"] is None
    json.dumps(res, allow_nan=False).encode("utf-8")


def test_reply_too_large(hub, monkeypatch):
    monkeypatch.setattr(remote, "MAX_REPLY", 100000)
    (hub / "bin" / "ssh").write_text("#!/bin/sh\ncat > /dev/null\nexec yes xxxxxxxxxxxxxxxx\n")
    t = time.time()
    res = actions.run("version", site="c1")
    assert not res["ok"] and "larger" in res["error"] and time.time() - t < 10
