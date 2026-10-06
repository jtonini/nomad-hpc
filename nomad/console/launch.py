"""Open the NØMAÐ Console in a browser on this computer, through an SSH tunnel.

The Console listens only on its own machine (127.0.0.1:8000 by default), so a
browser anywhere else reaches it through an SSH tunnel.  This module opens
that tunnel, waits until the Console answers through it, opens the browser,
and closes the tunnel on Ctrl-C.  It is what ``nomad console launch`` runs.

It uses the Python standard library only, so the file also runs on its own on
a computer without nomad (Python 3.8 or later):

    python3 launch.py NETID@mingus.richmond.edu

Nothing secret is kept: ssh asks for the password itself, and only the
machine names of the last successful launch are remembered, so that next time
``nomad console launch`` alone is enough.

Run on a cluster over SSH, where a browser would not be the person's own, it
prints the one ssh line to run on their own computer instead, through this
cluster. A site sets two names for that, in the environment (the shared
``nomad`` command on a cluster sets them):

    NOMAD_CONSOLE_HOST   the machine that serves the Console (mingus.richmond.edu)
    NOMAD_LOGIN_HOST     this cluster, as people's computers reach it
"""
from __future__ import annotations

import getpass
import http.client
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_REMOTE_PORT = 8000
WAIT_SECONDS = 300          # time to type a password (and answer a second factor)
NO_CONSOLE_SECONDS = 15     # tunnel up, nothing answering through it: give up
TRUST_AFTER_SECONDS = 10    # an answer ssh has not vouched for, trusted after this long
READY = "NOMAD-TUNNEL-READY"
SAVED = Path.home() / ".config" / "nomad" / "console_launch.json"
ENV_HOST = "NOMAD_CONSOLE_HOST"
ENV_LOGIN = "NOMAD_LOGIN_HOST"

NO_SSH = (
    "No ssh command was found on this computer. On Windows 10 and 11, add "
    "'OpenSSH Client' under Settings > System > Optional features; macOS and "
    "Linux have it already."
)


class LaunchError(Exception):
    """Something the person can fix, said in words they can act on."""


def _check_port(n, what: str) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise LaunchError(f"Not a port number for {what}: {n!r}") from None
    if not 0 < n < 65536:
        raise LaunchError(f"Not a port number for {what}: {n} (1 to 65535)")
    return n


@dataclass
class Target:
    destination: str                    # [user@]host serving the Console
    via: str | None = None              # jump host, as ssh -J
    remote_port: int = DEFAULT_REMOTE_PORT

    def validate(self) -> Target:
        for label, value in (("machine", self.destination), ("--via", self.via)):
            if value is None and label == "--via":
                continue
            if (not isinstance(value, str) or not value or value.startswith("-")
                    or any(c.isspace() for c in value)):
                raise LaunchError(f"That {label} name does not look right: {value!r}")
        self.remote_port = _check_port(self.remote_port, "--remote-port")
        return self

    @property
    def host(self) -> str:
        return self.destination.rsplit("@", 1)[-1]


# -- remembering the last launch ---------------------------------------------

def load_saved(path: Path | None = None) -> Target | None:
    path = path or SAVED
    try:
        data = json.loads(path.read_text())
        return Target(destination=data["destination"], via=data.get("via"),
                      remote_port=data.get("remote_port", DEFAULT_REMOTE_PORT)).validate()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, LaunchError):
        return None


def save(target: Target, path: Path | None = None) -> bool:
    """Remember the target; True when it was new or changed."""
    path = path or SAVED
    if load_saved(path) == target:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(target), indent=2) + "\n")
    except OSError:
        return False
    return True


# -- ports and the ssh command ------------------------------------------------

