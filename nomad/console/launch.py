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

With no password: ``nomad console --key-setup`` prints a setup that a person
runs once on their own computer (Mac or Linux); it makes a key just for the
Console and an ssh entry, so that ``ssh -N nomad-console`` opens the tunnel
and the browser. The server reads such keys from a folder its admin keeps.
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
ENV_CONTACT = "NOMAD_CONSOLE_CONTACT"   # who adds Console keys (said by --key-setup)
ENV_COMMAND = "NOMAD_COMMAND"          # this machine's nomad, as ssh would run it
ALIAS = "nomad-console"                 # the ssh name --key-setup sets up
KEY_NAME = "nomad_console"              # ~/.ssh/nomad_console, the Console's own key

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
    key = key_path()
    if key.is_file():
        # The Console's own key (nomad console --key-setup): no password once
        # it is in. Tried first; the usual keys and the password still work.
        argv += ["-o", f"IdentityFile={key}"]
    if target.via:
        argv += ["-J", target.via]
    argv.append(target.destination)
    return argv


def key_path() -> Path:
    return Path.home() / ".ssh" / KEY_NAME


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


def _here(login_host: str | None = None) -> str:
    return login_host or os.environ.get(ENV_LOGIN) or socket.getfqdn()


def nomad_command() -> str:
    """This machine's nomad by its full path when known: a command over ssh
    gets a shorter PATH than a login, which may not have it."""
    cmd = os.environ.get(ENV_COMMAND, "").strip()
    if cmd:
        return cmd
    arg0 = sys.argv[0] if sys.argv else ""
    if os.path.basename(arg0) == "nomad" and os.path.isabs(arg0):
        return arg0
    return "nomad"


def setup_line(login_host: str | None = None) -> str:
    """What a person runs once on their own computer to set up the Console
    key: this machine's nomad prints the setup, their computer runs it."""
    here = _here(login_host).rsplit("@", 1)[-1]
    return (f"bash <(ssh {_user()}@{here} {shlex.quote(nomad_command())} "
            "console --key-setup)")


def instructions(target: Target, login_host: str | None = None) -> list:
    """What to run on one's own computer, from a shell on a cluster."""
    user = _user()
    dest = target.destination if "@" in target.destination else f"{user}@{target.destination}"
    here = _here(login_host)
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
        f"    ssh -N {ALIAS}",
        "",
        "It opens the Console in your browser; that window keeps it open, and "
        "Ctrl-C there closes it.",
        "",
        "The first time on that computer (Mac or Linux), set it up once there with:",
        "",
        "    " + setup_line(login_host),
        "",
        "and send the line it prints to " + _contact() + ".",
        "",
        "Until then, or on Windows, this works with your password:",
        "",
        "    " + shlex.join(words),
        "",
        f"then open http://localhost:{port} and sign in with your NetID.",
    ]
    if jump:
        lines.append(f"(If your computer reaches {target.host} directly, leave out "
                     f"\"-J {jump}\".)")
    if "NETID@" in " ".join(words):
        lines.append("(Put your NetID where it says NETID.)")
    return lines


def _contact() -> str:
    return (os.environ.get(ENV_CONTACT) or "").strip() or "your research computing contact"


# -- the one-time setup on a person's own computer ----------------------------
#
# `nomad console --key-setup` prints this; the person's computer runs it:
#     bash <(ssh NETID@workstation nomad console --key-setup)
# It makes ~/.ssh/nomad_console, puts a "nomad-console" entry at the top of
# ~/.ssh/config (ssh takes the first value it finds for each setting) and
# prints the public key for whoever adds Console keys. `ssh -N nomad-console`
# then opens the tunnel and the browser. Mac and Linux; the values come from
# this machine and are checked before they are written into it.

