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

**What a user writes never lowers a finding.** Each ssh forward says where its account's connections on that host go, from what the kernel shows (`/proc/net`), not from the destination on its command line (a user's own `~/.ssh/config` can map any name anywhere): *stays inside* (this host, the cluster's own networks, the servers you list), *on campus* (private addresses beyond the cluster, and `campus_networks`), or *connects outside*. That is a label; it changes nothing by itself.

### Forwards to servers you trust (1.19.1)

An `ssh -R` to a server you list in `trusted_hosts` (and to the cluster's own login hosts: this one and `login_hosts`) is **verified**: shown by `nomad watch list` as info, with why, and not alerted. Only when all of this holds, on that host, at that sweep:

- the forward opens one port that leads to this host (`-R 2222:localhost:22`; any port here: what it exposes is the user's choice, and the note says the far end), asks for loopback on the far end (no bind address, or `localhost`), and has no `-J`, `-W`, `-F` or `-o HostName/ProxyJump/ProxyCommand/ProxyUseFdpass` of its own; a SOCKS proxy on either end (`-R 2222`, `-D`) never is;
- every connection its account has there goes to a listed server (on a port below 1024, which only root opens, or the one you give: `"vadur:2222"`), into the cluster below port 1024, or to a service on this host of a system account (below `min_uid`, or in `ignore_users`) -- and at least one to a listed server or into the cluster. A connection anywhere else, to a port of a person's on this host (its own `-L`, say: a relay) or above 1023 on a node, keeps it a warning. Connections *into* the account's own listening ports count only by where they come from (from this host, the cluster or a listed server: fine); a UDP socket counts by where it is connected to;
- the account has at least as many such connections as ssh processes (an ssh with no connection of its own, between connection attempts, could be going anywhere);
- no ssh of that account has a child process (`ProxyCommand` and `ProxyJump` from `~/.ssh/config` start one: a jump through a listed server to somewhere else) or relays (`-W`, `-J`, a proxy, a local forward from a socket);
- the account has no UDP socket open that isn't connected (it may send anywhere) beyond loopback, and no raw or ICMP socket.

The names in `trusted_hosts` are resolved by the collector (its own DNS and `/etc/hosts`), each sweep; `nomad watch probe` and `nomad config check` say what they resolve to. The far end is what was *asked*: that server's sshd decides (with `GatewayPorts`).

**Once verified, it stays verified while the same ssh runs.** An ssh keeps the connection it made for as long as it runs, so what its account does afterwards (a Jupyter reached through `ssh -L`, a name that doesn't resolve at one sweep) can't move its far end: the note says what is new ("... (the same ssh since; now: 1 connection to a person's port on this host)"). The probe names a forward's processes by a key (a hash of the boot, their pids and start times), only when each is ssh by its kernel name (`exec -a ssh` changes argv[0] alone; a user can set the kernel name too, so this stops only the simplest pose; where the probe runs as root, the program file must be the system's `/usr/bin/ssh`). It stops being kept when its account reaches anywhere at two of its last four sweeps -- campus, outside, a person's port on a listed server or a node, an open UDP or raw socket, no connection to a listed server (one such sweep, a `git pull`, is fine; a program posing as ssh that carries the forward elsewhere would show at more) -- or when you take its server off `trusted_hosts`. Without limit it rides through only what carries nothing elsewhere by itself: a connection to a person's port on this host (a Jupyter through `ssh -L`), and the account's other ssh processes, which are themselves counted. A new ssh (a new one on the same port, or `autossh` starting it again) is checked afresh: until a sweep finds its account quiet it is a warning, and alerts, its note saying why ("not verified: 1 connection outside; far end loopback"). An `autossh` row is verified when its ssh is ("as its ssh: ...").

The cluster's own networks are the private ones each host reaches directly, on an interface with no default route for that IP version (one with a default route is a way out, shared with whatever else is there); where a compute node's way out is the cluster network (through the head node), or a spare default route is on it, list it: `cluster_networks = ["10.1.1.0/24"]`.

**A site's own ProxyCommand** (FreeIPA's `/etc/ssh/ssh_config.d/04-ipa.conf` on older clients: `ProxyCommand /usr/bin/sss_ssh_knownhostsproxy`) gives every ssh a child process, so nothing is verified there; the note names the child by its kernel name, cut at 15 characters ("an ssh with a child process (sss_ssh_knownho: ...)"). The probe can't tell a site's proxy from a user's copy of it without root, so it doesn't try; newer sssd uses `KnownHostsCommand`, which starts no child. To see whether a host has one: `grep -ri proxycommand /etc/ssh/ssh_config /etc/ssh/ssh_config.d/`.

What it can't see without root: which process holds which connection. An account that hands ssh a ready connection through a socket of its own (`ProxyUseFdpass` in its `~/.ssh/config`, a relay of its own making), or runs a program of its own that poses as ssh, can hide where a forward goes, and so can two accounts working together; the check makes that deliberate, not easy. Like everything the watch sees, it sees only what runs when it looks: what runs between sweeps, it doesn't.

On compute nodes, programs listening are not findings unless `listeners_on_nodes = true`: MPI, NCCL, Dask and Ray open ports of their own there (and with it, a port in the kernel's ephemeral range is listed only). Listening on loopback only (VS Code's server, a Jupyter reached through `ssh -L`) is never one, nor is a local forward (`ssh -L`).

A host that answered before and hasn't for 3 sweeps raises its own alert: a probe that can't finish there sees nothing, and someone may be hiding.

Not seen: a forward set in someone's `~/.ssh/config` (it isn't on the command line), and what runs between sweeps.

## How

Every `interval_minutes` (15), a probe (`watch_probe.py`, standard library only, Python 3.6 or later) runs on the login node the collector runs on and, over ssh (BatchMode, no agent or X forwarding), on each compute node Slurm lists that is up, a few at a time, for at most `max_seconds`. It is piped to a fixed interpreter, the first of `python` (`/usr/bin/python3`, `/usr/libexec/platform-python`) that exists, with `-I -S`: not whatever `python3` comes first on the account's PATH, and no site-packages or `.pth` files, so nothing a user could put in the collector's way runs as its account. Nothing is installed on the nodes. It reads `/proc` as the collector's account: every account's processes and their command lines (at most 64 KB each), and the listening sockets with their owners (`/proc/net/tcp`, readable by every account). No root is needed, unless `/proc` is mounted with `hidepid`: then only the collector's own processes are seen (sockets still are), and `nomad watch status` says so. Which program listens on a port is known only to root (another account's sockets are its own): elsewhere a listener is its owner and port, and `ignore_ports` leaves out a sanctioned one.

