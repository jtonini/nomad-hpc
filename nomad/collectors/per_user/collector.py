# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ per-user collector: who uses a shared host (a login node, an
interactive server) heavily, with what, and for how long.

It runs from cron (``nomad collect --once``, every few minutes), a fresh
process each time. Each run:

1. reads every process (psutil): CPU seconds, resident memory, and I/O
   counters where they are readable (other users' need root); and, where
   systemd puts each user's logins in a slice, each user's CPU counter;
2. turns counters into averages over the interval since the previous run,
   from the counters that run left in ``per_user_state`` -- a process seen
   for the first time gets its average since it started, so one that has
   run flat out for two days is caught on the first reading;
3. stores a sample only for a process above a floor (``floor_cpu_percent``,
   ``floor_memory_gb``, ``floor_io_mb_per_s``): idle shells and daemons
   leave no row;
4. advances each rule's "held since" (per process, and per user for the
   ``user_*`` rules) and, once a rule has held for its duration, writes an
   alert: one row per process (or per episode of a user's totals) and rule,
   whose ``last_seen``, ``occurrences`` and ``sustained_for_seconds`` grow
   while it goes on;
5. adds the interval to each user's daily totals on this host
   (``per_user_daily``, kept), and prunes raw samples older than
   ``sample_retention_days``.

The in-memory engine this replaces kept its windows in the collector
object, which under cron lived for one reading: CPU was 0 for every
process and no rule could fire.
"""
from __future__ import annotations

import json
import logging
import os
import pwd
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import psutil
except ImportError:                       # pragma: no cover
    psutil = None

from ..base import BaseCollector, registry
from . import state as state_mod
from .ancestry import (
    ProcessInfo,
    WhitelistConfig,
    match_whitelist,
    walk_ancestry,
)
from .privileged import (
    PermissionDenied,
    can_walk_fds_of_other_users,
    walk_fds,
)
from .rules import DEFAULT_RULES, GB, MB, Reading, advance, parse_rules
from .state import RUN_KEY, Held, StateRow, alert_key, make_session_id

logger = logging.getLogger(__name__)

COLLECTOR_VERSION = "2.0-cron"
CMDLINE_TRUNCATE = 512
# A rate over a shorter window says little (a process 0.05 s old that used
# 0.04 s is "80%"): it is not used for the floor or the rules.
MIN_WINDOW_SECONDS = 10.0
# Raw samples deleted per run when pruning, so a large backlog (arachne's
# 12.4M rows from May) goes over hours without holding the database long.
PRUNE_BATCH = 100_000
# An interval longer than this (the collector stopped for a while) is not
# added to the daily totals: it would all land on the day the runs resumed.
# Two hours: room for a site that runs the collector hourly.
MAX_DAILY_INTERVAL = 7200.0
_SLICE_RE = re.compile(r"/user\.slice/user-(\d+)\.slice")
_INTERPRETER_RE = re.compile(
    r"(python[\d.]*|perl[\d.]*|ruby[\d.]*|Rscript|bash|sh|zsh|dash|ksh|csh|tcsh|node|julia)")


@dataclass(frozen=True)
class PerUserConfig:
    """Internal config. Built from the toml dict via from_dict()."""
    enabled: bool = True
    role: str = "headnode"
    ancestry_depth: int = 8
    rules: tuple = DEFAULT_RULES
    whitelist: WhitelistConfig = field(
        default_factory=lambda: WhitelistConfig(
            parent_paths=("/usr/local/sw/", "/var/spool/cron/"),
            users=("slurm", "munge"),
            min_uid=1000,
        )
    )
    fd_walk_enabled: bool = False
    floor_cpu_percent: float = 10.0
    floor_memory_gb: float = 2.0
    floor_io_mb_per_s: float = 10.0
    sample_retention_days: int = 30
    user_slices: bool = True
    cgroup_root: str = "/sys/fs/cgroup"

    @classmethod
    def from_dict(cls, d):
        wl_dict = d.get("whitelist", {}) or {}
        whitelist = WhitelistConfig(
            parent_paths=tuple(wl_dict.get("parent_paths",
                ("/usr/local/sw/", "/var/spool/cron/"))),
            users=tuple(wl_dict.get("users", ("slurm", "munge"))),
            user_commands=tuple(
                tuple(uc) for uc in wl_dict.get("user_commands", [])
            ),
            min_uid=wl_dict.get("min_uid", 1000),
        )
        rules = DEFAULT_RULES
        if d.get("rules"):
            rules = parse_rules(d["rules"]) or DEFAULT_RULES
        return cls(
            enabled=d.get("enabled", True),
            role=d.get("role", "headnode"),
            ancestry_depth=d.get("ancestry_depth", 8),
            rules=rules,
            whitelist=whitelist,
            fd_walk_enabled=d.get("fd_walk_enabled", False),
            floor_cpu_percent=float(d.get("floor_cpu_percent", 10.0)),
            floor_memory_gb=float(d.get("floor_memory_gb", 2.0)),
            floor_io_mb_per_s=float(d.get("floor_io_mb_per_s", 10.0)),
            sample_retention_days=int(d.get("sample_retention_days", 30)),
            user_slices=bool(d.get("user_slices", True)),
            cgroup_root=str(d.get("cgroup_root", "/sys/fs/cgroup")),
        )


@dataclass
class ProcessSnapshot:
    """One process at one reading. Counters are cumulative since it started."""
    info: ProcessInfo
    cpu_seconds: float | None             # user + system
    memory_rss_bytes: int
    memory_vms_bytes: int
    num_threads: int
    num_fds: int | None
    started_at: float
    cmdline: str
    io_read_bytes: int | None = None      # read_chars: every read through a system call
    io_write_bytes: int | None = None     # (files on any filesystem, NFS too, pipes, sockets)
    # Seconds from boot to the process's start: unlike the wall-clock start,
    # it does not move when the clock is stepped, so it keys the process.
    since_boot: float | None = None
    slice_uid: int | None = None          # the user-<uid>.slice it runs in, if any


@dataclass
class Measured:
    """A process's averages over the interval since it was last read."""
    window_start: float | None = None
    window: float | None = None
    cpu_percent: float | None = None
    io_read_bps: float | None = None
    io_write_bps: float | None = None
    # What it used inside the interval since the previous run, for the
    # user's totals: None when not known (first sight of a process that
    # was already running then).
    cpu_used: float | None = None
    io_read_used: int | None = None
    io_write_used: int | None = None
    # First sight of a process already running at the previous reading.
    provisional: bool = False

    @property
    def io_bps(self) -> float | None:
        if self.io_read_bps is None or self.io_write_bps is None:
            return None
        return self.io_read_bps + self.io_write_bps

    def usable(self) -> Measured:
        """The rates, unless the window is too short to mean anything."""
        if self.window is not None and self.window >= MIN_WINDOW_SECONDS:
            return self
        return Measured(self.window_start, self.window, cpu_used=self.cpu_used,
                        io_read_used=self.io_read_used, io_write_used=self.io_write_used,
                        provisional=self.provisional)


def _delta(now, before):
    if now is None or before is None or now < before:
        return None
    return now - before


def measure(snap: ProcessSnapshot, prev: StateRow | None, now: float,
            last_run: float | None) -> Measured:
    if prev is not None and now > prev.seen_at:
        start = prev.seen_at
        base_cpu, base_r, base_w = prev.cpu_seconds, prev.io_read_bytes, prev.io_write_bytes
        counted = True                    # all of it used since it was last read
    elif snap.started_at and now > snap.started_at:
        start = snap.started_at
        base_cpu = base_r = base_w = 0
        counted = last_run is not None and snap.started_at >= last_run
    else:
        return Measured()
    window = now - start
    cpu = _delta(snap.cpu_seconds, base_cpu)
    rd = _delta(snap.io_read_bytes, base_r)
    wr = _delta(snap.io_write_bytes, base_w)
    return Measured(
        window_start=start, window=window,
        cpu_percent=None if cpu is None else cpu / window * 100.0,
        io_read_bps=None if rd is None else rd / window,
        io_write_bps=None if wr is None else wr / window,
        cpu_used=cpu if counted else None,
        io_read_used=rd if counted else None,
        io_write_used=wr if counted else None,
        provisional=prev is None and not counted,
    )


def read_user_slices(root: str = "/sys/fs/cgroup") -> dict[int, float]:
    """CPU seconds used so far by each user's systemd slice (user-<uid>.slice).

    Counts every process of the user's logins, also those that started and
    ended between two readings (a parallel make), which reading processes
    misses. {} where there are no per-user slices with CPU accounting.
    """
    base = Path(root)
    layouts = (
        (base / "user.slice", "cpu.stat", _usage_usec),               # cgroup v2
        (base / "cpu,cpuacct" / "user.slice", "cpuacct.usage", _usage_ns),   # v1
        (base / "cpuacct" / "user.slice", "cpuacct.usage", _usage_ns),
        (base / "unified" / "user.slice", "cpu.stat", _usage_usec),   # hybrid
    )
    for directory, name, parse in layouts:
        out = {}
        try:
            entries = list(directory.glob("user-*.slice"))
        except OSError:
            continue
        for entry in entries:
            m = re.fullmatch(r"user-(\d+)\.slice", entry.name)
            if not m:
                continue
            try:
                value = parse((entry / name).read_text())
            except (OSError, ValueError):
                continue
            if value is not None:
                out[int(m.group(1))] = value
        if out:
            return out
    return {}


def _usage_usec(text: str) -> float | None:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "usage_usec":
            return int(parts[1]) / 1e6
    return None


def _usage_ns(text: str) -> float | None:
    text = text.strip()
    return int(text) / 1e9 if text else None


@dataclass
class _User:
    """A user's processes at one reading."""
    uid: int
    username: str
    processes: int = 0
    cpu_used: float | None = None         # inside the interval, all processes
    cpu_used_rules: float | None = None   # the processes the rules look at
    cpu_used_excused: float = 0.0         # whitelisted ones
    rss: int = 0
    rss_rules: int = 0
    io_read_used: int | None = None
    io_write_used: int | None = None
    top_cpu: tuple | None = None          # (cpu_used or rate, snap, ancestry)
    top_memory: tuple | None = None

    def add(self, snap, m: Measured, excused: bool, ancestry) -> None:
        self.processes += 1
        self.rss += snap.memory_rss_bytes
        if m.cpu_used is not None:
            self.cpu_used = (self.cpu_used or 0.0) + m.cpu_used
        if m.io_read_used is not None:
            self.io_read_used = (self.io_read_used or 0) + m.io_read_used
        if m.io_write_used is not None:
            self.io_write_used = (self.io_write_used or 0) + m.io_write_used
        if excused:
            self.cpu_used_excused += m.cpu_used or 0.0
            return
        self.rss_rules += snap.memory_rss_bytes
        if m.cpu_used is not None:
            self.cpu_used_rules = (self.cpu_used_rules or 0.0) + m.cpu_used
        weight = m.cpu_used if m.cpu_used is not None else 0.0
        if self.top_cpu is None or weight > self.top_cpu[0]:
            self.top_cpu = (weight, snap, ancestry)
        if self.top_memory is None or snap.memory_rss_bytes > self.top_memory[0]:
            self.top_memory = (snap.memory_rss_bytes, snap, ancestry)


def _envelope(samples, alerts, fd_rows, daily=(), state_rows=None, prune_before=None,
              based_on=None):
    return [{
        "_kind": "per_user_envelope",
        "samples": samples,
        "alerts": alerts,
        "fd_rows": fd_rows,
        "daily": list(daily),
        "state": state_rows,
        "prune_before": prune_before,
        "based_on": based_on,             # the previous run this one's intervals start at
    }]


@registry.register
class PerUserCollector(BaseCollector):
    """Per-user process tracking on shared hosts (login nodes)."""

    name = "per_user"
    description = "Per-user CPU/memory/I/O on shared hosts, with rules for heavy use"
    default_interval = 300

    def __init__(self, config, db_path):
        super().__init__(config, db_path)
        self.per_user_config = PerUserConfig.from_dict(config)
        self._hostname = _detect_hostname()
        self._can_walk_fds = (
            self.per_user_config.fd_walk_enabled and can_walk_fds_of_other_users()
        )
        if self.per_user_config.fd_walk_enabled and not self._can_walk_fds:
            logger.warning(
                "per_user: fd_walk_enabled=True but lacking privilege; "
                "falling back to no fd walking."
            )

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    def collect(self):
        cfg = self.per_user_config
        if not cfg.enabled:
            return []
        snapshots = list(self.iter_processes())
        if psutil is None and not snapshots:
            # Without psutil there are no processes to read. This used to
            # return an empty envelope, logged as "1 records" every run:
            # arachne looked healthy from May to September collecting nothing.
            self.note = "psutil not installed: nothing collected"
            return []
        me = os.geteuid()
        if snapshots and me != 0 and all(s.info.uid == me for s in snapshots):
            self.note = ("only this account's processes are visible "
                         "(is /proc mounted with hidepid?)")
        slices = read_user_slices(cfg.cgroup_root) if cfg.user_slices else {}
        self._own = self.own_usage() if slices else None
        now = time.time()
        return self.assess(snapshots, slices, self._load_state(), now)

    def own_usage(self):
        """(pid, start, CPU seconds, slice uid) of this nomad process with the
        commands it ran (squeue, ssh...): in a cron session it runs in its
        account's user slice, and that CPU is nomad's, not the account's."""
        if psutil is None:
            return None
        try:
            me = psutil.Process()
            t = me.cpu_times()
            return (me.pid, me.create_time(),
                    t.user + t.system + t.children_user + t.children_system,
                    _slice_uid(me.pid))
        except Exception:                                 # pragma: no cover
            return None

    def _load_state(self) -> dict[str, StateRow]:
        try:
            with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                return state_mod.load(conn, self._hostname)
        except sqlite3.Error as e:
            logger.warning("per_user: could not read state (%s); starting afresh", e)
            return {}

    def assess(self, snapshots, slices, state, now):
        """Everything one reading yields, as an envelope for store()."""
        cfg = self.per_user_config
        host, role = self._hostname, cfg.role
        run = state.get(RUN_KEY)
        last_run = run.seen_at if run is not None and run.seen_at < now else None
        interval = (now - last_run) if last_run is not None else None
        floor_mem = cfg.floor_memory_gb * GB
        floor_io = cfg.floor_io_mb_per_s * MB
        proc_rules = [r for r in cfg.rules if not r.per_user]
        user_rules = [r for r in cfg.rules if r.per_user]
        own_pid = os.getpid()

        by_pid = {s.info.pid: s.info for s in snapshots}
        new_state = [StateRow(RUN_KEY, "run", now)]
        samples, alerts, fd_rows = [], [], []
        users: dict[int, _User] = {}
        # CPU used inside each user's slice by processes of another account
        # (sudo, su): not that user's.
        foreign_in_slice: dict[int, float] = defaultdict(float)

        for snap in snapshots:
            info = snap.info
            if info.pid == own_pid:
                continue
            sid = make_session_id(host, info.pid, snap.since_boot
                                  if snap.since_boot is not None else snap.started_at)
            prev = state.get(sid)
            if prev is not None and prev.kind != "process":
                prev = None
            m = measure(snap, prev, now, last_run)
            rates = m.usable()
            ancestry = walk_ancestry(pid=info.pid, lookup=by_pid.get,
                                     max_depth=cfg.ancestry_depth)
            excused = match_whitelist(info, ancestry, cfg.whitelist)

            held, fired = {}, []
            if excused is None:
                reading = Reading(now, m.window_start, rates.cpu_percent,
                                  snap.memory_rss_bytes, rates.io_bps,
                                  provisional=m.provisional)
                held, fired = _advance_rules(proc_rules, reading, prev.rules if prev else {},
                                             rates.cpu_percent, snap.memory_rss_bytes)
            new_state.append(StateRow(sid, "process", now, snap.cpu_seconds,
                                      snap.io_read_bytes, snap.io_write_bytes, held))

            for rule, h in fired:
                alerts.append(_alert_row(
                    hostname=host, role=role, rule=rule, session_id=sid,
                    episode=f"{sid}-{int(h.since)}", held=h, now=now,
                    username=info.username, uid=info.uid, pid=info.pid,
                    command=info.command, cmdline=snap.cmdline, ancestry=ancestry.chain))

            above = ((rates.cpu_percent or 0.0) >= cfg.floor_cpu_percent
                     or snap.memory_rss_bytes >= floor_mem
                     or (rates.io_bps or 0.0) >= floor_io)
            if above or fired:
                samples.append(_sample_row(hostname=host, role=role, snap=snap,
                                           session_id=sid, ancestry=ancestry,
                                           whitelist_match=excused, m=rates, now=now))
                if self._can_walk_fds and role == "compute" and excused is None:
                    fd_rows.extend(self._fd_walk_one(snap, sid, now))

            if (snap.slice_uid is not None and snap.slice_uid != info.uid
                    and m.cpu_used is not None):
                foreign_in_slice[snap.slice_uid] += m.cpu_used
            if info.uid >= cfg.whitelist.min_uid:
                users.setdefault(info.uid, _User(info.uid, info.username)).add(
                    snap, m, excused is not None, ancestry)

        own = getattr(self, "_own", None)
        if own is not None and own[3] is not None:
            pid, created, cpu, slice_uid = own
            prev_self = state.get(f"self:{pid}")
            if prev_self is not None:                     # nomad collect running as a daemon
                own_used = _delta(cpu, prev_self.cpu_seconds)
            elif last_run is not None and created >= last_run:
                own_used = cpu                            # a cron run: all of it since
            else:
                own_used = None
            new_state.append(StateRow(f"self:{pid}", "self", now, cpu))
            if own_used:
                foreign_in_slice[slice_uid] += own_used

        daily = []
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        slice_uids = {uid for uid in slices if uid >= cfg.whitelist.min_uid}
        for uid in slice_uids:
            new_state.append(StateRow(f"slice:{uid}", "slice", now, slices[uid]))
        # Users with processes now, and users whose slice ran something
        # since the last reading even if nothing of theirs is alive now.
        for uid in sorted(set(users) | slice_uids):
            u = users.get(uid) or _User(uid, _username(uid))
            prev_u = state.get(f"user:{uid}")
            prev_slice = state.get(f"slice:{uid}")
            slice_own = None
            if uid in slices and prev_slice is not None and now > prev_slice.seen_at:
                used_in_slice = _delta(slices[uid], prev_slice.cpu_seconds)
                if used_in_slice is not None:
                    slice_own = max(used_in_slice - foreign_in_slice.get(uid, 0.0), 0.0)
            # Each source misses something (processes: those that came and
            # went between readings; the slice: processes outside it), so
            # the larger is the better lower bound.
            used = _max(slice_own, u.cpu_used)
            used_rules = _max(None if slice_own is None
                              else max(slice_own - u.cpu_used_excused, 0.0),
                              u.cpu_used_rules)
            cpu_pct = (used_rules / interval * 100.0
                       if interval and interval >= MIN_WINDOW_SECONDS and used_rules is not None
                       else None)
            if not u.processes and not used:
                continue                  # a slice left over from a logout
            reading = Reading(now, last_run, cpu_pct, u.rss_rules, None)
            held, fired = _advance_rules(user_rules, reading, prev_u.rules if prev_u else {},
                                         cpu_pct, u.rss_rules)
            for rule, h in fired:
                top = u.top_memory if rule.rule_type == "user_memory" else u.top_cpu
                if top is not None:
                    _, snap, ancestry = top
                    evidence = {"pid": snap.info.pid, "command": snap.info.command,
                                "cmdline": snap.cmdline, "ancestry": ancestry.chain}
                else:                     # nothing of theirs alive at the reading
                    evidence = {"pid": 0, "command": "(user slice)", "cmdline": "",
                                "ancestry": []}
                alerts.append(_alert_row(
                    hostname=host, role=role, rule=rule, session_id=f"user-{uid}",
                    episode=f"user-{uid}-{int(h.since)}", held=h, now=now,
                    username=u.username, uid=uid, **evidence))
            new_state.append(StateRow(f"user:{uid}", "user", now, rules=held))

            if interval is None or interval <= 0 or interval > MAX_DAILY_INTERVAL:
                continue
            used = used or 0.0
            if used < 1.0 and u.rss < floor_mem:
                continue                  # an idle login
            user_pct = used / interval * 100.0
            daily.append({
                "day": day, "hostname": host, "username": u.username, "uid": uid,
                "cpu_seconds": used,
                "busy_seconds": interval if user_pct >= cfg.floor_cpu_percent else 0.0,
                "peak_cpu_percent": user_pct,
                "peak_memory_bytes": u.rss,
                "io_read_bytes": u.io_read_used,
                "io_write_bytes": u.io_write_used,
                "cpu_source": ("cgroup" if slice_own is not None
                               and slice_own >= (u.cpu_used or 0.0) else "processes"),
                "updated_at": _utc_iso(now),
            })

        prune_before = None
        if cfg.sample_retention_days > 0:
            prune_before = _utc_iso(now - cfg.sample_retention_days * 86400)
        return _envelope(samples, alerts, fd_rows, daily, new_state, prune_before,
                         based_on=run.seen_at if run is not None else None)

    def count_records(self, data):
        """Samples, alerts and daily rows in the envelope, not the envelope itself."""
        env = data[0] if data else {}
        if isinstance(env, dict) and env.get("_kind") == "per_user_envelope":
            return (len(env.get("samples") or []) + len(env.get("alerts") or [])
                    + len(env.get("daily") or []))
        return len(data)

    def store(self, data):
        if not data:
            return
        env = data[0]
        if env.get("_kind") != "per_user_envelope":
            logger.warning("per_user.store: unexpected data shape, ignoring")
            return
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if (env.get("state") is not None
                    and _last_run(conn, self._hostname) != env.get("based_on")):
                # Another run stored after this one read the state (a run by
                # hand during cron's): its intervals overlap this one's, so
                # this one's totals, alerts and state are left out.
                self.note = "another per_user run stored meanwhile: this run's totals left out"
                env["alerts"], env["daily"], env["state"] = [], [], None
            self._persist_samples(conn, env.get("samples") or [])
            self._persist_alerts(conn, env.get("alerts") or [])
            if env.get("fd_rows"):
                self._persist_fd_samples(conn, env["fd_rows"])
            self._persist_daily(conn, env.get("daily") or [])
            if env.get("state") is not None:
                state_mod.save(conn, self._hostname, env["state"])
            if env.get("prune_before"):
                self._prune(conn, env["prune_before"])

    def iter_processes(self):
        if psutil is None:
            return
        attrs = ["pid", "ppid", "uids", "username", "name", "exe",
                 "memory_info", "num_threads", "num_fds", "create_time", "cmdline",
                 "cpu_times", "io_counters"]
        boot = psutil.boot_time()
        for proc in psutil.process_iter(attrs=attrs, ad_value=None):
            try:
                info = proc.info
                mem = info.get("memory_info")
                if mem is None:
                    continue
                ct = info.get("cpu_times")
                io = info.get("io_counters")
                uids = info.get("uids")
                args = info.get("cmdline") or []
                cmdline = " ".join(args)[:CMDLINE_TRUNCATE]
                created = info.get("create_time")
                yield ProcessSnapshot(
                    info=ProcessInfo(
                        pid=info["pid"],
                        ppid=info.get("ppid"),
                        uid=uids.real if uids is not None else -1,
                        username=info.get("username") or "",
                        command=info.get("name") or "",
                        exe_path=info.get("exe"),
                        script=script_of(args),
                    ),
                    cpu_seconds=(ct.user + ct.system) if ct is not None else None,
                    memory_rss_bytes=mem.rss,
                    memory_vms_bytes=mem.vms,
                    num_threads=info.get("num_threads") or 0,
                    num_fds=info.get("num_fds"),
                    started_at=info.get("create_time") or 0.0,
                    cmdline=cmdline,
                    io_read_bytes=getattr(io, "read_chars", None) if io is not None else None,
                    io_write_bytes=getattr(io, "write_chars", None) if io is not None else None,
                    since_boot=round(created - boot, 2) if created else None,
                    slice_uid=_slice_uid(info["pid"]),
                )
            except Exception as e:                       # pragma: no cover
                logger.debug("iter_processes: skipping %s: %s", proc, e)
                continue

    def _fd_walk_one(self, snap, session_id, now):
        try:
            walk = walk_fds(snap.info.pid)
        except PermissionDenied:
            return []
        except Exception as e:                            # pragma: no cover
            logger.debug("fd walk failed for pid=%s: %s", snap.info.pid, e)
            return []
        ts = _utc_iso(now)
        return [{
            "timestamp": ts,
            "hostname": self._hostname,
            "username": snap.info.username,
            "uid": snap.info.uid,
            "pid": snap.info.pid,
            "process_session_id": session_id,
            "fs_bucket": bucket,
            "fd_count": count,
            "representative_path": walk.representative_paths.get(bucket),
        } for bucket, count in walk.bucket_counts.items()]

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _persist_samples(self, conn, rows):
        if not rows:
            return
        conn.executemany("""
            INSERT INTO per_user_sample (
                timestamp, hostname, role, username, uid, pid, process_session_id,
                command, cmdline, exe_path,
                cpu_percent, cpu_window_seconds, memory_rss_bytes, memory_vms_bytes,
                io_read_bps, io_write_bps,
                num_threads, num_fds, started_at, elapsed_seconds,
                ancestry_chain, whitelist_match,
                collector_version, source
            ) VALUES (
                :timestamp, :hostname, :role, :username, :uid, :pid, :process_session_id,
                :command, :cmdline, :exe_path,
                :cpu_percent, :cpu_window_seconds, :memory_rss_bytes, :memory_vms_bytes,
                :io_read_bps, :io_write_bps,
                :num_threads, :num_fds, :started_at, :elapsed_seconds,
                :ancestry_chain, :whitelist_match,
                :collector_version, :source
            )
        """, rows)

    def _persist_alerts(self, conn, rows):
        if not rows:
            return
        # One row per process (or user episode) and rule. While the
        # condition goes on, each run updates it: how long, how high, and
        # when last seen. (MAX() of NULL is NULL in SQLite: COALESCE.)
        conn.executemany("""
            INSERT INTO per_user_alert (
                fired_at, hostname, role, username, uid, pid, process_session_id,
                rule_id, rule_type, severity, threshold_value, threshold_unit,
                sustained_for_seconds, command, cmdline, ancestry_chain,
                peak_cpu_percent, peak_memory_bytes, dedup_key, occurrences,
                last_seen, edu_template_id
            ) VALUES (
                :fired_at, :hostname, :role, :username, :uid, :pid, :process_session_id,
                :rule_id, :rule_type, :severity, :threshold_value, :threshold_unit,
                :sustained_for_seconds, :command, :cmdline, :ancestry_chain,
                :peak_cpu_percent, :peak_memory_bytes, :dedup_key, 1,
                :last_seen, :edu_template_id
            )
            ON CONFLICT(dedup_key) DO UPDATE SET
                occurrences = occurrences + 1,
                last_seen = excluded.last_seen,
                sustained_for_seconds = MAX(COALESCE(sustained_for_seconds, 0),
                                            COALESCE(excluded.sustained_for_seconds, 0)),
                peak_cpu_percent = MAX(COALESCE(peak_cpu_percent, 0),
                                       COALESCE(excluded.peak_cpu_percent, 0)),
                peak_memory_bytes = MAX(COALESCE(peak_memory_bytes, 0),
                                        COALESCE(excluded.peak_memory_bytes, 0))
        """, rows)

    def _persist_fd_samples(self, conn, rows):
        conn.executemany("""
            INSERT INTO per_user_fd_sample (
                timestamp, hostname, username, uid, pid, process_session_id,
                fs_bucket, fd_count, representative_path
            ) VALUES (
                :timestamp, :hostname, :username, :uid, :pid, :process_session_id,
                :fs_bucket, :fd_count, :representative_path
            )
        """, rows)

    def _persist_daily(self, conn, rows):
        if not rows:
            return
        conn.executemany("""
            INSERT INTO per_user_daily (
                day, hostname, username, uid, cpu_seconds, busy_seconds,
                peak_cpu_percent, peak_memory_bytes, io_read_bytes, io_write_bytes,
                cpu_source, updated_at
            ) VALUES (
                :day, :hostname, :username, :uid, :cpu_seconds, :busy_seconds,
                :peak_cpu_percent, :peak_memory_bytes, :io_read_bytes, :io_write_bytes,
                :cpu_source, :updated_at
            )
            ON CONFLICT(day, hostname, username) DO UPDATE SET
                uid = excluded.uid,
                cpu_seconds = cpu_seconds + excluded.cpu_seconds,
                busy_seconds = busy_seconds + excluded.busy_seconds,
                peak_cpu_percent = MAX(COALESCE(peak_cpu_percent, 0),
                                       COALESCE(excluded.peak_cpu_percent, 0)),
                peak_memory_bytes = MAX(COALESCE(peak_memory_bytes, 0),
                                        COALESCE(excluded.peak_memory_bytes, 0)),
                io_read_bytes = CASE WHEN excluded.io_read_bytes IS NULL THEN io_read_bytes
                                     ELSE COALESCE(io_read_bytes, 0) + excluded.io_read_bytes END,
                io_write_bytes = CASE WHEN excluded.io_write_bytes IS NULL THEN io_write_bytes
                                      ELSE COALESCE(io_write_bytes, 0) + excluded.io_write_bytes END,
                cpu_source = excluded.cpu_source,
                updated_at = excluded.updated_at
        """, rows)

    def _prune(self, conn, before: str) -> None:
        """Raw samples older than the retention, a batch per run. Alerts
        and daily totals are kept."""
        for table in ("per_user_sample", "per_user_fd_sample"):
            try:
                conn.execute(
                    f"DELETE FROM {table} WHERE id IN "
                    f"(SELECT id FROM {table} WHERE timestamp < ? LIMIT ?)",
                    (before, PRUNE_BATCH))
            except sqlite3.OperationalError as e:
                if "no such table" not in str(e):
                    raise
                logger.debug("per_user prune %s: %s", table, e)


def _max(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _sample_row(*, hostname, role, snap, session_id, ancestry, whitelist_match, m, now):
    return {
        "timestamp": _utc_iso(now),
        "hostname": hostname,
        "role": role,
        "username": snap.info.username,
        "uid": snap.info.uid,
        "pid": snap.info.pid,
        "process_session_id": session_id,
        "command": snap.info.command,
        "cmdline": snap.cmdline,
        "exe_path": snap.info.exe_path,
        "cpu_percent": m.cpu_percent,
        "cpu_window_seconds": m.window if m.cpu_percent is not None else None,
        "memory_rss_bytes": snap.memory_rss_bytes,
        "memory_vms_bytes": snap.memory_vms_bytes,
        "io_read_bps": m.io_read_bps,
        "io_write_bps": m.io_write_bps,
        "num_threads": snap.num_threads,
        "num_fds": snap.num_fds,
        "started_at": _utc_iso(snap.started_at) if snap.started_at else None,
        "elapsed_seconds": (now - snap.started_at) if snap.started_at else None,
        "ancestry_chain": json.dumps(ancestry.chain),
        "whitelist_match": (
            f"{whitelist_match.reason}:{whitelist_match.detail}"
            if whitelist_match else None
        ),
        "collector_version": COLLECTOR_VERSION,
        "source": "psutil",
    }


def _alert_row(*, hostname, role, rule, session_id, episode, held: Held, now,
               username, uid, pid, command, cmdline, ancestry):
    """One alert row per episode (since when the condition has held) and rule:
    a process busy for an hour, idle, then busy again has two."""
    ts = _utc_iso(now)
    return {
        "fired_at": ts,
        "hostname": hostname,
        "role": role,
        "username": username,
        "uid": uid,
        "pid": pid,
        "process_session_id": session_id,
        "rule_id": rule.rule_id,
        "rule_type": rule.rule_type,
        "severity": rule.severity,
        "threshold_value": rule.threshold_value,
        "threshold_unit": rule.threshold_unit,
        "sustained_for_seconds": int(now - held.since),
        "command": command,
        "cmdline": cmdline,
        "ancestry_chain": json.dumps(ancestry),
        "peak_cpu_percent": held.peak_cpu_percent,
        "peak_memory_bytes": held.peak_memory_bytes,
        "dedup_key": alert_key(hostname, episode, rule.rule_id),
        "last_seen": ts,
        "edu_template_id": rule.edu_template_id,
    }


def _advance_rules(rules, reading, prev_held: dict, cpu, memory):
    """Each rule's Held after this reading, and the rules that fire on it."""
    held, fired = {}, []
    for rule in rules:
        prev = prev_held.get(rule.rule_id)
        since, fires_now = advance(rule, reading, prev.since if prev else None)
        if since is None:
            continue
        if prev is not None and prev.since == since:
            h = Held(since, _max(prev.peak_cpu_percent, cpu),
                     _max(prev.peak_memory_bytes, memory))
        else:                             # a new episode: its own peaks
            h = Held(since, cpu, memory)
        held[rule.rule_id] = h
        if fires_now:
            fired.append((rule, h))
    return held, fired


def script_of(args) -> str | None:
    """The script an interpreter runs, from its arguments: `python3 -u
    /usr/local/sw/x/backup.py` -> /usr/local/sw/x/backup.py.

    It decides whitelisting, so it is strict: an option it does not know
    to take no value (it might be followed by its value, not the script),
    inline code (-c, -e), a module (-m) or a script on stdin (-, -s) give
    None.
    """
    if len(args) < 2:
        return None
    m = _INTERPRETER_RE.fullmatch(os.path.basename(args[0]))
    if not m:
        return None
    flags = _FLAGS.get(_family(m.group(1)), frozenset())
    for a in args[1:]:
        if a == "--":
            continue
        if a.startswith("-"):
            if a in flags:
                continue
            return None
        return os.path.normpath(a) if a.startswith("/") else a
    return None


def _family(interpreter: str) -> str:
    if interpreter.startswith("python"):
        return "python"
    if interpreter in ("bash", "sh", "zsh", "dash", "ksh", "csh", "tcsh"):
        return "shell"
    return interpreter


# Options that take no value, per interpreter; any other option ends the search.
_FLAGS = {
    "python": frozenset({"-b", "-bb", "-B", "-d", "-E", "-i", "-I", "-O", "-OO", "-P",
                         "-q", "-s", "-S", "-u", "-v", "-x"}),
    "shell": frozenset({"-e", "-x", "-u", "-v", "-l", "-n", "-p", "-r", "-f",
                        "--login", "--noprofile", "--norc", "--posix"}),
}


def _slice_uid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            m = _SLICE_RE.search(f.read())
    except OSError:
        return None
    return int(m.group(1)) if m else None


def _username(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError):
        return str(uid)


def _last_run(conn, hostname) -> float | None:
    try:
        row = conn.execute("SELECT seen_at FROM per_user_state "
                           "WHERE hostname = ? AND key = ?", (hostname, RUN_KEY)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _utc_iso(unix_ts):
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _detect_hostname():
    import socket
    return socket.gethostname()