_KEY_SETUP = r"""#!/bin/bash
# NOMAD Console: one-time setup on your own computer (Mac or Linux).
# Printed by `nomad console --key-setup` on @@FROM@@.
set -u
NETID=@@NETID@@
CONSOLE_HOST=@@CONSOLE@@
VIA=@@VIA@@
PORT=@@PORT@@
CONTACT=@@CONTACT@@
ALIAS=@@ALIAS@@
KEY=$HOME/.ssh/@@KEY@@
CFG=$HOME/.ssh/config
URL=http://localhost:$PORT
BEGIN="# NOMAD Console (added"
TTY=/dev/tty
[ -t 0 ] && TTY=/dev/stdin
(exec < "$TTY") 2>/dev/null || TTY=/dev/null

die() { echo "STOP: $*" >&2; exit 1; }
command -v ssh >/dev/null && command -v ssh-keygen >/dev/null || die "ssh is not installed here"
case "$NETID" in NETID|"") die "run this as yourself: the NetID it was printed for is not known" ;; esac
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"

# 1. The key, for the Console only.
if [ -f "$KEY" ]; then
    echo "Using your Console key ($KEY)."
else
    echo "Making a key for the Console. A passphrase is a good idea (Enter twice for none)."
    ssh-keygen -q -t ed25519 -f "$KEY" -C "nomad-console $NETID" < "$TTY" || die "ssh-keygen failed"
fi

# 2. Straight to the Console's machine, or through the workstation? A quick
#    look: refused (no key yet) or the tunnel message both mean reachable.
probe=$(ssh -o BatchMode=yes -o ConnectTimeout=6 -o IdentitiesOnly=yes -i "$KEY" \
        -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR \
        "$NETID@$CONSOLE_HOST" true < /dev/null 2>&1)
if printf '%s' "$probe" | grep -q -i -E 'denied|tunnel only'; then
    VIA=
    echo "Your computer reaches $CONSOLE_HOST directly."
elif [ -n "$VIA" ]; then
    echo "Your computer does not reach $CONSOLE_HOST directly now; going through $VIA."
else
    die "your computer does not reach $CONSOLE_HOST now (on campus or the VPN?)"
fi

# 3. What opens the browser once the tunnel is up.
if [ "$(uname -s)" = Darwin ]; then
    OPEN="open $URL"
    KEYCHAIN="    UseKeychain yes"
else
    OPEN="xdg-open $URL >/dev/null 2>&1 &"
    KEYCHAIN=
fi

# 4. The ssh entry, first in ~/.ssh/config. An entry this setup wrote before
#    is replaced; the rest of the file is kept as it was, after "Host *".
rest=
if [ -f "$CFG" ]; then
    if grep -q "^$BEGIN" "$CFG"; then
        rest=$(awk -v b="$BEGIN" '
            !skip && index($0, b) == 1 { skip = 1; next }
            skip == 1 && $0 == "Host *" { skip = 2; next }
            skip != 1 { print }' "$CFG")
    elif grep -q -E "^Host[[:space:]]+$ALIAS([[:space:]]|\$)" "$CFG"; then
        die "~/.ssh/config already has a $ALIAS entry this setup did not write; remove it and run this again"
    else
        rest=$(cat "$CFG")
    fi
    cp -p "$CFG" "$CFG.bak-$ALIAS-$(date +%Y%m%d-%H%M%S)"
fi
{
    echo "$BEGIN $(date +%F) by nomad console --key-setup)"
    echo "Host $ALIAS"
    echo "    HostName $CONSOLE_HOST"
    echo "    User $NETID"
    echo "    IdentityFile ~/.ssh/@@KEY@@"
    echo "    IdentitiesOnly yes"
    echo "    AddKeysToAgent yes"
    if [ -n "$KEYCHAIN" ]; then echo "$KEYCHAIN"; fi
    echo "    LocalForward $PORT localhost:$PORT"
    echo "    ExitOnForwardFailure yes"
    echo "    ServerAliveInterval 60"
    echo "    PermitLocalCommand yes"
    echo "    LocalCommand echo \"Tunnel open: $URL -- keep this window open, Ctrl-C closes it\"; $OPEN"
    if [ -n "$VIA" ]; then
        echo "    ProxyJump $ALIAS-via"
        echo
        echo "Host $ALIAS-via"
        echo "    HostName $VIA"
        echo "    User $NETID"
        echo "    IdentityFile ~/.ssh/@@KEY@@"
        echo "    IdentitiesOnly yes"
        echo "    AddKeysToAgent yes"
        if [ -n "$KEYCHAIN" ]; then echo "$KEYCHAIN"; fi
    fi
    echo
    echo "Host *"
    if [ -n "$rest" ]; then printf '%s\n' "$rest"; fi
} > "$CFG.new" || die "could not write $CFG.new"
chmod 600 "$CFG.new"
if ! ssh -G -F "$CFG.new" "$ALIAS" >/dev/null 2>&1 || \
   ! ssh -G -F "$CFG.new" some-other-host.invalid >/dev/null 2>&1; then
    rm -f "$CFG.new"; die "the new ~/.ssh/config would not be valid; nothing changed"
fi
if [ -e "$CFG" ]; then
    # Written through, so a ~/.ssh/config that is a link stays one.
    cat "$CFG.new" > "$CFG" && rm -f "$CFG.new" || die "could not write $CFG"
else
    mv -f "$CFG.new" "$CFG"
fi
echo "~/.ssh/config: the $ALIAS entry is in place."

# 5. Through the workstation: the key there too (your own account; your
#    password, once).
if [ -n "$VIA" ]; then
    echo "Putting the key on $VIA for the hop through it (your password, once):"
    ssh-copy-id -i "$KEY.pub" -o IdentitiesOnly=yes "$NETID@$VIA" < "$TTY" || \
        echo "  That didn't work; run it again later: ssh-copy-id -i $KEY.pub $NETID@$VIA"
fi

echo
if printf '%s' "$probe" | grep -q -i 'tunnel only'; then
    echo "Your key is already in. Open the Console with:  ssh -N $ALIAS"
else
    echo "Send this one line to $CONTACT (it is safe to share):"
    echo
    cat "$KEY.pub"
    echo
    echo "Once it's in, open the Console with:  ssh -N $ALIAS"
fi
echo "(Your browser opens by itself; that window keeps the Console open, Ctrl-C closes it.)"
"""