def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":
            # As ssh does: a port the last tunnel left in TIME_WAIT is free.
            # (On Windows this option would let two listeners share a port.)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_local_port(wanted: int | None, preferred: int) -> int:
    """The port given, else the Console's own number if free, else any free one."""
    if wanted is not None:
        wanted = _check_port(wanted, "--port")
        if not port_free(wanted):
            raise LaunchError(f"Port {wanted} is already in use on this computer; "
                              "choose another with --port, or leave it out.")
        return wanted
    if port_free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ssh_command(ssh: str, target: Target, local_port: int) -> list:
    argv = [
        ssh, "-N",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        # A connection of its own. A shared one (ControlMaster/ControlPersist
        # in ~/.ssh/config) would carry the tunnel past Ctrl-C, or ssh would
        # go to the background and look as if it had stopped.
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        # ssh prints READY once the forward is in place, so an answer after
        # that comes through this tunnel and not from something else that
        # took the port while ssh was waiting for the password.
        "-o", "PermitLocalCommand=yes",
        "-o", f"LocalCommand=echo {READY}",
        # Bound to this computer only, whatever ~/.ssh/config says (GatewayPorts).
        "-L", f"127.0.0.1:{local_port}:localhost:{target.remote_port}",
    ]
    if target.via:
        argv += ["-J", target.via]
    argv.append(target.destination)
    return argv


def manual_command(target: Target, local_port: int) -> str:
    """The same tunnel as a person would type it."""
    words = ["ssh", "-N", "-L", f"{local_port}:localhost:{target.remote_port}"]
    if target.via:
        words += ["-J", target.via]
    words.append(target.destination)
    if os.name == "nt":
        return subprocess.list2cmdline(words)
    return shlex.join(words)


# -- waiting for the Console --------------------------------------------------

def probe(port: int, timeout: float = 3.0) -> str:
    """'up' when the Console answers HTTP; 'tunnel' when the port accepts a
    connection but nothing answers through it; 'down' when nothing listens."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
        conn.getresponse()
        return "up"
    except ConnectionRefusedError:
        return "down"
    except (OSError, http.client.HTTPException):
        return "tunnel"
    finally:
        conn.close()


def wait_until_up(proc, port: int, *, ready: Callable[[], bool] = lambda: True,
                  stopped: Callable[[], bool] = lambda: False,
                  timeout: float = WAIT_SECONDS, no_console: float = NO_CONSOLE_SECONDS,
                  trust_after: float = TRUST_AFTER_SECONDS, interval: float = 0.5,
                  probe: Callable[[int], str] = probe,
                  clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep) -> str:
    """'up', 'exited' (ssh ended), 'no-console' (tunnel up, no answer),
    'timeout' or 'stopped' (asked to stop)."""
    start = clock()
    tunnel_since = up_since = None
    while True:
        if stopped():
            return "stopped"
        if proc.poll() is not None:
            return "exited"
        state = probe(port)
        if state == "up":
            if ready():
                return "up"
            # ssh has not said its forward is in place, so this answer may
            # come from something else on the port. Trust it only once it has
            # lasted (for an ssh client that cannot say).
            if up_since is None:
                up_since = clock()
            if clock() - up_since >= trust_after:
                return "up"
        else:
            up_since = None
        if state == "tunnel":
            if tunnel_since is None:
                tunnel_since = clock()
            if clock() - tunnel_since >= no_console:
                return "no-console"
        else:
            tunnel_since = None
        if clock() - start >= timeout:
            return "timeout"
        sleep(interval)


def _watch_for_ready(stream, flag: threading.Event) -> None:
    try:
        for line in iter(stream.readline, b""):
            if READY.encode() in line:
                flag.set()
    except (OSError, ValueError):
        pass


# -- stopping -----------------------------------------------------------------

_stop_requested = threading.Event()


def _on_stop(signum, frame) -> None:
    _stop_requested.set()


def _catch_stop_signals() -> dict:
    """SIGTERM/SIGHUP (a kill, the window closing) close the tunnel like Ctrl-C.

    The handler only sets a flag, which the waiting loops look at, so the
    signal can never land between starting ssh and being ready to stop it.
    A signal already ignored (nohup) stays ignored.
    """
    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            if signal.getsignal(sig) == signal.SIG_IGN:
                continue
            previous[sig] = signal.signal(sig, _on_stop)
        except (ValueError, OSError):      # not the main thread, or not allowed here
            pass
    return previous


def _restore_signals(previous: dict) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def _wait_or_stop(proc, stopped: Callable[[], bool], interval: float = 0.5) -> int | None:
    """ssh's exit code, or None when asked to stop first. Polls rather than
    blocking in wait(), which Ctrl-C cannot interrupt on Windows."""
    while True:
        code = proc.poll()
        if code is not None:
            return code
        if stopped():
            return None
        time.sleep(interval)


def stop(proc) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except OSError:
            pass


# -- on a remote shell -------------------------------------------------------------

def _user() -> str:
    """The user name for ssh lines: the login name (under sudo -u, that
    user's), but never root, which is nobody's NetID: then sudo's caller, or
    NETID for the person to fill in."""
    try:
        name = getpass.getuser()
    except Exception:
        return "NETID"
    if name == "root":
        caller = os.environ.get("SUDO_USER", "")
        return caller if caller and caller != "root" else "NETID"
    return name


def browser_here(env=None, platform: str | None = None) -> bool:
    """Whether a browser opened here would be in front of the person.

    Not over SSH (the browser would be on this machine, not theirs), and not on
    a Linux or BSD machine with no display.
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return False
    if platform.startswith(("linux", "freebsd", "openbsd", "netbsd")):
        return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))
    return True


