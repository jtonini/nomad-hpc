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
  another (-D). One to the cluster's own names (this host, ``login_hosts``,
  ``inside_hosts``; or a private address), with no proxy, jump or host
  name of its own, from an account reaching nothing public from there,
  says so, as a hint: a user's own ssh config can map any name anywhere,
  so it lowers nothing;
- code_tunnel: `code tunnel` (VS Code's relay through Microsoft);
- miner: a miner by name, or with a stratum pool on its command line;
- relay: a proxy or relay (socat listening, 3proxy, v2ray, shadowsocks, ...);
- listening: a program of a person's listening on a port open to the
  network, on a login node (``listeners_on_nodes = true`` for compute
  nodes too, where MPI, Dask or Ray open ports of their own; there, a port
  in the kernel's ephemeral range is listed, not alerted). Which program
  listens is known only where the account is root.

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
    # inside_hosts = []            # more of the cluster's own hosts (for ssh -R from a job)
    # keep_days = 365

Tables: ``policy_findings`` (one row per finding: host, role, username,
uid, kind, program, detail, reach (inside or outside, for an ssh forward),
first_seen, last_seen, sweeps, severity: copied to the hub);
``policy_watch_hosts`` (each host at each sweep, kept 7 days: whether it
answered, and what the probe could see there) and ``policy_watch_sweeps``
(when each sweep started): this host's own state, not copied to the hub.
"""
from __future__ import annotations

import json
import logging
import os
import re
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
            "listening": "warning"}
LABELS = {"tunnel": "a tunnel program", "ssh_reverse": "an ssh reverse forward (-R)",
          "ssh_socks": "an ssh SOCKS proxy (-D)", "code_tunnel": "a VS Code tunnel",
          "miner": "a miner", "relay": "a relay or proxy",
          "listening": "a program listening on a port open to the network"}

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
    return v if (v == "" or ok.match(v)) else "?"


def findings_of(probe: dict, host: str, role: str, *, listeners_on_nodes: bool = False,
                ignore_users=(), ignore_programs=(), ignore_ports=()) -> list[dict]:
    """The probe's answer as findings (rows for policy_findings, without
    times). What a user controls never lowers a finding: only a listener on
    an ephemeral port on a compute node (with listeners_on_nodes) is listed
    rather than alerted. An ssh forward to one of the cluster's names says
    so, as a hint. An ignore list leaves a finding out only if it names every
    name its process goes by."""
    out = []
    users, programs, ports = set(ignore_users), set(ignore_programs), {str(p) for p in ignore_ports}
    user_ok = re.compile(r"^[\w.$@-]{1,64}$")

    def ignored(prog, aka):
        names = {n for n in (aka or []) if n} or ({prog} if prog else set())
        return bool(names) and names <= programs

    for f in probe.get("findings") or []:
        prog, detail = _clean(f.get("program")), _clean(f.get("detail"))
        aka = [_clean(n) for n in f.get("aka") or []]
        if f.get("user") in users or ignored(prog, aka) or (detail and detail in ports):
            continue
        kind = f.get("kind") if f.get("kind") in SEVERITY else "other"
        # a hint only (a user's own ssh config can map any name anywhere): it lowers nothing
        reach = {True: "cluster name", False: "outside"}.get(f.get("inside"), "")
        sev = SEVERITY.get(kind, "info")
        out.append({"host": host, "role": role, "username": _clean(f.get("user"), user_ok),
                    "uid": f.get("uid") if isinstance(f.get("uid"), int) else None,
                    "kind": kind, "program": prog, "detail": detail, "reach": reach, "severity": sev})
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
                        "reach": "ephemeral" if eph else "",
                        "severity": "info" if (eph and role == "node") else SEVERITY["listening"]})
    return out


class PolicyWatchCollector(BaseCollector):
    name = "policy_watch"
    description = "Tunnels, miners, relays and exposed services on login and compute nodes"
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
        self.login_hosts = [h for h in (c.get("login_hosts") or []) if isinstance(h, str) and HOST_RE.match(h)]
        self.min_uid = int(c.get("min_uid", 1000) or 1000)
        self.listeners_on_nodes = c.get("listeners_on_nodes", False) is True
        # Services, not people: Slurm's and munge's accounts, and this one's own
        # (the collector's account: an admin's, which may run a site's services).
        self.ignore_users = list(c["ignore_users"]) if isinstance(c.get("ignore_users"), list) \
            else list(SERVICE_ACCOUNTS)
        if c.get("watch_self", False) is not True:
            try:
                import pwd
                me = pwd.getpwuid(os.getuid()).pw_name
                if me not in self.ignore_users:
                    self.ignore_users.append(me)
            except (ImportError, KeyError):
                pass
        self.ignore_programs = list(c.get("ignore_programs") or [])
        self.ignore_ports = list(c.get("ignore_ports") or [])
        self.owners_in_alerts = c.get("owners_in_alerts", False) is True
        py = c.get("python") or list(PYTHONS)
        py = [py] if isinstance(py, str) else list(py)
        self.pythons = [p for p in py if isinstance(p, str) and PATH_OK.fullmatch(p)] or list(PYTHONS)
        self.inside_hosts = [h for h in (c.get("inside_hosts") or []) if isinstance(h, str) and HOST_RE.match(h)]
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
            return [n for n in self.nodes if HOST_RE.match(n)]
        sinfo = find_tool("sinfo")
        if not sinfo:
            return []
        r = subprocess.run([sinfo, "-a", "-N", "-h", "-o", "%N %n %t"], capture_output=True, text=True,
                           errors="replace", timeout=60, env=tool_env())
        if r.returncode != 0:
            raise CollectionError(f"sinfo failed: {r.stderr.strip()[:200]}")
        return [host for _node, host in nodes_from_sinfo(r.stdout)]

    # -- asking -------------------------------------------------------------------------

    def _args(self) -> list[str]:
        """The probe's arguments: min_uid, and the names that are the
        cluster's own (this host, the login hosts, inside_hosts), for the
        hint that an ssh forward goes back inside."""
        here = os.uname().nodename.split(".")[0]
        names = sorted({h.lower() for h in [here, *self.login_hosts, *self.inside_hosts] if HOST_RE.match(h)})
        args = ["--min-uid", str(self.min_uid)] + (["--inside", ",".join(names)] if names else [])
        safe_list = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
        for flag, vals in (("--ignore-users", self.ignore_users), ("--ignore-programs", self.ignore_programs)):
            vals = [v for v in vals if isinstance(v, str) and safe_list.match(v)]
            if vals:
                args += [flag, ",".join(vals)]
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
        when = datetime.now().isoformat(timespec="seconds")
        self._note_sweep(when, 1 + len(targets))
        rows: list[dict] = []
        d, err = self.probe_here()
        rows.append(self._host_row(when, (d or {}).get("host") or here, "login", d, err))
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
                                            ignore_ports=self.ignore_ports))
        return row

    # -- keeping it -------------------------------------------------------------------------

    def store(self, data: list[dict]) -> None:
        self.new = []
        with self.get_db_connection() as conn:
            conn.executescript(SCHEMA)
            for table, col in (("policy_findings", "reach"), ("policy_watch_hosts", "listener_programs")):
                if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {'TEXT' if col == 'reach' else 'INTEGER'}")
            for h in data:
                conn.execute(
                    "INSERT INTO policy_watch_hosts (timestamp, host, role, answered, processes, others, "
                    "others_readable, hidepid, listener_programs, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (h["timestamp"], h["host"], h["role"], int(h["answered"]), h["processes"], h["others"],
                     h["others_readable"], None if h["hidepid"] is None else int(h["hidepid"]),
                     None if h["listener_programs"] is None else int(h["listener_programs"]), h["error"]))
                for f in h["findings"]:
                    old = conn.execute(
                        "SELECT id FROM policy_findings WHERE host = ? AND uid IS ? AND kind = ? AND "
                        "program = ? AND detail = ? AND reach = ? ORDER BY id DESC LIMIT 1",
                        (f["host"], f["uid"], f["kind"], f["program"], f["detail"], f["reach"])).fetchone()
                    if old:
                        conn.execute("UPDATE policy_findings SET last_seen = ?, sweeps = sweeps + 1, "
                                     "username = COALESCE(?, username) WHERE id = ?",
                                     (h["timestamp"], f["username"], old[0]))
                    else:
                        cur = conn.execute(
                            "INSERT INTO policy_findings (host, role, username, uid, kind, program, detail, "
                            "reach, first_seen, last_seen, sweeps, severity) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                            (f["host"], f["role"], f["username"], f["uid"], f["kind"], f["program"],
                             f["detail"], f["reach"], h["timestamp"], h["timestamp"], f["severity"]))
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
        and a new alert in the same sweep when the group gains a finding it
        didn't have. It names no one unless owners_in_alerts; its key is the
        finding's number here, which says nothing of the account."""
        try:
            from nomad.alerts import send_alert
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
                p = f["program"] + (f" port {f['detail']}" if f["detail"].isdigit() else
                                    f" {f['detail']}" if f["detail"] else "")
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
            fresh = [f for f in fs if f.get("id")]
            try:
                shown = what(fs)
                send_alert(sev, "policy_watch", f"{on}: {label}{' (' + shown + ')' if shown else ''}{who}. "
                           "See nomad watch list.", host=hosts[0], subject=f"{kind}:f{group}",
                           details={"kind": kind, "role": fs[0]["role"], "episode_gap_minutes": gap})
                if fresh and group not in {f["id"] for f in fresh}:      # the group was there before
                    shown = what(fresh)
                    send_alert(sev, "policy_watch", f"{on}: new, {label}{' (' + shown + ')' if shown else ''}"
                               f"{who}. See nomad watch list.", host=hosts[0],
                               subject=f"{kind}:f{group}:new",
                               details={"kind": kind, "role": fs[0]["role"], "episode_gap_minutes": gap})
            except Exception as e:                           # noqa: BLE001
                logger.warning(f"policy_watch: alert not sent: {e}")