def key_setup_script(target: Target, login_host: str | None = None,
                     user: str | None = None, contact: str | None = None) -> str:
    """The setup to run on a person's own computer (see _KEY_SETUP)."""
    target.validate()
    user = user or (target.destination.split("@", 1)[0] if "@" in target.destination
                    else _user())
    here = _here(login_host).rsplit("@", 1)[-1]
    via = "" if _short(here) == _short(target.host) else here
    for label, value in (("user", user), ("machine", target.host), ("workstation", via),
                         ("this machine", here)):
        if value and not all(c.isalnum() or c in "._-" for c in value):
            raise LaunchError(f"That {label} name does not look right: {value!r}")
    values = {
        "@@FROM@@": here, "@@NETID@@": shlex.quote(user),
        "@@CONSOLE@@": shlex.quote(target.host), "@@VIA@@": shlex.quote(via),
        "@@PORT@@": str(target.remote_port),
        "@@CONTACT@@": shlex.quote(contact or _contact()),
        "@@ALIAS@@": ALIAS, "@@KEY@@": KEY_NAME,
    }
    out = _KEY_SETUP
    for token, value in values.items():
        out = out.replace(token, value)
    return out


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
    if not key_path().is_file():
        out("(To skip the password next time:  "
            f"bash <({shlex.quote(nomad_command())} console --key-setup))")
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
    p.add_argument("--key-setup", action="store_true",
                   help="print the one-time setup for your own computer (a key, "
                        "so the Console needs no password)")
    a = p.parse_args(argv)
    try:
        target = resolve(a.destination, a.via, a.remote_port)
        if a.key_setup:
            sys.stdout.write(key_setup_script(target))
            return 0
        return launch(target, local_port=a.port, open_browser=not a.no_browser,
                      print_only=a.print_only, out=lambda m: print(m, flush=True),
                      here=True if a.here else None)
    except LaunchError as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