def _short(host: str) -> str:
    return host.rsplit("@", 1)[-1].split(".", 1)[0].lower()


def instructions(target: Target, login_host: str | None = None) -> list:
    """What to run on one's own computer, from a shell on a cluster."""
    user = _user()
    dest = target.destination if "@" in target.destination else f"{user}@{target.destination}"
    here = login_host or os.environ.get(ENV_LOGIN) or socket.getfqdn()
    jump = None if _short(here) == _short(dest) else f"{user}@{here.rsplit('@', 1)[-1]}"
    port = target.remote_port
    # Once connected, ssh itself says so: a tunnel prints nothing otherwise,
    # and a silent window reads as a hung one. (No ; or % in the message:
    # LocalCommand runs it through a shell, and % starts ssh's own tokens.)
    words = ["ssh", "-N", "-L", f"{port}:localhost:{port}",
             "-o", "PermitLocalCommand=yes",
             "-o", f"LocalCommand=echo Tunnel open: http://localhost:{port} -- keep this "
                   "window open, Ctrl-C closes it"]
    if jump:
        words += ["-J", jump]
    words.append(dest)
    lines = [
        f"A browser started on {_short(here)} would not be on your screen. "
        "On your own computer, run:",
        "",
        "    " + shlex.join(words),
        "",
        f"then open http://localhost:{port} and sign in with your NetID. That window "
        "keeps the tunnel open; Ctrl-C there closes it.",
    ]
    if jump:
        lines.append(f"(If your computer reaches {target.host} directly, leave out "
                     f"\"-J {jump}\".)")
    if "NETID@" in " ".join(words):
        lines.append("(Put your NetID where it says NETID.)")
    return lines


# -- the whole thing ----------------------------------------------------------

