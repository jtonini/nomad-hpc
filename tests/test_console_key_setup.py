# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad console --key-setup`: a key just for the Console, so that
`ssh -N nomad-console` on a person's own computer opens the tunnel and the
browser with no password.

The setup runs on their computer (bash -c "$(ssh NETID@workstation nomad
console --key-setup)"); here it runs with a fake ssh, in a home of its own.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad.cli import cli
from nomad.console import launch as L

REAL_SSH = shutil.which("ssh")
needs_ssh = pytest.mark.skipif(REAL_SSH is None or shutil.which("bash") is None,
                               reason="needs ssh and bash")

CONSOLE = L.Target("jdoe@console.example.edu")


def _script(**kw) -> str:
    return L.key_setup_script(CONSOLE, login_host=kw.pop("login_host", "labws1.example.edu"),
                              **kw)


def _sandbox(tmp_path, *, reachable=True, system="Linux", config=None):
    """A home with a Console key already made, and a PATH whose ssh answers
    the reachability probe as told (ssh -G is the real one), whose
    ssh-copy-id and xdg-open only record, and whose uname says `system`."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test",
                    "-f", str(home / ".ssh" / "nomad_console")], check=True)
    if config is not None:
        (home / ".ssh" / "config").write_text(config)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "calls.log"
    answer = ("echo 'jdoe@console.example.edu: Permission denied (publickey).' >&2; exit 255"
              if reachable else
              "echo 'ssh: connect to host console.example.edu port 22: Connection timed out' >&2; exit 255")
    real_g = (f'exec {REAL_SSH} "$@"' if system == "Linux" else "exit 0")
    (bin_ / "ssh").write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = -G ]; then {real_g}; fi\n'
        f'for a in "$@"; do [ "$a" = true ] && {{ {answer}; }}; done\n'
        "exit 0\n")
    (bin_ / "ssh-copy-id").write_text(f'#!/bin/sh\necho "ssh-copy-id $*" >> {log}\n')
    (bin_ / "uname").write_text(f"#!/bin/sh\necho {system}\n")
    (bin_ / "xdg-open").write_text(f'#!/bin/sh\necho "xdg-open $*" >> {log}\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    env = dict(os.environ, HOME=str(home), PATH=f"{bin_}:{os.environ['PATH']}")
    return home, env, log


def _run(script, env):
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60)


# -- what the remote machine prints ------------------------------------------

def test_the_short_way_comes_first(monkeypatch):
    monkeypatch.setattr(L, "_user", lambda: "jdoe")
    monkeypatch.setenv(L.ENV_COMMAND, "/opt/sw/bin/nomad")
    monkeypatch.setenv(L.ENV_CONTACT, "the lab's admin")
    lines = L.instructions(CONSOLE, login_host="labws1.example.edu")
    assert lines[2].strip() == "ssh -N nomad-console"
    text = "\n".join(lines)
    line = 'bash -c "$(ssh jdoe@labws1.example.edu /opt/sw/bin/nomad console --key-setup)"'
    assert line in text
    assert "<(" not in text           # ssh in a process substitution can't ask for a password
    assert "send the line it prints to the lab's admin." in text
    assert "ssh -N -L 8000:localhost:8000" in text          # still there, for Windows


def test_nomad_is_named_by_its_full_path_when_known(monkeypatch):
    monkeypatch.delenv(L.ENV_COMMAND, raising=False)
    monkeypatch.setattr(L.sys, "argv", ["/opt/conda/bin/nomad", "console"])
    assert L.nomad_command() == "/opt/conda/bin/nomad"
    monkeypatch.setattr(L.sys, "argv", ["launch.py"])
    assert L.nomad_command() == "nomad"


def test_the_setup_carries_this_sites_names():
    s = _script(contact="the lab's admin")
    assert "NETID=jdoe\n" in s and "CONSOLE_HOST=console.example.edu\n" in s
    assert "VIA=labws1.example.edu\n" in s and "PORT=8000\n" in s
    assert "CONTACT='the lab'\"'\"'s admin'\n" in s
    assert "@@" not in s
    assert subprocess.run(["bash", "-n"], input=s, text=True).returncode == 0


def test_no_hop_from_the_consoles_own_machine():
    assert "VIA=''\n" in _script(login_host="console.example.edu")


@pytest.mark.parametrize("dest", ["jdoe@console.example.edu;reboot", "j$(x)@console"])
def test_names_a_shell_would_read_are_refused(dest):
    with pytest.raises(L.LaunchError):
        L.key_setup_script(L.Target(dest), login_host="labws1")


def test_cli_prints_the_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(L, "SAVED", tmp_path / "none.json")
    monkeypatch.setenv(L.ENV_HOST, "console.example.edu")
    monkeypatch.setenv(L.ENV_LOGIN, "labws1.example.edu")
    monkeypatch.setattr(L, "_user", lambda: "jdoe")
    r = CliRunner().invoke(cli, ["console", "--key-setup"])
    assert r.exit_code == 0, r.output
    assert r.output.startswith("#!/bin/bash") and "NETID=jdoe\n" in r.output


def test_the_tunnel_uses_the_key_when_there_is_one(monkeypatch, tmp_path):
    monkeypatch.setattr(L.Path, "home", classmethod(lambda cls: tmp_path))
    argv = L.ssh_command("ssh", CONSOLE, 8000)
    assert not any(a.startswith("IdentityFile=") for a in argv)
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "nomad_console").write_text("key")
    argv = L.ssh_command("ssh", CONSOLE, 8000)
    assert f"IdentityFile={tmp_path / '.ssh' / 'nomad_console'}" in argv
    assert argv[-1] == "jdoe@console.example.edu"


# -- the setup, run --------------------------------------------------------------

@needs_ssh
def test_on_campus_it_goes_straight_and_opens_the_browser(tmp_path):
    before = "User someone\nServerAliveInterval 30\n\nHost work\n    HostName work.example.org\n"
    home, env, log = _sandbox(tmp_path, config=before)
    r = _run(_script(), env)
    assert r.returncode == 0, r.stderr
    cfg = (home / ".ssh" / "config").read_text()
    assert cfg.splitlines()[1] == "Host nomad-console"
    assert "ProxyJump" not in cfg and "UseKeychain" not in cfg
    assert cfg.endswith("Host *\n" + before)                 # the rest, as it was
    assert "Send this one line" in r.stdout
    assert (home / ".ssh" / "nomad_console.pub").read_text().strip() in r.stdout
    # What ssh -G makes of it: the entry's settings, and the old ones elsewhere.
    g = subprocess.run([REAL_SSH, "-G", "-F", str(home / ".ssh" / "config"), "nomad-console"],
                       capture_output=True, text=True).stdout
    assert "user jdoe" in g and "hostname console.example.edu" in g
    assert "localforward 8000 [localhost]:8000" in g
    g = subprocess.run([REAL_SSH, "-G", "-F", str(home / ".ssh" / "config"), "work"],
                       capture_output=True, text=True).stdout
    assert "user someone" in g and "serveraliveinterval 30" in g
    # The command ssh runs once the tunnel is up: the message, then the browser.
    local = next(x.strip().split(" ", 1)[1] for x in cfg.splitlines()
                 if x.strip().startswith("LocalCommand "))
    out = subprocess.run(["sh", "-c", local], env=env, capture_output=True, text=True)
    assert out.stdout.startswith("Tunnel open: http://localhost:8000")
    subprocess.run(["sleep", "0.3"])
    assert log.read_text().strip() == "xdg-open http://localhost:8000"


@needs_ssh
def test_off_campus_it_goes_through_the_workstation(tmp_path):
    home, env, log = _sandbox(tmp_path, reachable=False)
    r = _run(_script(), env)
    assert r.returncode == 0, r.stderr
    cfg = (home / ".ssh" / "config").read_text()
    assert "    ProxyJump nomad-console-via\n" in cfg
    assert "Host nomad-console-via\n    HostName labws1.example.edu\n    User jdoe\n" in cfg
    assert "ssh-copy-id -i " in log.read_text() and "jdoe@labws1.example.edu" in log.read_text()


@needs_ssh
def test_running_it_again_replaces_its_own_entry(tmp_path):
    before = "Host work\n    HostName work.example.org\n"
    home, env, _ = _sandbox(tmp_path, config=before)
    assert _run(_script(), env).returncode == 0
    assert _run(_script(), env).returncode == 0
    cfg = (home / ".ssh" / "config").read_text()
    assert cfg.count("Host nomad-console\n") == 1 and cfg.count("Host *\n") == 1
    assert cfg.endswith("Host *\n" + before)


@needs_ssh
def test_an_entry_from_the_first_setup_script_is_replaced(tmp_path):
    old = ("# NOMAD Console (added 2026-10-06 by nomad_console_key_setup.sh)\n"
           "Host nomad-console\n    HostName console.example.edu\n    User jdoe\n"
           "    LocalCommand echo \"Tunnel open\"\n\nHost *\nUser someone\n")
    home, env, _ = _sandbox(tmp_path, config=old)
    assert _run(_script(), env).returncode == 0
    cfg = (home / ".ssh" / "config").read_text()
    assert cfg.count("Host nomad-console\n") == 1 and "xdg-open" in cfg
    assert cfg.endswith("Host *\nUser someone\n")


@needs_ssh
def test_an_entry_it_did_not_write_is_left_alone(tmp_path):
    mine = "Host nomad-console\n    HostName elsewhere.example.org\n"
    home, env, _ = _sandbox(tmp_path, config=mine)
    r = _run(_script(), env)
    assert r.returncode == 1 and "did not write" in r.stderr
    assert (home / ".ssh" / "config").read_text() == mine


@needs_ssh
def test_on_a_mac_the_browser_opens_with_open_and_the_keychain_keeps_the_passphrase(tmp_path):
    home, env, _ = _sandbox(tmp_path, system="Darwin")
    assert _run(_script(), env).returncode == 0
    cfg = (home / ".ssh" / "config").read_text()
    assert "    UseKeychain yes\n" in cfg
    assert '; open http://localhost:8000\n' in cfg


@needs_ssh
def test_a_key_already_in_is_said(tmp_path):
    home, env, _ = _sandbox(tmp_path)
    (tmp_path / "bin" / "ssh").write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = -G ]; then exec {REAL_SSH} "$@"; fi\n'
        "echo 'This login is for the NOMAD Console tunnel only.'\n")
    r = _run(_script(), env)
    assert r.returncode == 0 and "Your key is already in" in r.stdout
    assert "Send this one line" not in r.stdout


def test_at_the_desk_without_a_key_it_says_how_to_skip_the_password(monkeypatch, tmp_path):
    monkeypatch.setattr(L.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv(L.ENV_COMMAND, "/opt/sw/bin/nomad")
    monkeypatch.setattr(L, "save", lambda *a, **k: False)
    false = shutil.which("false")
    out = []
    L.launch(CONSOLE, out=out.append, here=True, ssh=false, timeout=5)
    assert "key-setup" in out[1] and "/opt/sw/bin/nomad" in out[1]
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "nomad_console").write_text("key")
    out = []
    L.launch(CONSOLE, out=out.append, here=True, ssh=false, timeout=5)
    assert not any("key-setup" in x for x in out)


@needs_ssh
def test_a_config_that_is_a_link_stays_one(tmp_path):
    home, env, _ = _sandbox(tmp_path)
    real = tmp_path / "dotfiles" / "ssh_config"
    real.parent.mkdir()
    real.write_text("Host work\n    HostName work.example.org\n")
    (home / ".ssh" / "config").symlink_to(real)
    assert _run(_script(), env).returncode == 0
    assert (home / ".ssh" / "config").is_symlink()
    assert real.read_text().splitlines()[1] == "Host nomad-console"


def test_a_name_for_this_machine_a_shell_would_read_is_refused():
    with pytest.raises(L.LaunchError):
        L.key_setup_script(CONSOLE, login_host="console.example.edu\nrm -rf ~")


@needs_ssh
def test_the_setup_line_runs_ssh_in_the_foreground_and_the_setup(tmp_path):
    """The printed line, run by a shell: ssh (here a fake that prints the
    setup) in a command substitution, the setup run by bash -c."""
    home, env, _ = _sandbox(tmp_path)
    fake = tmp_path / "bin" / "ssh"
    script = tmp_path / "setup.sh"
    script.write_text(_script())
    body = fake.read_text()
    fake.write_text(body.replace("exit 0\n", "") +
                    f'case "$*" in *--key-setup*) cat {script}; exit 0;; esac\nexit 0\n')
    line = 'bash -c "$(ssh jdoe@labws1.example.edu nomad console --key-setup)"'
    r = subprocess.run(["sh", "-c", line], env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "nomad-console entry is in place" in r.stdout
