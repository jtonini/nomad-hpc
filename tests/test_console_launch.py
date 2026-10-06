"""nomad console launch: the SSH tunnel to the Console, from the browser's computer."""
import http.server
import json
import os
import select
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

import nomad.console.launch as L
from nomad.cli import cli

posix_only = pytest.mark.skipif(os.name == "nt", reason="fake ssh is a POSIX script")


@pytest.fixture(autouse=True)
def saved_file(tmp_path, monkeypatch):
    path = tmp_path / "console_launch.json"
    monkeypatch.setattr(L, "SAVED", path)
    for var in (L.ENV_HOST, L.ENV_LOGIN, "SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(var, raising=False)
    return path


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- the ssh command ----------------------------------------------------------

def test_command_binds_this_computer_only_and_jumps_when_asked():
    t = L.Target("carol@mingus.example")
    argv = L.ssh_command("/usr/bin/ssh", t, 8000)
    assert argv[:2] == ["/usr/bin/ssh", "-N"]
    assert argv[argv.index("-L") + 1] == "127.0.0.1:8000:localhost:8000"
    assert "-J" not in argv and argv[-1] == "carol@mingus.example"

    t = L.Target("carol@mingus.example", via="carol@ws.example", remote_port=8080)
    argv = L.ssh_command("ssh", t, 9000)
    assert argv[argv.index("-L") + 1] == "127.0.0.1:9000:localhost:8080"
    assert argv[argv.index("-J") + 1] == "carol@ws.example"
    assert argv[-1] == "carol@mingus.example"


def test_command_keeps_its_own_connection_and_says_when_ready():
    opts = L.ssh_command("ssh", L.Target("u@h"), 8000)
    opts = {opts[i + 1] for i, a in enumerate(opts) if a == "-o"}
    assert {"ExitOnForwardFailure=yes", "ControlMaster=no", "ControlPath=none",
            "PermitLocalCommand=yes", f"LocalCommand=echo {L.READY}"} <= opts


def test_manual_command_reads_like_typed():
    text = L.manual_command(L.Target("u@h", via="u@j"), 8000)
    assert text == "ssh -N -L 8000:localhost:8000 -J u@j u@h" or os.name == "nt"


@pytest.mark.parametrize("bad", ["", "-oProxyCommand=evil", "u@h x", "u@\th"])
def test_names_that_are_not_machines_are_refused(bad):
    with pytest.raises(L.LaunchError):
        L.Target(bad).validate()
    with pytest.raises(L.LaunchError):
        L.Target("u@h", via=bad).validate()


@pytest.mark.parametrize("port", [0, -5, 70000, "x"])
def test_port_numbers_checked(port):
    with pytest.raises(L.LaunchError, match="Not a port number"):
        L.Target("u@h", remote_port=port).validate()
    with pytest.raises(L.LaunchError, match="Not a port number"):
        L.pick_local_port(port, 8000)


# -- local port ---------------------------------------------------------------

def test_local_port_prefers_the_consoles_number_then_any_free():
    p = free_port()
    assert L.pick_local_port(None, p) == p
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", p))
        busy.listen()
        other = L.pick_local_port(None, p)
        assert other != p and L.port_free(other)
        with pytest.raises(L.LaunchError, match="already in use"):
            L.pick_local_port(p, 8000)


@posix_only
def test_port_left_in_time_wait_is_free_again():
    """A browser connected when the last tunnel closed leaves the port in TIME_WAIT."""
    p = free_port()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", p))
    srv.listen()
    cli_sock = socket.create_connection(("127.0.0.1", p))
    conn, _ = srv.accept()
    conn.close()            # the listening side closes first: it keeps TIME_WAIT
    srv.close()
    cli_sock.close()
    time.sleep(0.1)
    assert L.port_free(p)


# -- probing ------------------------------------------------------------------

class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"console")

    def log_message(self, *a):
        pass


