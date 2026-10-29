# Policy watch

What runs on a cluster's login and compute nodes that its rules usually forbid, for an administrator to look at (1.19). It reads; it never stops, changes or reports anything to anyone but the site's admins.

## What it finds

Of accounts from `min_uid` (1000) up -- system services aren't people; nor are Slurm's and munge's accounts, nor the collector's own (`ignore_users`, `watch_self`):

| Kind | What | Alerted |
|---|---|---|
| `tunnel` | a program that opens a way in or out: ngrok, cloudflared, frp, chisel, Tailscale, ZeroTier, sshuttle, wstunnel, iodine, ... (and `ssh -w`); generic names (bore, gost, naive, xray, ...) only with an argument that listens or proxies | warning |
| `ssh_reverse` | an ssh client forwarding a port from outside to here (`-R`, `RemoteForward`), before or after the destination, as ssh reads them (checked against `ssh -G`) | warning |
| `ssh_socks` | an ssh SOCKS proxy (`-D`, `DynamicForward`): this host's traffic carried out through another | warning |
| `code_tunnel` | `code tunnel`: VS Code's relay through Microsoft (Remote-SSH's server isn't one) | warning |
| `miner` | a miner by name (xmrig, t-rex, lolMiner, ...), xmrig's `--donate-level`, or a `stratum+tcp://` pool on the command line of a program that isn't a text tool (`grep stratum+tcp://` isn't a miner) | critical |
| `relay` | a relay or proxy: socat listening, 3proxy, v2ray, shadowsocks, sing-box, ... | warning |
| `listening` | a person's program listening on a port open to the network, on a login node | warning |

A program is known by its name as it started (argv[0]), its kernel name (`comm`: what `exec -a` doesn't change), a download's suffix aside (`cloudflared-linux-amd64`, `chisel_1.10.1_linux_amd64`), and, for an interpreter (python, node, java, sh, ...), the script or module it runs, by its exact name (`python3 -m sshuttle`, `sh pagekite.py`; `rigel_fit.py` isn't the rigel miner). An `ignore_programs` entry leaves a finding out only if it names every name the process goes by.

**What a user controls never lowers a finding.** An ssh forward whose destination is one of the cluster's own names (the collector's host, `login_hosts`, `inside_hosts`) or a private address, with no `-J`, `-W`, `-F` or `-o HostName/ProxyJump/ProxyCommand`, from an account that reaches nothing public from that host, says "to one of the cluster's names (not verified)": a hint for the reader (a batch job's Jupyter reached from a login node, say), not a pass, since a user's own `~/.ssh/config` can map any name anywhere. It stays a warning.

On compute nodes, programs listening are not findings unless `listeners_on_nodes = true`: MPI, NCCL, Dask and Ray open ports of their own there (and with it, a port in the kernel's ephemeral range is listed only). Listening on loopback only (VS Code's server, a Jupyter reached through `ssh -L`) is never one, nor is a local forward (`ssh -L`).

A host that answered before and hasn't for 3 sweeps raises its own alert: a probe that can't finish there sees nothing, and someone may be hiding.

Not seen: a forward set in someone's `~/.ssh/config` (it isn't on the command line), and what runs between sweeps.

## How

Every `interval_minutes` (15), a probe (`watch_probe.py`, standard library only, Python 3.6 or later) runs on the login node the collector runs on and, over ssh (BatchMode, no agent or X forwarding), on each compute node Slurm lists that is up, a few at a time, for at most `max_seconds`. It is piped to a fixed interpreter, the first of `python` (`/usr/bin/python3`, `/usr/libexec/platform-python`) that exists, with `-I -S`: not whatever `python3` comes first on the account's PATH, and no site-packages or `.pth` files, so nothing a user could put in the collector's way runs as its account. Nothing is installed on the nodes. It reads `/proc` as the collector's account: every account's processes and their command lines (at most 64 KB each), and the listening sockets with their owners (`/proc/net/tcp`, readable by every account). No root is needed, unless `/proc` is mounted with `hidepid`: then only the collector's own processes are seen (sockets still are), and `nomad watch status` says so. Which program listens on a port is known only to root (another account's sockets are its own): elsewhere a listener is its owner and port, and `ignore_ports` leaves out a sanctioned one.

## What is kept

The host, the account, the kind, the program's name (a short word: letters, digits, `. _ + -`; at most five per account and kind on a host, listeners too, then `+N more`), the port it forwards or listens on, and inside or outside for an ssh forward: **never a command line, an address or a host it connects to.** Each finding has a first and a last time seen, and how many sweeps saw it, in `policy_findings`, which the hub gets with every other table: admins see NetIDs, as elsewhere in the Console. `policy_watch_hosts` keeps, per sweep, which hosts answered and what the probe could see there.

Findings raise one alert per account and kind, on whatever hosts (renaming a program, or the same thing on many nodes, makes no new one; something new in it makes one more, itself deduplicated; deduplicated, with reminders as for every alert) that names no one unless `owners_in_alerts = true`: alerts also go to Slack and webhooks, and their key is the finding's number on the site, which says nothing of the account. `policy_watch_hosts` (7 days) and `policy_watch_sweeps` stay on the site.

## Turning it on

```toml
[collectors.policy_watch]
enabled = true
# interval_minutes = 15
# nodes = "slurm"              # every node Slurm lists that is up; or a list; or [] for this host only
# login_hosts = []             # other login nodes, asked over ssh
# min_uid = 1000
# listeners_on_nodes = false
# ignore_users = ["slurm", "munge"]   # services' accounts above min_uid (the default)
# watch_self = false           # the collector's own account (an admin's: it may run services) left out
# ignore_programs = []         # e.g. ["tailscaled"] where IT runs it
# ignore_ports = []
# owners_in_alerts = false
# parallel = 8
# timeout = 15
# max_seconds = 180
# ssh_user = "..."
# python = ["/usr/bin/python3", "/usr/libexec/platform-python"]
# inside_hosts = []            # more of the cluster's own hosts (for ssh -R from a job)
# keep_days = 365              # findings not seen for this long are removed
```

The collector's account needs ssh to the nodes without a password (as `node_disks` does). Before turning it on, `nomad watch probe` (here) and `nomad watch probe NODE` show what a sweep would find, storing nothing.

## Looking at it

```
nomad watch list              findings of the last 7 days, the most serious first (* seen in the last sweep)
nomad watch list --all        also what is only listed (ephemeral ports on compute nodes)
nomad watch list --mask       stand-ins for people and hosts, for anything shared outside ARC
nomad watch list --json
nomad watch status            the last sweep: each host, whether it answered, what it could see
nomad watch probe [HOST]      look now
```

## What it is not

It is a set of signs for a person to judge, not a verdict: an administrator's own tunnel, a sanctioned service or a research server may well be fine (`ignore_*`). It sees what runs while it looks (every 15 minutes), not what ran in between. It doesn't stop anything: closing these doors (compute nodes reachable only through Slurm with `pam_slurm_adopt`, outbound traffic from compute nodes allowed only where needed, limits on login nodes) is the cluster's configuration, and what is allowed is the site's policy.