## What is kept

The host, the account, the kind, the program's name (a short word: letters, digits, `. _ + -`; at most five per account and kind on a host, listeners too, then `+N more`), the port it forwards or listens on, and for an ssh forward where its account's connections go (inside, campus, outside), whether it is verified and why, as last seen (in the watch's own words, naming only the servers you listed, by the name or network you gave without its port, the cluster's own hosts, and the kernel name of a child process), and a key of its processes (a hash): **never a command line, or an address or host a user gave.** `--mask` replaces the server names in notes too, and `nomad watch probe --mask` shows no addresses or settings. Each finding has a first and a last time seen, and how many sweeps saw it, in `policy_findings`, which the hub gets with every other table: admins see NetIDs, as elsewhere in the Console. `policy_watch_hosts` keeps, per sweep, which hosts answered and what the probe could see there.

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
# trusted_hosts = []           # servers an ssh -R may go to, verified: ["vadur"], "vadur:2222",
#                              # an address or a network (ports below 1024 unless one is given)
# campus_networks = []         # public ranges that are campus, a label: ["141.166.0.0/16"]
# cluster_networks = []        # the cluster's own, where a host's default route is on it: ["10.1.1.0/24"]
# keep_days = 365              # findings not seen for this long are removed
```

The collector's account needs ssh to the nodes without a password (as `node_disks` does). Before turning it on, `nomad watch probe` (here) and `nomad watch probe NODE` show what a sweep would find, storing nothing.

## Looking at it

```
nomad watch list              findings of the last 7 days, the most serious first (* seen in the last sweep),
                              and verified forwards
nomad watch list --all        also what is only listed (ephemeral ports on compute nodes)
nomad watch list --mask       stand-ins for people and hosts, for anything shared outside ARC
nomad watch list --json
nomad watch status            the last sweep: each host, whether it answered, what it could see
nomad watch probe [HOST]      look now
```

## What it is not

It is a set of signs for a person to judge, not a verdict: an administrator's own tunnel, a sanctioned service or a research server may well be fine (`ignore_*`). It sees what runs while it looks (every 15 minutes), not what ran in between. It doesn't stop anything: closing these doors (compute nodes reachable only through Slurm with `pam_slurm_adopt`, outbound traffic from compute nodes allowed only where needed, limits on login nodes) is the cluster's configuration, and what is allowed is the site's policy.