def serve_http(port):
    srv = http.server.HTTPServer(("127.0.0.1", port), _Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_probe_tells_down_tunnel_and_up_apart():
    port = free_port()
    assert L.probe(port) == "down"

    srv = serve_http(port)
    try:
        assert L.probe(port) == "up"
    finally:
        srv.shutdown()
        srv.server_close()

    # What ssh does when the far side refuses: accept here, then hang up.
    port = free_port()
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", port))
    lsock.listen()
    stop = threading.Event()

    def hang_up():
        lsock.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = lsock.accept()
                c.close()
            except OSError:
                pass
    th = threading.Thread(target=hang_up, daemon=True)
    th.start()
    try:
        assert L.probe(port, timeout=2) == "tunnel"
    finally:
        stop.set()
        th.join()
        lsock.close()


# -- waiting ------------------------------------------------------------------

class FakeProc:
    def __init__(self, exit_after=None):
        self.calls = 0
        self.exit_after = exit_after
        self.returncode = None

    def poll(self):
        self.calls += 1
        if self.exit_after is not None and self.calls > self.exit_after:
            self.returncode = 255
        return self.returncode


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run_wait(states, proc=None, ready=lambda: True, stopped=lambda: False, **kw):
    clock = Clock()
    seq = iter(states)
    last = [states[-1]]

    def probe(port):
        try:
            last[0] = next(seq)
        except StopIteration:
            pass
        return last[0]
    kw.setdefault("timeout", 300)
    kw.setdefault("no_console", 15)
    return L.wait_until_up(proc or FakeProc(), 1, ready=ready, stopped=stopped,
                           probe=probe, clock=clock, sleep=clock.sleep, **kw), clock.t


def test_wait_up_after_the_password():
    assert run_wait(["down"] * 20 + ["up"])[0] == "up"


def test_wait_ssh_gave_up():
    assert run_wait(["down"], proc=FakeProc(exit_after=3))[0] == "exited"


def test_wait_tunnel_but_no_console():
    assert run_wait(["down", "down", "tunnel"], no_console=5)[0] == "no-console"


def test_wait_tunnel_hiccup_resets():
    states = ["tunnel"] * 4 + ["down"] + ["tunnel"] * 4 + ["up"]
    assert run_wait(states, no_console=2.5)[0] == "up"


def test_wait_times_out():
    assert run_wait(["down"], timeout=10)[0] == "timeout"


def test_wait_stops_when_asked():
    assert run_wait(["down"], stopped=lambda: True)[0] == "stopped"


def test_an_answer_before_ssh_is_ready_is_not_trusted_at_once():
    """Something else on the port while ssh waits for the password."""
    flag = threading.Event()
    state, elapsed = run_wait(["up"], ready=flag.is_set, trust_after=10)
    assert state == "up" and elapsed >= 10          # only after it lasted

    calls = {"n": 0}

    def ready_soon():
        calls["n"] += 1
        return calls["n"] > 3
    state, elapsed = run_wait(["up"], ready=ready_soon, trust_after=10)
    assert state == "up" and elapsed < 10

    # ssh fails (it could not bind the port someone else took): reported, not "up".
    state, _ = run_wait(["up"], ready=flag.is_set, proc=FakeProc(exit_after=6),
                        trust_after=10)
    assert state == "exited"


# -- remembering --------------------------------------------------------------

def test_resolve_needs_a_machine_the_first_time(saved_file):
    with pytest.raises(L.LaunchError, match="Which machine"):
        L.resolve(None, None, None)


def test_save_and_reuse(saved_file):
    t = L.Target("carol@mingus.example", via="carol@ws.example")
    assert L.save(t) is True
    assert L.save(t) is False
    assert json.loads(saved_file.read_text())["destination"] == "carol@mingus.example"
    assert L.resolve(None, None, None) == t
    assert L.resolve(None, "other@jump", 8080) == L.Target("carol@mingus.example",
                                                           "other@jump", 8080)
    # A machine named on the command line wins, without the saved jump.
    assert L.resolve("zeus@mingus", None, None) == L.Target("zeus@mingus")


@pytest.mark.parametrize("content", ["{not json", '{"destination": 5}',
                                     '{"destination": "-oX=y"}',
                                     '{"destination": "u@h", "remote_port": 0}',
                                     '["u@h"]'])
def test_a_bad_saved_file_is_ignored(saved_file, content):
    saved_file.write_text(content)
    assert L.load_saved() is None


# -- stop signals -------------------------------------------------------------

@posix_only
def test_an_ignored_hangup_stays_ignored():
    """Under nohup, closing the window must not close the tunnel."""
    old = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        previous = L._catch_stop_signals()
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) == L._on_stop
        L._restore_signals(previous)
        assert signal.getsignal(signal.SIGTERM) != L._on_stop
    finally:
        signal.signal(signal.SIGHUP, old)


# -- the whole launch, with a stand-in for ssh --------------------------------

