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

## From a cluster

Typed in a shell on a cluster over SSH, `nomad console` (the same as
`nomad console launch`) knows a browser there would not be on your screen, and
prints the one line to run on your own computer, through the cluster:

```
$ nomad console
A browser started on spydur would not be on your screen. On your own computer, run:

    ssh -N nomad-console

It opens the Console in your browser; that window keeps it open, and Ctrl-C there closes it.

The first time on that computer (Mac or Linux), set it up once there with:

    bash <(ssh NETID@spydur.example.edu /usr/local/sw/bin/nomad console --key-setup)

and send the line it prints to your research computing contact.

Until then, or on Windows, this works with your password:

    ssh -N -L 8000:localhost:8000 ... -J NETID@spydur.example.edu NETID@console-host.example.edu
...
```

Your own computer then needs nothing but ssh.

### No password: a key for the Console

`nomad console --key-setup` prints a short bash script; the line above runs
it on the person's own computer, fetched over ssh from the cluster. It

- makes a key just for the Console, `~/.ssh/nomad_console`;
- tries the Console's machine directly: when it answers, the entry goes
  straight there, otherwise through the cluster (and copies the key to the
  person's own account there, asking their password once);
- puts a `nomad-console` entry first in `~/.ssh/config`, keeping a copy of the
  old file and everything that was in it, after a `Host *` line (ssh takes
  the first value it finds for each setting); run again, it replaces its own
  entry;
- prints the public key, for whoever adds Console keys on the server (below).

`ssh -N nomad-console` then opens the tunnel and, once it is up, the browser
(`open` on a Mac, which keeps the key's passphrase in the Keychain;
`xdg-open` on Linux). At a workstation's own screen, `nomad console` uses the
key when there is one and otherwise says how to set it up.
`NOMAD_CONSOLE_CONTACT` names who adds keys; `NOMAD_COMMAND` gives the
shared `nomad` by its full path, since a command over ssh gets a shorter
PATH than a login. Two names come from the
environment, which a cluster's shared `nomad` command sets for everyone:
`NOMAD_CONSOLE_HOST` (the machine that serves the Console) and
`NOMAD_LOGIN_HOST` (the cluster, as people's computers reach it). `--here`
opens the tunnel and browser on the cluster after all (a remote desktop, say).

A shared install is one virtual environment every user can read (for example
`/usr/local/sw/nomad`) and a small `nomad` script on everyone's PATH:

```
#!/bin/bash
export NOMAD_CONSOLE_HOST="${NOMAD_CONSOLE_HOST:-console-host.example.edu}"
export NOMAD_LOGIN_HOST="${NOMAD_LOGIN_HOST:-cluster.example.edu}"
exec env -u PYTHONHOME -u PYTHONPATH /usr/local/sw/nomad/bin/nomad "$@"
```

Clearing `PYTHONHOME` and `PYTHONPATH` keeps a site's own Python settings
(an Anaconda in the login scripts, say) from breaking nomad's environment.
Users can run every command, but nomad's databases stay readable only by the
account that collects them, so what anyone sees of other people goes through
the Console's own scoping.

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

### Keys for those accounts

They can't write anything on the machine, so their keys come from a folder
the admin keeps, in a second block after the first, with the same `Match`:

```
Match Group *,!console-shell User *,!root
    AuthorizedKeysFile /etc/ssh/nomad_keys/%u
```

`/etc/ssh/nomad_keys` is root's (mode 755), one file per account (644). Each
key line starts with options that hold it to the tunnel even without the
block above:

```
restrict,port-forwarding,permitopen="localhost:8000",permitopen="127.0.0.1:8000" ssh-ed25519 AAAA... nomad-console
```

A key in the account's own home is not read. Passwords keep working.

## Later: a web address

The tunnel is the way in until the Console sits behind HTTPS on a named host:
a reverse proxy with a certificate, ideally with campus single sign-on. Then
people open an address and nothing needs installing.
