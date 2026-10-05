# Console access

The Console, NØMAÐ's web interface, listens only on the machine that serves it
(`127.0.0.1:8000` by default). Nothing on the network can reach it directly,
and that is deliberate: the Console asks for passwords, and until it sits
behind HTTPS they must not cross the network where they could be read. People
reach it through an SSH tunnel instead, which encrypts everything, passwords
included.

## Opening it from your own computer

Run this on the computer you are sitting at, the one with the browser:

```
nomad console launch NETID@console-host.example.edu
```

It opens the tunnel, waits until the Console answers through it, and opens
your browser. Keep the window open while you use the Console; press Ctrl-C to
close the tunnel. ssh asks for your password itself; nomad never sees it.

When the Console's machine can only be reached through another one (a lab
workstation, a gateway), go through that one with `--via`:

```
nomad console launch NETID@console-host --via NETID@gateway
```

The machine names of the last launch that worked are remembered
(`~/.config/nomad/console_launch.json`), so next time `nomad console launch`
alone is enough.

| Option | |
|---|---|
| `--via [USER@]HOST` | Go through this machine first (ssh's `-J`). |
| `--port N` | Port on this computer. Default: the Console's own number (8000) if free here, else any free port. |
| `--remote-port N` | The Console's port on its machine (default 8000). |
| `--no-browser` | Don't open the browser; only print the address. |
| `--print` | Only print the ssh command, to run it by hand. |

### What it needs

- An ssh client. macOS and Linux have one; on Windows 10 and 11, add
  *OpenSSH Client* under Settings › System › Optional features.
- An account that can log in to the Console's machine. It can be a
  tunnel-only one (below).
- nomad itself: `pip install nomad-hpc` (Python 3.10 or later). That brings
  NumPy, pandas and SciPy along. A computer that only needs the launcher can
  use the single file `nomad/console/launch.py` instead, with Python 3.8 or
  later: `python3 launch.py NETID@console-host` (on Windows, `py launch.py ...`).

### By hand

`nomad console launch --print` shows the same tunnel as a command to type:

```
ssh -N -L 8000:localhost:8000 NETID@console-host
```

Then open <http://localhost:8000>. The launcher itself adds a few options:
it keeps a connection of its own even when `~/.ssh/config` shares
connections (`ControlMaster`), binds the port to 127.0.0.1 only, and has ssh
say when the tunnel is in place, so that it never mistakes another program on
the same port for the Console.

### When it doesn't work

- **ssh stopped (exit 255)**: ssh could not reach the machine or log in; its
  own message is printed above.
- **The tunnel is up, but nothing answers**: the Console isn't running on that
  machine, or the machine doesn't allow a tunnel to that port.
- **Port N is already in use**: leave out `--port`, or choose another.

Ctrl-C, closing the window, or ending the launcher with `kill` closes the
tunnel. On Windows, ending Python from Task Manager does not: end `ssh.exe`
there too, or use Ctrl-C.

## On the server: accounts that may only open the tunnel

To let people open the Console without giving them a shell on its machine,
sshd can limit everyone outside a group to this one tunnel. Put this block at
the **end** of `/etc/ssh/sshd_config`; anything after it would belong to the
`Match`:

```
Match Group *,!console-shell User *,!root
    AllowTcpForwarding local
    PermitOpen localhost:8000 127.0.0.1:8000
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    PermitTunnel no
    GatewayPorts no
    ForceCommand echo "This login is for the NOMAD Console tunnel only."
```

Members of `console-shell` and root keep everything. Everyone else who can log
in gets the tunnel to the Console and nothing more: no shell, no file copy, no
other forwarding (including jumps through the machine).

Before reloading sshd:

1. Create the group and add yourself and anyone who works on that machine or
   jumps through it: `groupadd console-shell`, `gpasswd -a NAME console-shell`.
2. Check the file: `sshd -t`.
3. Check what an account will get:
   `sshd -T -C user=NAME,host=x,addr=127.0.0.1 | grep -E 'forcecommand|permitopen|permittty'`.
   A group member shows `forcecommand none` and `permittty yes`.
4. Reload, and keep a root session open until a new login works.

On a machine joined to the campus directory (sssd), this lets anyone with a
campus account open the Console without an account being created for them;
the Console's own login then decides what each person sees.

## Later: a web address

The tunnel is the way in until the Console sits behind HTTPS on a named host:
a reverse proxy with a certificate, ideally with campus single sign-on. Then
people open an address and nothing needs installing.