FAKE_SSH = textwrap.dedent('''\
    #!{python}
    import http.server, os, signal, socket, sys, threading, time
    mode = os.environ.get("FAKE_SSH_MODE", "serve")
    if os.environ.get("FAKE_SSH_IGNORE_INT"):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    pidfile = os.environ.get("FAKE_SSH_PIDFILE")
    if pidfile:
        open(pidfile, "w").write(str(os.getpid()))
    args = sys.argv[1:]
    spec = args[args.index("-L") + 1]
    port = int(spec.split(":")[1])
    local = [a.split("=", 1)[1] for a in args if a.startswith("LocalCommand=")]
    time.sleep(float(os.environ.get("FAKE_SSH_DELAY", "0.3")))   # the password
    if mode == "fail":
        print("ssh: connect to host: Connection refused", file=sys.stderr)
        sys.exit(255)
    if mode == "serve":
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    else:
        s = socket.socket(); s.bind(("127.0.0.1", port)); s.listen()
        def hang_up():
            while True:
                c, _ = s.accept(); c.close()
        threading.Thread(target=hang_up, daemon=True).start()
    if local and local[0].startswith("echo "):
        print(local[0][5:], flush=True)
    time.sleep(float(os.environ.get("FAKE_SSH_SECONDS", "1.5")))
''')


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    path = d / "ssh"
    path.write_text(FAKE_SSH.format(python=sys.executable))
    path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    return path


@posix_only
def test_launch_opens_saves_and_reports_when_the_tunnel_closes(fake_ssh, saved_file,
                                                               monkeypatch):
    opened = []
    monkeypatch.setattr(L.webbrowser, "open", lambda url: opened.append(url) or True)
    out = []
    port = free_port()
    code = L.launch(L.Target("carol@mingus.example"), local_port=port,
                    out=out.append, ssh=str(fake_ssh), here=True)
    text = "\n".join(out)
    assert code == 0
    assert opened == [f"http://127.0.0.1:{port}/"]
    assert f"NØMAÐ Console: http://127.0.0.1:{port}/" in text
    assert "Saved:" in text and saved_file.exists()
    assert "The tunnel closed" in text