def launch(target: Target, *, local_port: int | None = None, open_browser: bool = True,
           print_only: bool = False, out: Callable[[str], None] = print,
           ssh: str | None = None, saved_path: Path | None = None,
           timeout: float = WAIT_SECONDS, no_console: float = NO_CONSOLE_SECONDS,
           trust_after: float = TRUST_AFTER_SECONDS, here: bool | None = None) -> int:
    """here: open the tunnel and browser on this machine even over SSH; None
    decides by browser_here()."""
    target.validate()
    if not print_only and not (browser_here() if here is None else here):
        for line in instructions(target):
            out(line)
        return 0
    port = pick_local_port(local_port, target.remote_port)
    if print_only:
        out(manual_command(target, port))
        out(f"then open http://localhost:{port}/ in your browser")
        return 0
    ssh = ssh or shutil.which("ssh")
    if not ssh:
        raise LaunchError(NO_SSH)
    argv = ssh_command(ssh, target, port)
    # 127.0.0.1, not "localhost": that can mean ::1 first, where something
    # else may be listening on the same port number.
    url = f"http://127.0.0.1:{port}/"

    out(f"Opening a tunnel to the Console on {target.host}"
        + (f" through {target.via.rsplit('@', 1)[-1]}" if target.via else "")
        + "; type your password if asked.")
    _stop_requested.clear()
    previous = _catch_stop_signals()
    proc = None
    ready = threading.Event()
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE)
        threading.Thread(target=_watch_for_ready, args=(proc.stdout, ready),
                         daemon=True).start()
        state = wait_until_up(proc, port, ready=ready.is_set,
                              stopped=_stop_requested.is_set, timeout=timeout,
                              no_console=no_console, trust_after=trust_after)
        if state == "stopped":
            out("Tunnel closed.")
            return 0
        if state == "exited":
            code = proc.returncode
            out(f"ssh stopped (exit {code}) before the tunnel was up; its message is above."
                + (" Check the machine name, your user name and password." if code == 255 else ""))
            return code or 1
        if state == "no-console":
            out(f"The tunnel is up, but nothing answers on {target.host} port "
                f"{target.remote_port}: the Console may be stopped there, or that "
                "machine does not allow a tunnel to that port.")
            return 1
        if state == "timeout":
            out(f"Gave up after {int(timeout)} seconds without reaching the Console.")
            return 1

        if save(target, saved_path):
            out(f"Saved: next time, leave out the machine name to open {target.host} again.")
        out(f"NØMAÐ Console: {url}")
        out("Keep this window open while you use it; press Ctrl-C here to close the tunnel.")
        if open_browser and not webbrowser.open(url):
            out("(Open that address in your browser.)")
        code = _wait_or_stop(proc, _stop_requested.is_set)
        if code is None:
            out("Tunnel closed.")
            return 0
        out(f"The tunnel closed (ssh exit {code}); launch again to reopen it.")
        return code
    except KeyboardInterrupt:
        out("\nTunnel closed.")
        return 0
    finally:
        if proc is not None:
            stop(proc)
        _restore_signals(previous)


def resolve(destination: str | None, via: str | None, remote_port: int | None,
            saved_path: Path | None = None) -> Target:
    """Arguments first, then the last successful launch."""
    if destination:
        return Target(destination, via,
                      DEFAULT_REMOTE_PORT if remote_port is None else remote_port).validate()
    saved = load_saved(saved_path)
    if saved is None and os.environ.get(ENV_HOST):
        host = os.environ[ENV_HOST].strip()
        saved = Target(host if "@" in host else f"{_user()}@{host}")
    if saved is None:
        raise LaunchError("Which machine serves the Console? For example:\n"
                          "  nomad console launch NETID@mingus.richmond.edu")
    if via:
        saved.via = via
    if remote_port is not None:
        saved.remote_port = remote_port
    return saved.validate()


def main(argv: list | None = None) -> int:
    """Standalone use: python3 launch.py [USER@]HOST [--via ...] [--port N] ..."""
    import argparse
    p = argparse.ArgumentParser(prog="launch.py", description=__doc__.split("\n")[0])
    p.add_argument("destination", nargs="?", help="[user@]host that serves the Console")
    p.add_argument("--via", help="jump host to go through first ([user@]host)")
    p.add_argument("--port", type=int, help="port on this computer")
    p.add_argument("--remote-port", type=int, help="the Console's port on its machine")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--print", dest="print_only", action="store_true",
                   help="only print the ssh command")
    p.add_argument("--here", action="store_true",
                   help="open the tunnel and browser on this machine even over SSH")
    a = p.parse_args(argv)
    try:
        target = resolve(a.destination, a.via, a.remote_port)
        return launch(target, local_port=a.port, open_browser=not a.no_browser,
                      print_only=a.print_only, out=lambda m: print(m, flush=True),
                      here=True if a.here else None)
    except LaunchError as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
