# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The policy watch (1.19): what runs on a cluster's login and compute nodes
that its rules usually forbid, for an administrator to look at. Read-only:
nothing is stopped, changed or reported to anyone but the site's admins.

Every ``interval_minutes`` (15), the probe (watch_probe.py, standard
library only) runs here (a login node) and, over ssh, on the compute nodes
Slurm lists that are up (``nodes = "slurm"``), a few at a time, for at most
``max_seconds``. It is piped to a fixed interpreter, the first of
``python`` (/usr/bin/python3, /usr/libexec/platform-python) that exists,
with -I -S (no PATH, no site-packages, no .pth files: nothing a user could
put in this account's way), over ssh without agent or X forwarding: nothing
is installed there.
It reads /proc as this account (no root needed, unless /proc hides other
accounts' processes: hidepid; then only this account's are seen, and
`nomad watch status` says so). What it finds, of accounts from ``min_uid``
(1000) up:

- tunnel: a program that opens a way in or out (ngrok, cloudflared, frp,
  chisel, Tailscale, ZeroTier, ...);
- ssh_reverse, ssh_socks: an ssh client forwarding a port from outside to
  here (-R), or a SOCKS proxy that carries this host's traffic out through
  another (-D). Each says where its account's connections on that host go
  (inside: this host, the cluster's own networks, listed servers; campus;
  outside: from what the kernel shows, never from what a user wrote).
  A reverse forward is verified -- listed, not alerted -- when it opens one
  port leading to this host, asks for loopback on the far end, has no proxy
  or jump, and its account's connections there go only to a listed server
  (``trusted_hosts``, and the cluster's own login hosts) on its port, into
  the cluster below port 1024 (where only root listens), or to a system
  account's service on that host; with no ssh of it starting a child (a
  proxy or jump) or relaying, and no UDP socket open beyond loopback (see
  watch_probe.py);
- code_tunnel: `code tunnel` (VS Code's relay through Microsoft);
- miner: a miner by name, or with a stratum pool on its command line;
- relay: a proxy or relay (socat listening, 3proxy, v2ray, shadowsocks, ...);
- escalation: a process running as root whose parent is owned by a regular
  account and isn't a sanctioned helper (sudo, su, pkexec, ...): the shape
  of a process that reached root outside the usual path (1.20);
- agent: an AI agent command-line program (Claude Code, Codex, aider, ...),
  listed, not alerted -- AI use is not misuse (``agents`` adds names; 1.20);
- auth_failures, setuid_new: from a root helper (scripts/watch_root_probe.py,
  ``root_report``; off by default), repeated failed privilege attempts per
  account and new setuid files in user-writable places -- counts only (1.20);
- listening: a program of a person's listening on a port open to the
  network, on a login node (``listeners_on_nodes = true`` for compute
  nodes too, where MPI, Dask or Ray open ports of their own; there, a port
  in the kernel's ephemeral range is listed, not alerted). Which program
  listens is known only where the account is root.
- gpu, gpu_served: a GPU compute process (nvidia-smi, ``watch_gpu``, on by
  default; 1.21), listed (gpu), or a warning (gpu_served) when it listens on
  a port open to the network or its account also runs a tunnel, relay or
  forward -- a GPU reachable from outside, the shape of reselling or an open
  inference server. The note says the memory held, whether it is detached (no
  terminal: the shape of an unattended job -- a heuristic, so it only annotates),
  and whether it is an AI agent. Graphics (Xorg, a browser) are not compute
  apps, so they don't show. ``gpu_serve_hosts`` names boxes where serving a
  GPU is sanctioned (then listed, not alerted). Per-process GPU attribution is
  opaque under MIG or in containers, and a GPU another account serves is
  caught through its account's exposure unless the probe runs as root.

The GPU workstations are asked as such: ``workstations`` lists hosts probed
over ssh with the role "workstation" (login and compute nodes aside), so the
GPU findings reach the hub and the Console the same way (1.21).

Behaviour (1.22): the tells an AI agent (or a careless script) driving the
cluster over the user's own ssh leaves -- which look like the user to the
/proc watch, so the watch reads what was done, not who typed it.
- heavy (``watch_heavy``, on by default): a user process using real CPU by
  lifetime average (its whole CPU time / its age) of at least ``heavy_cpu``
  cores, having used at least ``heavy_min_seconds`` CPU-seconds, on a **login**
  node (a compute node is where jobs are meant to be heavy) -- a computation
  run where it shouldn't be;
- from this site's ``jobs`` table (``watch_jobs``, on by default; nothing where
  there is none): job_burst (``burst_jobs`` sbatches within ``burst_minutes`` --
  listed, not alerted, since a parameter sweep bursts too; an array counts as
  one), fail_fast (``fail_fast_jobs`` jobs that FAILED within
  ``fail_fast_seconds`` -- an agent resubmitting what it doesn't understand),
  job_churn (the same job name resubmitted ``churn_repeats`` times with
  cancellations between).

A host that answered before and hasn't for 3 sweeps raises its own alert:
a probe that can't finish there sees nothing. Findings not seen for
``keep_days`` (365) are removed.

Kept: the host, the account, the kind, the program's name and the port it
forwards or listens on -- never a command line, a host it connects to or an
address. A program's name is kept to a short word (letters, digits, . _ +
-), at most five per account and kind on a host (listeners too), then +N
more. Each finding has a first and a last time seen, and how many sweeps saw
it. Findings raise one alert per account and kind, on whatever hosts, and
one more when that group gains something new (both deduplicated; reminders
as for every alert), which name no one unless ``owners_in_alerts = true``
(alerts go to Slack and webhooks too; their key is the finding's number
here, not the account). ``ignore_users``, ``ignore_programs`` and
``ignore_ports`` leave out what a site allows (a sanctioned service).

    [collectors.policy_watch]
    enabled = true
    # interval_minutes = 15
    # nodes = "slurm"              # or a list, or [] for this host only
    # login_hosts = []             # other login nodes, asked over ssh
    # min_uid = 1000
    # listeners_on_nodes = false
    # ignore_users = ["slurm", "munge"]   # services' accounts above min_uid
    # watch_self = false           # this account's own (an admin's) left out too
    # ignore_programs = []         # e.g. ["tailscaled"] where IT runs it
    # ignore_ports = []
    # owners_in_alerts = false
    # parallel = 8
    # timeout = 15
    # max_seconds = 180
    # ssh_user = "..."
    # python = ["/usr/bin/python3", "/usr/libexec/platform-python"]
    # trusted_hosts = []           # e.g. ["vadur"]: servers an ssh -R may go to (ports below 1024,
    #                              # or "vadur:2222"); names, addresses or networks
    # campus_networks = []         # e.g. ["141.166.0.0/16"]: public ranges that are campus (a label)
    # cluster_networks = []        # e.g. ["10.1.1.0/24"]: where a host's way out is the cluster network
    # agents = []                  # AI agent programs to add to the built-in list (listed, not alerted)
    # root_report = ""             # path a root helper writes (scripts/watch_root_probe.py); off by default
    # workstations = []            # e.g. ["ml-ws01"]: GPU workstations to ask over ssh (role "workstation")
    # watch_gpu = true             # GPU compute processes (nvidia-smi); nothing where there is no GPU
    # gpu_min_mem = 256            # a compute context smaller than this (MiB) is a probe, not a workload
    # gpu_serve_hosts = []         # e.g. ["shared-infer01"]: boxes where serving a GPU is sanctioned (listed)
    # watch_heavy = true           # heavy use of a login node (a user process holding real CPU)
    # heavy_cpu = 2.0              # cores (by lifetime average) at or above which it is heavy
    # heavy_min_seconds = 600      # CPU-seconds it must have used (so a brief spike doesn't count)
    # watch_jobs = true            # job-submission patterns from this site's jobs table
    # job_window_hours = 24        # how far back the job patterns look
    # burst_jobs = 60              # sbatches within burst_minutes to list a burst (an array is one)
    # burst_minutes = 10
    # fail_fast_jobs = 15          # jobs that FAILED within fail_fast_seconds to warn
    # fail_fast_seconds = 60
    # churn_repeats = 8            # resubmissions of one job (with cancellations) to warn
    # keep_days = 365

Tables: ``policy_findings`` (one row per finding: host, role, username,
uid, kind, program, detail, reach (an ssh forward's: inside, campus or
outside, as last seen), verified, note (why, as last seen), procs (a key of
the ssh processes a verified forward was verified with: a hash) and away
(which of a kept one's last four sweeps found its account reaching elsewhere),
first_seen, last_seen, sweeps, severity (as last seen): copied to the hub);
``policy_watch_hosts`` (each host at each sweep, kept 7 days: whether it
answered, and what the probe could see there) and ``policy_watch_sweeps``
(when each sweep started): this host's own state, not copied to the hub.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timedelta
from typing import Any

from nomad.collectors.base import BaseCollector, CollectionError, find_tool, tool_env
from nomad.collectors.node_disks import HOST_RE, nodes_from_sinfo

logger = logging.getLogger(__name__)

MARK = "__NOMAD_WATCH_BEGINS__"
PYTHONS = ("/usr/bin/python3", "/usr/libexec/platform-python")
SERVICE_ACCOUNTS = ("slurm", "munge")
PATH_OK = re.compile(r"/[A-Za-z0-9/._+-]+")       # fullmatch: no newline, no shell
NAME_OK = re.compile(r"^(\+\d+ more|[A-Za-z0-9._+ -]{1,40})$")
SEVERITY = {"tunnel": "warning", "ssh_reverse": "warning", "ssh_socks": "warning",
            "code_tunnel": "warning", "miner": "critical", "relay": "warning",
            "listening": "warning", "escalation": "critical", "agent": "info",
            "auth_failures": "warning", "setuid_new": "warning",
            "gpu": "info", "gpu_served": "warning",
            "heavy": "warning", "job_burst": "info", "fail_fast": "warning", "job_churn": "warning"}
LABELS = {"tunnel": "a tunnel program", "ssh_reverse": "an ssh reverse forward (-R)",
          "ssh_socks": "an ssh SOCKS proxy (-D)", "code_tunnel": "a VS Code tunnel",
          "miner": "a miner", "relay": "a relay or proxy",
          "listening": "a program listening on a port open to the network",
          "escalation": "a process that reached root outside the usual path",
          "agent": "an AI agent", "auth_failures": "repeated failed privilege attempts",
          "setuid_new": "a new setuid or setgid file",
          "gpu": "a process using a GPU", "gpu_served": "a GPU served to the network",
          "heavy": "heavy use of a login node", "job_burst": "a burst of job submissions",
          "fail_fast": "jobs failing within seconds, again and again",
          "job_churn": "the same job cancelled and resubmitted over and over"}
# Kinds the probe sends that are a person's finding of their own (not an ssh forward's reach).
ESCALATION = "escalation"
REACH = {"inside": "stays inside", "campus": "on campus", "outside": "connects outside"}
# Why a forward isn't verified, as the probe says it (watch_probe.reach, check), in this order.
WHY = {"outside": "{n} connection{s} outside", "campus": "{n} connection{s} on campus",
       "host_port": "{n} connection{s} to a person's port on this host",
       "cluster_port": "{n} connection{s} into the cluster above port 1023",
       "server_port": "{n} connection{s} to a listed server on another port",
       "udp": "an open UDP socket", "raw": "a raw or ICMP socket",
       "none": "no connection to a listed server or into the cluster",
       "carrier": "fewer such connections than ssh processes",
       "child": "an ssh with a child process (a proxy or jump)", "relay": "an ssh that relays (-W, -J, a proxy)",
       "proxied": "a proxy or jump on its command line", "far": "asks for {far} on the far end",
       "to": "leads to {to}", "unchecked": "not checked"}
FAR = ("loopback", "an address", "every address", "a socket", "unknown")
TO = ("this host", "another host", "a socket", "socks", "unknown")
ENTRY = re.compile(r"^(?:\[(?P<b>[^\]]+)\]|(?P<h>[^:\s\[\]]+))(?::(?P<port>\d{1,5}))?$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    role TEXT,
    username TEXT,
    uid INTEGER,
    kind TEXT NOT NULL,
    program TEXT,
    detail TEXT,
    reach TEXT,
    verified INTEGER,
    note TEXT,
    procs TEXT,
    away INTEGER,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    sweeps INTEGER DEFAULT 1,
    severity TEXT
);
CREATE INDEX IF NOT EXISTS idx_policy_findings_key ON policy_findings(host, uid, kind, program, detail);
CREATE INDEX IF NOT EXISTS idx_policy_findings_seen ON policy_findings(last_seen);
CREATE TABLE IF NOT EXISTS policy_watch_hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    host TEXT NOT NULL,
    role TEXT,
    answered INTEGER,
    processes INTEGER,
    others INTEGER,
    others_readable INTEGER,
    hidepid INTEGER,
    listener_programs INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_policy_watch_hosts ON policy_watch_hosts(host, timestamp);
CREATE TABLE IF NOT EXISTS policy_watch_sweeps (
    timestamp TEXT NOT NULL,
    asked INTEGER,
    answered INTEGER
);
"""


COLUMNS = {"policy_findings": (("reach", "TEXT"), ("verified", "INTEGER"), ("note", "TEXT"), ("procs", "TEXT"),
                               ("away", "INTEGER")),
           "policy_watch_hosts": (("listener_programs", "INTEGER"),)}


def ensure_schema(conn) -> None:
    """The tables, with the columns later releases added (1.19.1: verified, note, procs, away)."""
    conn.executescript(SCHEMA)
    for table, cols in COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def _resolve(name: str) -> list[str]:
    try:
        return sorted({ai[4][0].split("%")[0] for ai in socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)})
    except (OSError, UnicodeError):
        return []


def trusted_entries(entries, *, here: str = "", login_hosts=(), resolve=None) -> tuple[list, list]:
    """(label, network, port) for each address of the cluster's own hosts
    (this one, ``login_hosts``) and of ``trusted_hosts``, resolved here (not
    by a user's ssh config): a name, an address or a network, with :PORT
    (an IPv6 one in brackets), else port 0: ports below 1024, which only
    root opens. And notes on what couldn't be used."""
    resolve = resolve or _resolve
    out, notes, seen, loop = [], [], set(), set()

    def add(label, addrs, port):
        """How many were usable, and how many of those were new."""
        usable = new = 0
        for a in addrs:
            try:
                net = ipaddress.ip_network(a, strict=False)
            except ValueError:
                continue
            if net.network_address.is_loopback or net.prefixlen == 0:
                loop.add(label)
                continue
            usable += 1
            if (str(net), port) in seen:
                continue
            seen.add((str(net), port))
            out.append((label, str(net), port))
            new += 1
            if net.prefixlen < (16 if net.version == 4 else 48):
                notes.append(f"trusted host {label[:60]}: a very wide network (/{net.prefixlen})")
        return usable, new

    def said(label, got):
        usable, new = got
        if not usable:
            notes.append(f"trusted host {label[:60]}: " + ("not used (loopback, or everything)" if label in loop
                                                           else "not found"))
        elif not new:
            notes.append(f"trusted host {label[:60]}: already listed")

    for e in [entries] if isinstance(entries, str) else entries or []:
        e = e.strip() if isinstance(e, str) else ""
        try:
            net = ipaddress.ip_network(e, strict=False)
            said(e, add(e, [str(net)], 0))
            continue
        except ValueError:
            pass
        m = ENTRY.fullmatch(e)
        port = int(m.group("port")) if m and m.group("port") else 0
        host = (m.group("b") or m.group("h")) if m else ""
        if not m or (m.group("port") and not 0 < port < 65536):
            notes.append(f"trusted host {e[:60]!r}: not a name, an address or a network")
            continue
        try:
            addrs, label = [str(ipaddress.ip_network(host, strict=False))], host
        except ValueError:
            if not HOST_RE.fullmatch(host):
                notes.append(f"trusted host {e[:60]!r}: not a name, an address or a network")
                continue
            addrs, label = resolve(host), host
        said(label, add(label, addrs, port))
    for h in [*login_hosts, here]:
        if h and HOST_RE.fullmatch(h):
            add(h, resolve(h), 0)
    return out, notes


def entry_labels(entries, *, here: str = "", login_hosts=()) -> set:
    """The labels trusted_entries would give, without resolving anything:
    what the site names now (a note naming one it no longer does is stale)."""
    out = {h for h in [*login_hosts, here] if h}
    for e in _list(entries):
        e = e.strip()
        try:
            ipaddress.ip_network(e, strict=False)
            out.add(e)
            continue
        except ValueError:
            pass
        m = ENTRY.fullmatch(e)
        if m:
            out.add(m.group("b") or m.group("h"))
    return out


KEYS = {"enabled", "interval", "interval_minutes", "nodes", "login_hosts", "min_uid", "listeners_on_nodes",
        "ignore_users", "watch_self", "ignore_programs", "ignore_ports", "owners_in_alerts", "parallel", "timeout",
        "max_seconds", "ssh_user", "python", "trusted_hosts", "campus_networks", "cluster_networks", "keep_days",
        "agents", "root_report", "alerts_enabled", "max_retries", "retry_delay",
        "workstations", "watch_gpu", "gpu_min_mem", "gpu_serve_hosts",
        "watch_heavy", "heavy_cpu", "heavy_min_seconds", "watch_jobs", "job_window_hours",
        "burst_jobs", "burst_minutes", "fail_fast_jobs", "fail_fast_seconds", "churn_repeats"}

# Job-behaviour thresholds (1.22) and their defaults; validated as whole numbers.
JOB_DEFAULTS = {"window_hours": 24, "burst_jobs": 60, "burst_minutes": 10,
                "fail_fast_jobs": 15, "fail_fast_seconds": 60, "churn_repeats": 8}
_JOB_KEYS = {"job_window_hours": "window_hours", "burst_jobs": "burst_jobs", "burst_minutes": "burst_minutes",
             "fail_fast_jobs": "fail_fast_jobs", "fail_fast_seconds": "fail_fast_seconds",
             "churn_repeats": "churn_repeats"}


def _list(v) -> list:
    """A setting that takes a list, given one, or one string."""
    return [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def networks(values, what: str) -> tuple[list[str], list[str]]:
    """Networks from a setting (campus_networks, cluster_networks), and notes."""
    out, notes = [], []
    for n in _list(values):
        try:
            net = ipaddress.ip_network(n.strip(), strict=False)
        except ValueError:
            notes.append(f"{what} {n[:60]!r}: not a network")
            continue
        if net.prefixlen == 0:
            notes.append(f"{what} {n[:60]}: everything, not used")
            continue
        if net.prefixlen < (16 if net.version == 4 else 48):
            notes.append(f"{what} {n[:60]}: a very wide network (/{net.prefixlen})")
        out.append(str(net))
    return out, notes


def describe_trusted(entries) -> list[str]:
    """Trusted entries as a person reads them: each label, its addresses, its ports."""
    by: dict = {}
    for label, net, port in entries:
        nets, ports = by.setdefault(label, ([], []))
        shown = net.removesuffix("/32").removesuffix("/128")
        if shown not in nets:
            nets.append(shown)
        if port not in ports:
            ports.append(port)
    return [f"{label} = {', '.join(nets)} ("
            + ", ".join("ports below 1024" if p == 0 else f"port {p}" for p in sorted(ports)) + ")"
            for label, (nets, ports) in by.items()]


def config_problems(conf, *, resolve=None) -> tuple[list[str], list[str]]:
    """What `nomad config check` says of [collectors.policy_watch]: what is
    wrong (a setting it doesn't know, a list that isn't one, a trusted host
    not found, a network that isn't one), and what trusted_hosts resolve to
    here."""
    problems: list[str] = []
    if not isinstance(conf, dict):
        return ["[collectors.policy_watch] is not a table"], []
    for k in sorted(set(conf) - KEYS):
        hint = " (1.19.1: trusted_hosts lists the servers an ssh -R may go to)" if k == "inside_hosts" else ""
        problems.append(f"[collectors.policy_watch] {k}: not a setting{hint}")
    lists = {}
    like = {"trusted_hosts": '["vadur"]', "campus_networks": '["141.166.0.0/16"]',
            "cluster_networks": '["10.1.1.0/24"]', "login_hosts": '["login02"]', "ignore_users": '["slurm"]',
            "ignore_programs": '["tailscaled"]', "agents": '["myorg-agent"]',
            "workstations": '["ml-ws01"]', "gpu_serve_hosts": '["shared-infer01"]'}
    for k, ex in like.items():
        v = conf.get(k)
        ok = v is None or isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v))
        if not ok:
            problems.append(f"[collectors.policy_watch] {k}: a list of strings, like {ex}")
        lists[k] = _list(v) if ok else []
    gm = conf.get("gpu_min_mem")
    if gm is not None and not (isinstance(gm, int) and not isinstance(gm, bool) and gm >= 0):
        problems.append("[collectors.policy_watch] gpu_min_mem: a whole number of MiB, like 256")
    for b in ("watch_gpu", "watch_heavy", "watch_jobs"):
        if conf.get(b) is not None and not isinstance(conf.get(b), bool):
            problems.append(f"[collectors.policy_watch] {b}: true or false")
    for k in _JOB_KEYS:
        v = conf.get(k)
        if v is not None and not (isinstance(v, int) and not isinstance(v, bool) and v > 0):
            problems.append(f"[collectors.policy_watch] {k}: a whole number greater than 0, like {JOB_DEFAULTS[_JOB_KEYS[k]]}")
    hc = conf.get("heavy_cpu")
    if hc is not None and not (isinstance(hc, (int, float)) and not isinstance(hc, bool) and hc > 0):
        problems.append("[collectors.policy_watch] heavy_cpu: a number of cores greater than 0, like 2")
    hm = conf.get("heavy_min_seconds")
    if hm is not None and not (isinstance(hm, int) and not isinstance(hm, bool) and hm >= 0):
        problems.append("[collectors.policy_watch] heavy_min_seconds: a whole number of seconds, like 600")
    entries, notes = trusted_entries(lists["trusted_hosts"], resolve=resolve)
    problems += [f"[collectors.policy_watch] {n}" for n in notes]
    campus, n1 = networks(lists["campus_networks"], "campus network")
    cluster, n2 = networks(lists["cluster_networks"], "cluster network")
    problems += [f"[collectors.policy_watch] {n}" for n in n1 + n2]
    rr = conf.get("root_report")
    if rr not in (None, "") and not (isinstance(rr, str) and PATH_OK.fullmatch(rr)):
        problems.append("[collectors.policy_watch] root_report: an absolute path with no spaces or shell "
                        'characters, like "/var/lib/nomad-watch/root_signals.json"')
    info = [f"trusted: {line}" for line in describe_trusted(entries)]
    if campus:
        info.append("campus: " + ", ".join(campus) + " (and private addresses beyond the cluster)")
    if cluster:
        info.append("cluster: " + ", ".join(cluster) + " (and the private networks each host reaches directly)")
    if lists["workstations"]:
        info.append("workstations: " + ", ".join(lists["workstations"]) + " (asked over ssh, as a workstation)")
    # Echo the GPU watch only when the site has set something about it, as the lines above do.
    if "watch_gpu" in conf or lists["gpu_serve_hosts"] or lists["workstations"]:
        if conf.get("watch_gpu") is False:
            info.append("GPU watch: off")
        else:
            allow = (" (serving allowed on " + ", ".join(lists["gpu_serve_hosts"]) + ")") \
                if lists["gpu_serve_hosts"] else ""
            info.append("GPU watch: on; a GPU reachable from the network is a warning" + allow)
    behaviour_keys = {"watch_heavy", "heavy_cpu", "heavy_min_seconds", "watch_jobs", *_JOB_KEYS}
    if behaviour_keys & set(conf):
        on = []
        if conf.get("watch_heavy") is not False:
            on.append("heavy login-node use")
        if conf.get("watch_jobs") is not False:
            on.append("job bursts, fail-fast and churn")
        info.append("behaviour: " + ("; ".join(on) if on else "off"))
    return problems, info


def probe_source() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "watch_probe.py"), encoding="utf-8") as f:
        return f.read()


def parse_probe(stdout: str) -> dict | None:
    """The probe's JSON: the first line after the mark (a login banner may
    come before it), else the last line that is JSON."""
    text = stdout or ""
    if MARK in text:
        text = text.split(MARK, 1)[1]
    for line in reversed([ln for ln in text.splitlines() if ln.strip()]):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and "findings" in d:
            return d
    return None


def _clean(v, ok=NAME_OK) -> str:
    v = "" if v is None else str(v)
    return v if (v == "" or ok.fullmatch(v)) else "?"


def forward_note(f: dict, servers=()) -> tuple[bool, str, str, dict]:
    """An ssh forward's (verified, reach, note, why) from the probe's answer:
    verified only for a -R the probe found nothing against, of a shape it
    may be; the note in this module's own words (no command line, no
    address: the names of listed servers are the site's)."""
    why = f.get("why") if isinstance(f.get("why"), dict) else {"unchecked": 1}
    why = {k: (v if isinstance(v, int) and 0 < v < 100000 else 1) for k, v in why.items() if k in WHY}
    if set(f.get("why") or {}) - set(WHY):
        why["unchecked"] = 1
    far = f.get("far") if f.get("far") in FAR else "unknown"
    to = f.get("to") if f.get("to") in TO else "unknown"
    reach = f.get("reach") if f.get("reach") in REACH else ""
    if f.get("kind") == "ssh_socks":
        return False, reach, f"a SOCKS proxy here, on {far}", {"unchecked": 1}
    ok = (f.get("kind") == "ssh_reverse" and f.get("verified") is True and not why and far == "loopback"
          and to == "this host" and f.get("proxied") is False and str(f.get("detail", "")).isdigit())
    names = []
    for i in f.get("servers") or []:
        if isinstance(i, int) and 0 <= i < len(servers) and servers[i] not in names:
            names.append(servers[i])
    if ok:
        to_where = ", ".join(_clean(n, re.compile(r"[\w.:/\[\]-]{1,64}")) for n in names[:3]) or "the cluster"
        return True, reach, f"verified: to {to_where}; far end loopback", {}
    if not why:
        why = {"unchecked": 1}
    parts = [WHY[k].format(n=why[k], s="s" if why[k] != 1 else "", far=far, to=to) for k in WHY if k in why]
    kids = [_clean(c, re.compile(r"[A-Za-z0-9._+-]{1,32}")) for c in (f.get("children") or [])[:3]
            if isinstance(c, str)]
    if "child" in why and kids:
        parts[[k for k in WHY if k in why].index("child")] = \
            f"an ssh with a child process ({', '.join(kids)}: a proxy or jump)"
    if "far" not in why and far in ("loopback", "an address", "every address", "a socket"):
        parts.append(f"far end {far}")
    return False, reach, "not verified: " + "; ".join(parts), why


def gpu_note(g: dict) -> str:
    """A GPU finding's note from the probe's ``gpu`` facts, in this module's
    own words: how much GPU memory, whether it is reachable from the network,
    whether it is an AI agent, and whether it is detached (no terminal: the
    shape of a job running unattended -- a heuristic, so it only annotates)."""
    if not isinstance(g, dict):
        return ""
    parts = []
    mem = g.get("mem_mb")
    if isinstance(mem, int) and mem > 0:
        parts.append(f"~{mem / 1024:.1f} GB on the GPU" if mem >= 1024 else f"~{mem} MB on the GPU")
    if g.get("served"):
        parts.append("reachable from the network")
    if g.get("agent"):
        parts.append(f"an AI agent ({_clean(str(g['agent']), re.compile(r'[A-Za-z0-9._+-]{1,32}'))})")
    if g.get("detached"):
        parts.append("detached, no terminal")
    return "; ".join(parts)


def findings_of(probe: dict, host: str, role: str, *, listeners_on_nodes: bool = False,
                ignore_users=(), ignore_programs=(), ignore_ports=(), servers=(),
                gpu_serve_hosts=()) -> list[dict]:
    """The probe's answer as findings (rows for policy_findings, without
    times). What a user writes never lowers a finding: only a listener on
    an ephemeral port on a compute node (with listeners_on_nodes) is listed
    rather than alerted, and a reverse forward the probe verified from what
    the kernel shows (forward_note; ``servers``: the labels of the trusted
    entries the probe was given, by index). An ignore list leaves a finding
    out only if it names every name its process goes by."""
    out = []
    users, programs, ports = set(ignore_users), set(ignore_programs), {str(p) for p in ignore_ports}
    user_ok = re.compile(r"^[\w.$@-]{1,64}$")

    def ignored(prog, aka):
        names = {n for n in (aka or []) if n} or ({prog} if prog else set())
        return bool(names) and names <= programs

    port_kinds = {"ssh_reverse", "ssh_socks", "listening"}      # kinds whose detail is a port
    for f in probe.get("findings") or []:
        prog, detail = _clean(f.get("program")), _clean(f.get("detail"))
        aka = [_clean(n) for n in f.get("aka") or []]
        kind = f.get("kind") if f.get("kind") in SEVERITY else "other"
        if f.get("user") in users or ignored(prog, aka) or (detail and detail in ports and kind in port_kinds):
            continue
        verified, reach, note, why = False, "", None, None
        if kind in ("ssh_reverse", "ssh_socks"):
            verified, reach, note, why = forward_note(dict(f, kind=kind, detail=detail), servers)
        if kind == ESCALATION and detail:
            note = f"its parent is a {detail}"            # the program that spawned the root process
        if kind in ("gpu", "gpu_served"):
            g = f.get("gpu") if isinstance(f.get("gpu"), dict) else {}
            note = gpu_note(g)
            if kind == "gpu_served" and host in set(gpu_serve_hosts):
                kind = "gpu"                              # serving a GPU is sanctioned on this host
                note = (note + "; serving allowed here").strip("; ") if note else "serving allowed here"
            elif kind == "gpu_served" and role == "node" and g.get("via") == "listen" \
                    and (not listeners_on_nodes or g.get("ephemeral")):
                # On a compute node a GPU job opens ports of its own (a distributed
                # job's rendezvous): listed, as a listener there is, unless the site
                # has turned node listeners on -- and even then an ephemeral port is
                # listed. A GPU served through its account's tunnel still warns.
                kind = "gpu"
        if kind == "heavy":
            if role != "login":
                continue                                  # heavy compute is normal off a login node
            note = heavy_note(f.get("heavy"))
        sev = "info" if verified else SEVERITY.get(kind, "info")
        procs = f.get("procs") if isinstance(f.get("procs"), str) and re.fullmatch(r"[0-9a-f]{16}", f["procs"]) \
            else None
        out.append({"host": host, "role": role, "username": _clean(f.get("user"), user_ok),
                    "uid": f.get("uid") if isinstance(f.get("uid"), int) else None,
                    "kind": kind, "program": prog, "detail": detail, "reach": reach,
                    "verified": verified if kind in ("ssh_reverse", "ssh_socks") else None, "note": note,
                    "procs": procs if kind == "ssh_reverse" and not prog.startswith("+") else None,
                    "why": why, "severity": sev})
    if role == "login" or listeners_on_nodes:
        for s in probe.get("listeners") or []:
            prog = _clean(s.get("program"))
            aka = [_clean(n) for n in s.get("aka") or []]
            if s.get("scope") != "network" or s.get("user") in users or ignored(prog, aka) \
                    or str(s.get("port")) in ports:
                continue
            eph = bool(s.get("ephemeral"))
            port = s.get("port")
            out.append({"host": host, "role": role, "username": _clean(s.get("user"), user_ok),
                        "uid": s.get("uid") if isinstance(s.get("uid"), int) else None,
                        "kind": "listening", "program": prog if not str(port).startswith("+") else str(port),
                        "detail": "" if str(port).startswith("+") else _clean(port),
                        "reach": "ephemeral" if eph else "", "verified": None, "note": None,
                        "severity": "info" if (eph and role == "node") else SEVERITY["listening"]})
    for r in probe.get("root") or []:            # findings a root helper left (watch_root_probe.py)
        kind = r.get("kind")
        if kind not in ("auth_failures", "setuid_new"):
            continue                             # root_report meta and anything unknown: not a finding
        prog, detail = _clean(r.get("program")), _clean(r.get("detail"))
        if r.get("user") in users:
            continue
        out.append({"host": host, "role": role, "username": _clean(r.get("user"), user_ok),
                    "uid": r.get("uid") if isinstance(r.get("uid"), int) else None,
                    "kind": kind, "program": prog, "detail": detail, "reach": "", "verified": None,
                    "note": None, "severity": SEVERITY[kind]})
    return out


KEPT = " (the same ssh since; now: "
# What a forward kept verified may meet sweep after sweep: its account's other doings that carry nothing
# elsewhere by themselves (a Jupyter through ssh -L: a person's port here; another ssh, a jump, a relay:
# their own connections are counted). Anything else of KEPT_THROUGH at one of its last four sweeps only
# (a git pull, a name not resolving): what its account reaches elsewhere at two of them ends it.
FREE = {"host_port", "carrier", "child", "relay"}
KEPT_THROUGH = FREE | {"cluster_port", "server_port", "udp", "raw", "none", "campus", "outside"}


def keep(f: dict, old, names=None) -> None:
    """A reverse forward verified before stays verified while the same ssh
    processes hold it (``procs``: the probe's key of their pids and start
    times, given only when each is ssh by its kernel name): an ssh keeps the
    connection it made, so what its account does since (a Jupyter, a git
    pull, a name not resolving at one sweep) doesn't move its far end. Not
    when the site no longer names its server (``names``: entry_labels), nor
    when its account reaches anywhere but FREE's at two of its last four
    sweeps (``away``: those sweeps, a bit each, the latest lowest; reset when
    it is verified afresh). The note says what is new. ``old``: (id,
    verified, procs, note, severity, away) as stored."""
    f["away"] = 0
    if f.get("kind") != "ssh_reverse" or f.get("verified") or not old or old[1] != 1:
        return
    if not f.get("procs") or old[2] != f["procs"]:
        return
    was = (old[3] or "").split(KEPT)[0]
    if not was.startswith("verified: to "):
        return
    servers = was[len("verified: to "):].split(";")[0]
    if names is not None and servers != "the cluster" and \
            not all(n.strip() in names for n in servers.split(",")):
        return
    why = f.get("why") or {}
    if set(why) - KEPT_THROUGH:
        return
    away = (((old[5] or 0) << 1) | (1 if set(why) - FREE else 0)) & 0b1111
    if bin(away).count("1") >= 2:
        return
    now = (f.get("note") or "").removeprefix("not verified: ")
    room = 400 - len(was) - len(KEPT) - 1
    now = now if len(now) <= room else now[:max(0, room - 3)] + "..."
    f.update(verified=True, severity="info", note=f"{was}{KEPT}{now})", away=away)


def heavy_note(h: dict) -> str:
    """A heavy-use finding's note: the CPU it holds (by lifetime average) and,
    when large, the memory."""
    if not isinstance(h, dict):
        return ""
    parts = []
    cores = h.get("cores")
    if isinstance(cores, (int, float)) and cores > 0:
        parts.append(f"~{cores:g} cores, sustained")
    rss = h.get("rss_mb")
    if isinstance(rss, int) and rss >= 1024:
        parts.append(f"{rss / 1024:.1f} GB")
    return "; ".join(parts)


# -- behaviour from the jobs table (1.22): burst, fail-fast, churn --------------------

def _state_word(s) -> str:
    """A Slurm state word: 'CANCELLED by 123' -> 'CANCELLED'."""
    return str(s or "").strip().upper().split(" ")[0]


def _jtime(s):
    try:
        return datetime.fromisoformat(str(s)[:19].replace("T", " ").replace(" ", "T"))
    except (ValueError, TypeError):
        return None


def _max_in_window(times: list, window: timedelta) -> int:
    """The most of ``times`` (sorted) that fall within any ``window``."""
    best = j = 0
    for i in range(len(times)):
        while times[i] - times[j] > window:
            j += 1
        best = max(best, i - j + 1)
    return best


def _uid_of(name: str):
    try:
        import pwd
        return pwd.getpwnam(name).pw_uid
    except (KeyError, OSError, TypeError):
        return None


def job_findings(conn, host: str, role: str, now: datetime, cfg: dict) -> list[dict]:
    """Per-user job-behaviour findings from the local ``jobs`` table (1.22):
    the patterns an AI agent (or a careless script) driving the cluster over
    the user's own ssh leaves -- which look like the user to the /proc watch.
    An array counts as one submission (its tasks share a submit time), so a
    legitimate array isn't a burst. Signs for an admin to judge, not verdicts.

      job_burst  (info)    at least ``burst_jobs`` sbatches within
                           ``burst_minutes`` -- normal for a parameter sweep,
                           so listed, not alerted;
      fail_fast  (warning) at least ``fail_fast_jobs`` jobs that FAILED within
                           ``fail_fast_seconds`` -- the shape of an agent
                           resubmitting something it doesn't understand;
      job_churn  (warning) the same job name resubmitted at least
                           ``churn_repeats`` times with cancellations between.
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    except sqlite3.OperationalError:
        return []
    if not {"user_name", "submit_time", "state"} <= cols:
        return []
    rt = "runtime_seconds" if "runtime_seconds" in cols else "NULL"
    jn = "job_name" if "job_name" in cols else "NULL"
    cutoff = (now - timedelta(hours=cfg["window_hours"])).isoformat(timespec="seconds")
    try:
        rows = conn.execute(
            f"SELECT user_name, submit_time, state, {rt} AS runtime, {jn} AS job_name "
            "FROM jobs WHERE submit_time >= ? AND user_name IS NOT NULL AND user_name != ''",
            (cutoff,)).fetchall()
    except sqlite3.OperationalError:
        return []
    by_user: dict = {}
    for r in rows:
        by_user.setdefault(r["user_name"], []).append(r)
    user_ok = re.compile(r"^[\w.$@-]{1,64}$")
    out = []

    def finding(user, kind, note):
        return {"host": host, "role": role, "username": _clean(user, user_ok), "uid": _uid_of(user),
                "kind": kind, "program": "", "detail": "", "reach": "", "verified": None,
                "note": note, "procs": None, "why": None, "severity": SEVERITY.get(kind, "warning")}

    for user, js in by_user.items():
        subs = sorted({t for t in (_jtime(j["submit_time"]) for j in js) if t})   # distinct: an array is one
        n_burst = _max_in_window(subs, timedelta(minutes=cfg["burst_minutes"]))
        if n_burst >= cfg["burst_jobs"]:
            out.append(finding(user, "job_burst", f"{n_burst} job submissions within {cfg['burst_minutes']} min"))
        ff = {_jtime(j["submit_time"]) for j in js
              if _state_word(j["state"]) == "FAILED" and isinstance(j["runtime"], (int, float))
              and 0 <= j["runtime"] < cfg["fail_fast_seconds"]}
        ff.discard(None)
        if len(ff) >= cfg["fail_fast_jobs"]:
            out.append(finding(user, "fail_fast",
                               f"{len(ff)} jobs failed within {cfg['fail_fast_seconds']}s of starting"))
        by_name: dict = {}
        for j in js:
            b = by_name.setdefault(j["job_name"] or "", {"subs": set(), "cancels": set()})
            t = _jtime(j["submit_time"])
            if t:
                b["subs"].add(t)
                if _state_word(j["state"]) == "CANCELLED":
                    b["cancels"].add(t)
        worst = max(((len(b["subs"]), len(b["cancels"])) for b in by_name.values()), default=(0, 0))
        if worst[0] >= cfg["churn_repeats"] and worst[1] >= max(2, cfg["churn_repeats"] // 2):
            out.append(finding(user, "job_churn",
                               f"{worst[0]} resubmissions of one job, {worst[1]} cancelled"))
    return out


class PolicyWatchCollector(BaseCollector):
    name = "policy_watch"
    description = "Tunnels, miners, relays, exposed services and GPUs on login, compute and workstation hosts"
    default_interval = 900

    def __init__(self, config: dict[str, Any], db_path):
        super().__init__(config, db_path)
        c = self.config
        self.every = timedelta(minutes=float(c.get("interval_minutes", 15) or 15))
        self.parallel = max(1, int(c.get("parallel", 8) or 8))
        self.timeout = max(3, int(c.get("timeout", 15) or 15))
        self.max_seconds = max(5.0, float(c.get("max_seconds", 180) or 180))
        self._max_retries = 1
        self.ssh_user = c.get("ssh_user") or None
        nodes = c.get("nodes", "slurm")
        self.nodes = nodes if nodes == "slurm" else [n for n in (nodes or []) if isinstance(n, str)]
        self.login_hosts = [h for h in _list(c.get("login_hosts")) if HOST_RE.fullmatch(h)]
        self.min_uid = int(c.get("min_uid", 1000) or 1000)
        self.listeners_on_nodes = c.get("listeners_on_nodes", False) is True
        # Services, not people: Slurm's and munge's accounts, and this one's own
        # (the collector's account: an admin's, which may run a site's services).
        self.ignore_users = _list(c["ignore_users"]) if isinstance(c.get("ignore_users"), (list, str)) \
            else list(SERVICE_ACCOUNTS)
        if c.get("watch_self", False) is not True:
            try:
                import pwd
                me = pwd.getpwuid(os.getuid()).pw_name
                if me not in self.ignore_users:
                    self.ignore_users.append(me)
            except (ImportError, KeyError):
                pass
        self.ignore_programs = _list(c.get("ignore_programs"))
        self.agents = _list(c.get("agents"))             # names to add to the probe's built-in agent list
        # The GPU watch (1.21): workstations asked as such, and GPU compute
        # processes placed by whether they are served to the network. On by
        # default -- it costs a host without a GPU nothing (nvidia-smi says so).
        self.workstations = [h for h in _list(c.get("workstations")) if HOST_RE.fullmatch(h)]
        self.watch_gpu = c.get("watch_gpu", True) is not False
        self.gpu_min_mem = int(c["gpu_min_mem"]) if isinstance(c.get("gpu_min_mem"), int) \
            and not isinstance(c.get("gpu_min_mem"), bool) and c["gpu_min_mem"] >= 0 else 256
        self.gpu_serve_hosts = [h for h in _list(c.get("gpu_serve_hosts")) if HOST_RE.fullmatch(h)]
        # Behaviour signals (1.22): heavy use of a login node (the probe), and
        # job-submission patterns (the local jobs table). On by default; both
        # no-op where there is nothing to see (no heavy process, no jobs table).
        self.watch_heavy = c.get("watch_heavy", True) is not False
        self.heavy_cpu = float(c["heavy_cpu"]) if isinstance(c.get("heavy_cpu"), (int, float)) \
            and not isinstance(c.get("heavy_cpu"), bool) and c["heavy_cpu"] > 0 else 2.0
        self.heavy_min = int(c["heavy_min_seconds"]) if isinstance(c.get("heavy_min_seconds"), int) \
            and not isinstance(c.get("heavy_min_seconds"), bool) and c["heavy_min_seconds"] >= 0 else 600
        self.watch_jobs = c.get("watch_jobs", True) is not False
        self.job_cfg = dict(JOB_DEFAULTS)
        for key, inner in _JOB_KEYS.items():
            v = c.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                self.job_cfg[inner] = v
        rr = c.get("root_report")
        self.root_report = rr if isinstance(rr, str) and PATH_OK.fullmatch(rr) else ""
        ports = c.get("ignore_ports")
        self.ignore_ports = list(ports) if isinstance(ports, list) else [ports] if ports not in (None, "") else []
        self.owners_in_alerts = c.get("owners_in_alerts", False) is True
        py = c.get("python") or list(PYTHONS)
        py = [py] if isinstance(py, str) else list(py)
        self.pythons = [p for p in py if isinstance(p, str) and PATH_OK.fullmatch(p)] or list(PYTHONS)
        self.trusted_hosts = _list(c.get("trusted_hosts"))
        self.campus, n1 = networks(c.get("campus_networks"), "campus network")
        self.cluster, n2 = networks(c.get("cluster_networks"), "cluster network")
        self.trust_notes = n1 + n2
        self.here_addresses: list[str] = []
        self._trust: list | None = None
        self.keep_days = max(7, int(c.get("keep_days", 365) or 365))
        self.new: list[dict] = []

    # -- when ---------------------------------------------------------------------

    def _swept_recently(self) -> bool:
        try:
            with self.get_db_connection() as conn:
                conn.executescript(SCHEMA)
                last = conn.execute("SELECT MAX(timestamp) FROM policy_watch_sweeps").fetchone()[0]
        except Exception:
            return False
        if not last:
            return False
        try:
            t = datetime.fromisoformat(str(last)[:19])
        except ValueError:
            return False
        return timedelta(0) <= datetime.now() - t < self.every - timedelta(seconds=30)

    def _note_sweep(self, when: str, asked: int, answered: int | None = None) -> None:
        try:
            with self.get_db_connection() as conn:
                conn.executescript(SCHEMA)
                if answered is None:
                    conn.execute("INSERT INTO policy_watch_sweeps (timestamp, asked) VALUES (?, ?)",
                                 (when, asked))
                    conn.execute("DELETE FROM policy_watch_sweeps WHERE timestamp < ?",
                                 ((datetime.now() - timedelta(days=7)).isoformat(),))
                else:
                    conn.execute("UPDATE policy_watch_sweeps SET answered = ? WHERE timestamp = ?",
                                 (answered, when))
                conn.commit()
        except Exception as e:
            logger.warning(f"policy_watch: could not note the sweep: {e}")

    # -- where ----------------------------------------------------------------------

    def _compute_nodes(self) -> list[str]:
        if self.nodes != "slurm":
            return [n for n in self.nodes if HOST_RE.fullmatch(n)]
        sinfo = find_tool("sinfo")
        if not sinfo:
            return []
        r = subprocess.run([sinfo, "-a", "-N", "-h", "-o", "%N %n %t"], capture_output=True, text=True,
                           errors="replace", timeout=60, env=tool_env())
        if r.returncode != 0:
            raise CollectionError(f"sinfo failed: {r.stderr.strip()[:200]}")
        return [host for _node, host in nodes_from_sinfo(r.stdout)]

    # -- asking -------------------------------------------------------------------------

    def trusted(self) -> list[tuple[str, str, int]]:
        """The trusted entries, resolved once a sweep: trusted_hosts, the
        login hosts, this host (by name, then by the addresses its own probe
        found, appended: an index the probe here was given stays the same)."""
        if self._trust is None:
            here = os.uname().nodename.split(".")[0]
            self._trust, notes = trusted_entries(self.trusted_hosts, here=here, login_hosts=self.login_hosts)
            self.trust_notes = [n for n in self.trust_notes if not n.startswith("trusted host")] + notes
        have = {(net, port) for _l, net, port in self._trust}
        here = os.uname().nodename.split(".")[0]
        for a in self.here_addresses:
            try:
                net = str(ipaddress.ip_network(a, strict=False))
            except ValueError:
                continue
            if (net, 0) not in have and not ipaddress.ip_network(net).network_address.is_loopback:
                self._trust.append((here, net, 0))
                have.add((net, 0))
        return self._trust

    def servers(self) -> list[str]:
        return [label for label, _n, _p in self.trusted()]

    def _args(self) -> list[str]:
        """The probe's arguments: min_uid, the trusted entries (network@port,
        0: below 1024), campus_networks, and who and what to leave out."""
        args = ["--min-uid", str(self.min_uid)]
        trusted = self.trusted()
        if trusted:
            args += ["--trusted", ",".join(f"{net}@{port}" for _l, net, port in trusted)]
        if self.campus:
            args += ["--campus", ",".join(self.campus)]
        if self.cluster:
            args += ["--cluster", ",".join(self.cluster)]
        safe_list = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
        for flag, vals in (("--ignore-users", self.ignore_users), ("--ignore-programs", self.ignore_programs),
                           ("--agents", self.agents)):
            vals = [v for v in vals if isinstance(v, str) and safe_list.fullmatch(v)]
            if vals:
                args += [flag, ",".join(vals)]
        if self.root_report:
            args += ["--root-report", self.root_report]
        if self.watch_gpu:
            args += ["--gpu"]
            if self.gpu_min_mem != 256:
                args += ["--gpu-min-mem", str(self.gpu_min_mem)]
        if self.watch_heavy:
            args += ["--heavy"]
            if self.heavy_cpu != 2.0:
                args += ["--heavy-cpu", repr(self.heavy_cpu)]
            if self.heavy_min != 600:
                args += ["--heavy-min", str(self.heavy_min)]
        return args

    def remote_command(self) -> str:
        """The first of `python` that exists, run with -I -S on the probe
        piped in (no PATH, no site-packages); a node with none says so."""
        tries = "; ".join(f'[ -f {p} ] && [ -x {p} ] && exec {p} -I -S - {" ".join(self._args())}'
                          for p in self.pythons)
        return f"echo {MARK}; {tries}; echo 'no python here: {' '.join(self.pythons)}' >&2; exit 127"

    def probe_here(self) -> tuple[dict | None, str]:
        try:
            r = subprocess.run([sys.executable, "-I", "-S", "-", *self._args()], input=probe_source(),
                               capture_output=True, text=True, errors="replace", timeout=self.timeout + 30)
        except subprocess.TimeoutExpired:
            return None, "timed out"
        except OSError as e:
            return None, str(e)[:120]
        d = parse_probe(r.stdout)
        return d, "" if d else ((r.stderr or "").strip().splitlines() or [f"exit {r.returncode}"])[-1][:160]

    def probe_over_ssh(self, ssh: str, host: str) -> tuple[dict | None, str]:
        argv = [ssh, "-T", "-a", "-x", "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes",
                "-o", "RequestTTY=no", "-o", f"ConnectTimeout={self.timeout}"]
        if self.ssh_user:
            argv += ["-l", str(self.ssh_user)]
        argv += ["--", host, self.remote_command()]
        try:
            r = subprocess.run(argv, input=probe_source(), capture_output=True, text=True, errors="replace",
                               timeout=self.timeout + 30)
        except subprocess.TimeoutExpired:
            return None, "timed out"
        except OSError as e:
            return None, str(e)[:120]
        d = parse_probe(r.stdout)
        if d:
            return d, ""
        err = (r.stderr or "").strip().splitlines()
        return None, (err[-1] if err else f"exit {r.returncode}")[:160]

    # -- the sweep ------------------------------------------------------------------------

    def collect(self) -> list[dict]:
        if self._swept_recently():
            self.note = f"swept less than {int(self.every.total_seconds() // 60)} minutes ago"
            return []
        here = os.uname().nodename.split(".")[0]
        targets: list[tuple[str, str]] = [(h, "login") for h in self.login_hosts if h != here]
        try:
            targets += [(h, "node") for h in self._compute_nodes() if h != here and h not in self.login_hosts]
        except (CollectionError, OSError, subprocess.SubprocessError) as e:
            self.note = f"compute nodes not asked: {e}"
        picked = {h for h, _r in targets} | {here}
        targets += [(h, "workstation") for h in self.workstations if h not in picked]   # asked as workstations (1.21)
        when = datetime.now().isoformat(timespec="seconds")
        self._note_sweep(when, 1 + len(targets))
        rows: list[dict] = []
        self._trust = None                                  # names resolved again each sweep
        d, err = self.probe_here()
        rows.append(self._host_row(when, (d or {}).get("host") or here, "login", d, err))
        self.here_addresses = [a for a in (d or {}).get("addresses") or [] if isinstance(a, str)][:64]
        self.trusted()                                      # appended now, before the nodes' threads read it
        if self.trust_notes:
            self.note = (self.note + "; " if self.note else "") + "; ".join(self.trust_notes)[:300]
        ssh = find_tool("ssh") if targets else None
        if targets and not ssh:
            self.note = "ssh not found: only this host was asked"
            targets = []
        if targets:
            pool = ThreadPoolExecutor(max_workers=min(self.parallel, len(targets)))
            futures = {pool.submit(self.probe_over_ssh, ssh, h): (h, role) for h, role in targets}
            done = set()
            try:
                for fut in as_completed(futures, timeout=self.max_seconds):
                    h, role = futures[fut]
                    done.add(fut)
                    try:
                        pd, perr = fut.result()
                    except Exception as e:                 # noqa: BLE001 -- one node's failure
                        pd, perr = None, str(e)[:120]
                    rows.append(self._host_row(when, h, role, pd, perr))
            except FuturesTimeout:
                for fut, (h, role) in futures.items():
                    if fut not in done:
                        rows.append(self._host_row(when, h, role, None,
                                                   f"no answer within the sweep's {int(self.max_seconds)} s"))
            finally:
                pool.shutdown(wait=False, cancel_futures=True)   # late answers aren't waited for
        # Job-behaviour findings from this site's own jobs table, attributed to
        # the login host where the collector runs (no-op where there is no
        # jobs table: a lab headnode without Slurm).
        if self.watch_jobs and rows:
            try:
                with self.get_db_connection() as conn:
                    conn.row_factory = sqlite3.Row
                    jf = job_findings(conn, rows[0]["host"], "login", datetime.now(), self.job_cfg)
                rows[0]["findings"].extend(jf)
            except Exception as e:                           # noqa: BLE001 -- behaviour is best-effort
                logger.warning(f"policy_watch: job behaviour not read: {e}")
        answered = sum(1 for r in rows if r["answered"])
        self._note_sweep(when, len(rows), answered)
        if answered < len(rows):
            self.note = (self.note + "; " if self.note else "") + \
                f"{answered} of {len(rows)} hosts answered (nomad watch status)"
        return rows

    def _host_row(self, when: str, host: str, role: str, d: dict | None, err: str) -> dict:
        row = {"timestamp": when, "host": host, "role": role, "answered": bool(d), "error": err or None,
               "processes": None, "others": None, "others_readable": None, "hidepid": None,
               "listener_programs": None, "findings": []}
        if d:
            row.update(processes=d.get("processes"), others=d.get("others"),
                       others_readable=d.get("others_readable"), hidepid=bool(d.get("hidepid")),
                       listener_programs=bool(d.get("programs_of_listeners")),
                       findings=findings_of(d, host, role, listeners_on_nodes=self.listeners_on_nodes,
                                            ignore_users=self.ignore_users,
                                            ignore_programs=self.ignore_programs,
                                            ignore_ports=self.ignore_ports, servers=self.servers(),
                                            gpu_serve_hosts=self.gpu_serve_hosts))
        return row

    # -- keeping it -------------------------------------------------------------------------

    def store(self, data: list[dict]) -> None:
        self.new = []
        with self.get_db_connection() as conn:
            ensure_schema(conn)
            for h in data:
                conn.execute(
                    "INSERT INTO policy_watch_hosts (timestamp, host, role, answered, processes, others, "
                    "others_readable, hidepid, listener_programs, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (h["timestamp"], h["host"], h["role"], int(h["answered"]), h["processes"], h["others"],
                     h["others_readable"], None if h["hidepid"] is None else int(h["hidepid"]),
                     None if h["listener_programs"] is None else int(h["listener_programs"]), h["error"]))
                names = entry_labels(self.trusted_hosts, here=os.uname().nodename.split(".")[0],
                                     login_hosts=self.login_hosts)
                ssh_ok = {}
                # ssh's own rows first: an autossh row is verified as its ssh is
                for f in sorted(h["findings"], key=lambda f: f["program"] == "autossh"):
                    # one finding whatever its reach or verdict, which are as last seen
                    old = conn.execute(
                        "SELECT id, verified, procs, note, severity, away FROM policy_findings WHERE host = ? AND "
                        "uid IS ? AND kind = ? AND program = ? AND detail = ? ORDER BY id DESC LIMIT 1",
                        (f["host"], f["uid"], f["kind"], f["program"], f["detail"])).fetchone()
                    f["was_info"] = bool(old) and old[4] == "info"
                    keep(f, old, names)
                    if f["kind"] == "ssh_reverse" and f["program"] == "ssh" and f.get("verified"):
                        ssh_ok[(f["uid"], f["detail"])] = f["note"]
                    elif f["kind"] == "ssh_reverse" and f["program"] == "autossh" and not f.get("verified") \
                            and (f["uid"], f["detail"]) in ssh_ok:
                        inner = ssh_ok[(f["uid"], f["detail"])]
                        f.update(verified=True, severity="info",
                                 note="as its ssh: " + (inner if len(inner) <= 388 else inner[:384] + "...)"))
                    verified = None if f.get("verified") is None else int(bool(f["verified"]))
                    procs = f.get("procs") if f.get("verified") else None
                    if old:
                        conn.execute("UPDATE policy_findings SET last_seen = ?, sweeps = sweeps + 1, "
                                     "username = COALESCE(?, username), reach = ?, verified = ?, note = ?, "
                                     "procs = ?, away = ?, severity = ? WHERE id = ?",
                                     (h["timestamp"], f["username"], f["reach"], verified, f.get("note"), procs,
                                      f.get("away") or 0, f["severity"], old[0]))
                    else:
                        cur = conn.execute(
                            "INSERT INTO policy_findings (host, role, username, uid, kind, program, detail, "
                            "reach, verified, note, procs, away, first_seen, last_seen, sweeps, severity) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                            (f["host"], f["role"], f["username"], f["uid"], f["kind"], f["program"],
                             f["detail"], f["reach"], verified, f.get("note"), procs, f.get("away") or 0,
                             h["timestamp"], h["timestamp"], f["severity"]))
                        f["id"] = cur.lastrowid
                        self.new.append(f)
                    # the alert's key: the first finding of this account and kind, on any host
                    f["group"] = conn.execute(
                        "SELECT MIN(id) FROM policy_findings WHERE uid IS ? AND kind = ?",
                        (f["uid"], f["kind"])).fetchone()[0]
            conn.execute("DELETE FROM policy_watch_hosts WHERE timestamp < ? AND id NOT IN "
                         "(SELECT MAX(id) FROM policy_watch_hosts WHERE answered = 1 GROUP BY host)",
                         ((datetime.now() - timedelta(days=7)).isoformat(),))
            conn.execute("DELETE FROM policy_findings WHERE last_seen < ?",
                         ((datetime.now() - timedelta(days=self.keep_days)).isoformat(),))
            silent = self._silent(conn, data)
            conn.commit()
        self._alert(data)
        self._alert_silent(silent)

    def _silent(self, conn, data: list[dict]) -> list[tuple]:
        """Hosts that answered before and not in the last 3 sweeps (a probe
        that can't finish there sees nothing: someone may be hiding)."""
        out = []
        for h in data:
            if h["answered"]:
                continue
            last = conn.execute("SELECT answered FROM policy_watch_hosts WHERE host = ? ORDER BY id DESC "
                                "LIMIT 3", (h["host"],)).fetchall()
            ever = conn.execute("SELECT 1 FROM policy_watch_hosts WHERE host = ? AND answered = 1 LIMIT 1",
                                (h["host"],)).fetchone()
            if ever and len(last) == 3 and not any(r[0] for r in last):
                out.append((h["host"], h["error"] or "no answer"))
        return out

    def _alert_silent(self, silent: list[tuple]) -> None:
        if not silent:
            return
        try:
            from nomad.alerts import send_alert
        except Exception:                                    # noqa: BLE001
            return
        for host, err in silent:
            try:
                send_alert("warning", "policy_watch",
                           f"{host}: the policy watch has had no answer for 3 sweeps ({err[:120]}): what runs there "
                           "is unseen. See nomad watch status.", host=host, subject=f"silent:{host}",
                           details={"episode_gap_minutes": max(60, int(self.every.total_seconds() // 60) * 3)})
            except Exception as e:                           # noqa: BLE001
                logger.warning(f"policy_watch: alert not sent: {e}")

    def _alert(self, data: list[dict]) -> None:
        """One alert per account and kind, on whatever hosts (renaming a
        program, or the same thing on many nodes, makes no new one),
        deduplicated by the dispatcher: raised when new, then as reminders;
        and, while that alert stands (the dispatcher held this one back), a
        new one when the group gains a finding (or one listed until now
        becomes a warning). It names no one unless owners_in_alerts; its key is the
        finding's number here, which says nothing of the account."""
        try:
            from nomad.alerts import send_alert
            from nomad.alerts.dispatcher import Held
        except Exception:                                    # noqa: BLE001 -- alerts are optional here
            return
        rank = {"info": 0, "warning": 1, "critical": 2}
        groups: dict = {}
        for h in data:
            for f in h["findings"]:
                if f["severity"] == "info" or f.get("group") is None:
                    continue
                groups.setdefault((f["group"], f["kind"]), []).append(f)
        gap = max(60, int(self.every.total_seconds() // 60) * 3)

        def what(fs):
            progs = []
            for f in fs:
                if f["kind"] == ESCALATION:
                    p = f["program"] + (f" ({f['note']})" if f.get("note") else "")
                else:
                    p = f["program"] + (f" port {f['detail']}" if f["detail"].isdigit() else
                                        f" {f['detail']}" if f["detail"] else "")
                    if f.get("reach") in REACH:
                        p += f", {REACH[f['reach']]}"
                    if f.get("note") and f["kind"] == "ssh_reverse":
                        p += f" ({f['note']})"
                if p.strip() and p.strip() not in progs:
                    progs.append(p.strip())
            return ", ".join(progs[:3]) + (f", +{len(progs) - 3} more" if len(progs) > 3 else "")

        for (group, kind), fs in groups.items():
            sev = max((f["severity"] for f in fs), key=lambda s: rank.get(s, 0))
            hosts = sorted({f["host"] for f in fs})
            more = len(hosts) - 1
            on = hosts[0] + (f" and {more} more host{'s' if more > 1 else ''}" if more else "")
            who = f", by {fs[0]['username']}" if self.owners_in_alerts and fs[0].get("username") else ""
            label = LABELS.get(kind, kind)
            # new to alerting: a new finding, or one listed (info) until this sweep
            fresh = [f for f in fs if f.get("id") or f.get("was_info")]
            try:
                shown = what(fs)
                r = send_alert(sev, "policy_watch", f"{on}: {label}{' (' + shown + ')' if shown else ''}{who}. "
                               "See nomad watch list.", host=hosts[0], subject=f"{kind}:f{group}",
                               details={"kind": kind, "role": fs[0]["role"], "episode_gap_minutes": gap})
                if fresh and isinstance(r, Held):            # the group's alert stands: say what is new
                    shown = what(fresh)
                    send_alert(sev, "policy_watch", f"{on}: new, {label}{' (' + shown + ')' if shown else ''}"
                               f"{who}. See nomad watch list.", host=hosts[0],
                               subject=f"{kind}:f{group}:new",
                               details={"kind": kind, "role": fs[0]["role"], "episode_gap_minutes": gap})
            except Exception as e:                           # noqa: BLE001
                logger.warning(f"policy_watch: alert not sent: {e}")