@posix_only
def test_launch_when_ssh_cannot_log_in(fake_ssh, saved_file, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MODE", "fail")
    out = []
    code = L.launch(L.Target("carol@nowhere"), out=out.append, ssh=str(fake_ssh),
                    open_browser=False, here=True)
    assert code == 255
    assert "Check the machine name" in "\n".join(out)
    assert not saved_file.exists()          # a failed launch is not remembered


@posix_only
def test_something_else_on_the_port_is_not_taken_for_the_console(fake_ssh, saved_file,
                                                                 monkeypatch):
    """Another program takes the port while ssh waits for the password; ssh then
    fails to bind it. The launcher must report the failure, not that program."""
    port = free_port()
    other = serve_http(port)
    monkeypatch.setattr(L, "port_free", lambda p: True)
    monkeypatch.setenv("FAKE_SSH_MODE", "fail")
    monkeypatch.setenv("FAKE_SSH_DELAY", "2")
    out = []
    try:
        code = L.launch(L.Target("carol@mingus.example"), local_port=port, out=out.append,
                        ssh=str(fake_ssh), open_browser=False, here=True)
    finally:
        other.shutdown()
        other.server_close()
    text = "\n".join(out)
    assert code == 255
    assert "NØMAÐ Console:" not in text
    assert not saved_file.exists()


@posix_only
def test_launch_when_nothing_answers_through_the_tunnel(fake_ssh, saved_file, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MODE", "silent")
    monkeypatch.setenv("FAKE_SSH_SECONDS", "10")
    out = []
    code = L.launch(L.Target("carol@mingus.example"), out=out.append, ssh=str(fake_ssh),
                    open_browser=False, no_console=1, local_port=free_port(), here=True)
    assert code == 1
    assert "nothing answers" in "\n".join(out)
    assert not saved_file.exists()


def test_print_only_needs_no_ssh_here(saved_file, monkeypatch):
    monkeypatch.setattr(L.shutil, "which", lambda name: None)
    out = []
    assert L.launch(L.Target("u@h", via="u@j"), print_only=True, out=out.append,
                    local_port=free_port()) == 0
    assert out[0].startswith("ssh -N -L ") and "-J u@j" in out[0]
    assert out[1].startswith("then open http://localhost:")


def test_no_ssh_client(monkeypatch):
    monkeypatch.setattr(L.shutil, "which", lambda name: None)
    with pytest.raises(L.LaunchError, match="OpenSSH Client"):
        L.launch(L.Target("u@h"), out=lambda s: None, here=True)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@posix_only
@pytest.mark.parametrize("how", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_stopping_the_launcher_closes_the_tunnel(fake_ssh, tmp_path, how):
    """Ctrl-C, a kill, or the window closing: the launcher closes ssh itself.
    The stand-in ssh ignores SIGINT, so only the launcher can end it."""
    pidfile = tmp_path / "ssh.pid"
    env = dict(os.environ, HOME=str(tmp_path), FAKE_SSH_SECONDS="60",
               FAKE_SSH_PIDFILE=str(pidfile), FAKE_SSH_IGNORE_INT="1",
               PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "nomad.console.launch", "carol@mingus.example",
         "--no-browser", "--here", "--port", str(free_port())],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        start_new_session=True, cwd=Path(__file__).resolve().parents[1])
    ssh_pid = None
    try:
        seen = b""
        deadline = time.time() + 20
        while b"Ctrl-C" not in seen and time.time() < deadline:
            r, _, _ = select.select([proc.stdout], [], [], 0.5)
            if r:
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                seen += chunk
        assert "NØMAÐ Console".encode() in seen, seen
        ssh_pid = int(pidfile.read_text())
        sig = getattr(signal, how)
        if how == "SIGINT":
            os.killpg(proc.pid, sig)        # what Ctrl-C in a terminal does
        else:
            os.kill(proc.pid, sig)
        rest, _ = proc.communicate(timeout=20)
        assert b"Tunnel closed." in rest
        for _ in range(50):
            if not _alive(ssh_pid):
                break
            time.sleep(0.1)
        assert not _alive(ssh_pid), "ssh was left running"
    finally:
        for pid in (ssh_pid, proc.pid):
            if pid and _alive(pid):
                os.kill(pid, signal.SIGKILL)
        if proc.poll() is None:
            proc.wait(timeout=5)


# -- the command line ---------------------------------------------------------

def test_cli_print(saved_file, monkeypatch):
    monkeypatch.setattr(L.shutil, "which", lambda name: None)
    r = CliRunner().invoke(cli, ["console", "launch", "--print", "carol@mingus.example",
                                 "--via", "carol@ws.example"])
    assert r.exit_code == 0, r.output
    assert "ssh -N -L" in r.output and "-J carol@ws.example" in r.output


def test_cli_without_a_machine_says_which(saved_file):
    r = CliRunner().invoke(cli, ["console", "launch"])
    assert r.exit_code == 1
    assert "Which machine serves the Console?" in r.output


def test_cli_refuses_option_like_names(saved_file):
    r = CliRunner().invoke(cli, ["console", "launch", "--print", "--", "-oProxyCommand=x"])
    assert r.exit_code == 1
    assert "does not look right" in r.output


@pytest.mark.parametrize("opt", ["--port", "--remote-port"])
def test_cli_port_out_of_range(saved_file, opt):
    r = CliRunner().invoke(cli, ["console", "launch", "--print", "u@h", opt, "70000"])
    assert r.exit_code == 2 and "70000" in r.output


def test_nomad_loads_without_posix_only_modules():
    """On Windows there is no fcntl; `nomad` (and so `nomad console launch`) must load."""
    code = ("import sys\n"
            "for m in ('fcntl', 'pwd', 'grp', 'resource', 'termios'):\n"
            "    sys.modules[m] = None\n"
            "import nomad.cli\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=Path(__file__).resolve().parents[1])
    assert r.returncode == 0, r.stderr


# -- from a shell on a cluster ----------------------------------------------------

@pytest.mark.parametrize("env,platform,want", [
    ({}, "darwin", True),
    ({}, "win32", True),
    ({"DISPLAY": ":0"}, "linux", True),
    ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", True),
    ({}, "linux", False),                                   # a server with no display
    ({"SSH_CONNECTION": "1 2 3 4"}, "darwin", False),
    ({"SSH_TTY": "/dev/pts/3", "DISPLAY": "localhost:10"}, "linux", False),
])
def test_browser_here(env, platform, want):
    assert L.browser_here(env, platform) is want


def test_on_a_cluster_it_prints_the_line_for_your_own_computer(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.5 50000 10.0.0.1 22")
    monkeypatch.setattr(L, "_user", lambda: "carol")
    monkeypatch.setattr(L.shutil, "which", lambda name: pytest.fail("no ssh should run here"))
    out = []
    code = L.launch(L.Target("mingus.example"), out=out.append, local_port=None)
    text = "\n".join(out)
    assert code == 0
    assert "ssh -N -L 8000:localhost:8000 " in text and " -J carol@" in text
    assert text.count("carol@mingus.example") == 1
    assert "http://localhost:8000" in text


def test_the_jump_is_this_cluster_by_the_name_users_reach_it_by(monkeypatch):
    monkeypatch.setenv(L.ENV_LOGIN, "spydur.example.edu")
    monkeypatch.setattr(L, "_user", lambda: "carol")
    lines = L.instructions(L.Target("carol@mingus.example.edu"))
    assert _long(lines) == ("ssh -N -L 8000:localhost:8000 " + OPEN_SAYS + " "
                            "-J carol@spydur.example.edu carol@mingus.example.edu")
    assert 'leave out "-J carol@spydur.example.edu"' in lines[-1]


def test_no_jump_on_the_consoles_own_machine(monkeypatch):
    monkeypatch.setattr(L, "_user", lambda: "zeus")
    lines = L.instructions(L.Target("zeus@mingus.example.edu"), login_host="mingus")
    assert _long(lines) == ("ssh -N -L 8000:localhost:8000 " + OPEN_SAYS
                            + " zeus@mingus.example.edu")


def _long(lines):
    """The line that works with a password, without a key."""
    return next(x.strip() for x in lines if x.strip().startswith("ssh -N -L"))


# Once both logins succeed, ssh prints this itself: a tunnel is otherwise silent.
OPEN_SAYS = ("-o PermitLocalCommand=yes -o 'LocalCommand=echo Tunnel open: "
             "http://localhost:8000 -- keep this window open, Ctrl-C closes it'")


def test_the_open_message_runs_through_a_shell_as_one_echo():
    import shlex
    lines = L.instructions(L.Target("zeus@mingus.example.edu"), login_host="mingus")
    words = shlex.split(_long(lines))
    local = next(w for w in words if w.startswith("LocalCommand="))
    command = local.split("=", 1)[1]
    assert ";" not in command and "%" not in command and "&" not in command
    out = subprocess.run(["sh", "-c", command], capture_output=True, text=True).stdout
    assert out == ("Tunnel open: http://localhost:8000 -- keep this window open, "
                   "Ctrl-C closes it\n")


def test_the_site_names_the_consoles_machine(monkeypatch, saved_file):
    monkeypatch.setenv(L.ENV_HOST, "mingus.example.edu")
    monkeypatch.setattr(L, "_user", lambda: "carol")
    assert L.resolve(None, None, None) == L.Target("carol@mingus.example.edu")
    # A launch that worked, and the command line, both come first.
    L.save(L.Target("carol@other.example.edu"))
    assert L.resolve(None, None, None).destination == "carol@other.example.edu"
    assert L.resolve("x@y", None, None).destination == "x@y"


def test_nomad_console_alone_is_launch(monkeypatch, saved_file):
    monkeypatch.setenv("SSH_CONNECTION", "1 2 3 4")
    monkeypatch.setenv(L.ENV_HOST, "mingus.example.edu")
    monkeypatch.setenv(L.ENV_LOGIN, "spydur.example.edu")
    r = CliRunner().invoke(cli, ["console"])
    assert r.exit_code == 0, r.output
    assert "-J " in r.output and "@spydur.example.edu" in r.output
    assert "On your own computer, run:" in r.output


# -- whose name goes in the laptop line -----------------------------------------

@pytest.mark.parametrize("login, sudo_user, expected", [
    ("pi1", None, "pi1"),                  # logged in as herself, or sudo -u pi1
    ("root", "jdoe", "jdoe"),              # sudo -i: the admin's own name
    ("root", None, "NETID"),               # root logged in directly: nobody's NetID
    ("root", "root", "NETID"),
])
def test_the_user_is_never_root(monkeypatch, login, sudo_user, expected):
    monkeypatch.setattr(L.getpass, "getuser", lambda: login)
    if sudo_user is None:
        monkeypatch.delenv("SUDO_USER", raising=False)
    else:
        monkeypatch.setenv("SUDO_USER", sudo_user)
    assert L._user() == expected


def test_a_line_for_root_asks_for_the_netid(monkeypatch):
    monkeypatch.setattr(L.getpass, "getuser", lambda: "root")
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv(L.ENV_LOGIN, raising=False)
    lines = L.instructions(L.Target("console.example.edu"), login_host="labws1.example.edu")
    text = "\n".join(lines)
    assert "-J NETID@labws1.example.edu NETID@console.example.edu" in text
    assert "(Put your NetID where it says NETID.)" in text
