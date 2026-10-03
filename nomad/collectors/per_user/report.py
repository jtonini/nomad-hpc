# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""``nomad per-user``: heavy use of shared hosts, from what per_user stored.

On a site's database, its hosts; on a hub's combined database, every site's.
For each host: what was flagged (one line per process, or per episode of a
user's processes together), and who used the host most, from the daily
totals.

``--mask`` replaces user and command names with stand-ins, for output that
is shared; hostnames, times and numbers stay.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

GB = 1024 ** 3

_WHAT = {
    "cpu": "CPU ≥ {:g}%",
    "memory": "memory ≥ {:g} GB",
    "io": "I/O ≥ {:g} MB/s",
    "user_cpu": "all processes CPU ≥ {:g}%",
    "user_memory": "all processes memory ≥ {:g} GB",
}


class _Masker:
    def __init__(self, on: bool):
        self.on = on
        self.names: dict[tuple, str] = {}

    def __call__(self, kind: str, name) -> str:
        if not self.on:
            return str(name or "?")
        key = (kind, name)
        if key not in self.names:
            n = sum(1 for k in self.names if k[0] == kind) + 1
            self.names[key] = f"{kind}{n}"
        return self.names[key]


def _cols(conn, table) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _local(utc_text: str | None) -> str:
    if not utc_text:
        return "?"
    try:
        t = datetime.strptime(str(utc_text)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(utc_text)[:16]
    return t.replace(tzinfo=timezone.utc).astimezone().strftime("%m-%d %H:%M")


def _held_from(last_utc, sustained) -> str:
    """When the condition began: last seen, less how long it had held."""
    try:
        t = datetime.strptime(str(last_utc)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(last_utc)
    return (t - timedelta(seconds=int(sustained or 0))).strftime("%Y-%m-%d %H:%M:%S")


def _span(seconds) -> str:
    s = int(seconds or 0)
    if s < 60:
        return f"{s}s"
    h, m = divmod(s // 60, 60)
    if h >= 48:
        return f"{h // 24}d{h % 24:02d}h"
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _gb(b) -> str:
    return f"{(b or 0) / GB:.1f} GB"


def _seconds(utc_text) -> float:
    try:
        t = datetime.strptime(str(utc_text)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0.0
    return t.replace(tzinfo=timezone.utc).timestamp()


def _episodes(alert_rows) -> list[dict]:
    """One line per process (or user) and stretch of time: the rules a
    process broke over overlapping stretches together, a later stretch after
    it went quiet apart. Newest first."""
    by_session = defaultdict(list)
    for (sid, user, command, rtype, thr, severity, last, sustained, pcpu, pmem) in alert_rows:
        by_session[sid].append({
            "user": user, "command": command, "first": _held_from(last, sustained),
            "last": last, "cpu": pcpu or 0.0, "mem": pmem or 0,
            "what": [_WHAT.get(rtype, rtype + " {:g}").format(thr or 0)],
            "actionable": severity == "actionable"})
    out = []
    for parts in by_session.values():
        parts.sort(key=lambda e: e["first"])
        merged = [parts[0]]
        for e in parts[1:]:
            m = merged[-1]
            if e["first"] <= m["last"]:
                m["last"] = max(m["last"], e["last"])
                m["cpu"] = max(m["cpu"], e["cpu"])
                m["mem"] = max(m["mem"], e["mem"])
                m["what"] += [w for w in e["what"] if w not in m["what"]]
                m["actionable"] |= e["actionable"]
            else:
                merged.append(e)
        out.extend(merged)
    for e in out:
        e["span"] = _seconds(e["last"]) - _seconds(e["first"])
    out.sort(key=lambda e: e["last"], reverse=True)
    return out


def report(conn: sqlite3.Connection, days: int = 7, mask: bool = False,
           db_label: str = "") -> list[str]:
    alert_cols = _cols(conn, "per_user_alert")
    if not alert_cols:
        return [f"No per_user tables in {db_label or 'this database'}: per_user has "
                "never run here (nomad 1.7.15 or later, [collectors.per_user] enabled = true)."]
    hub = "source_site" in alert_cols
    site = "source_site" if hub else "''"
    name = _Masker(mask)
    since_utc = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    since_day = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    hosts: dict[tuple, dict] = defaultdict(lambda: {"alerts": [], "users": [], "io": None})
    for row in conn.execute(
            f"SELECT {site}, hostname, process_session_id, username, command, rule_type, "
            "threshold_value, severity, last_seen, sustained_for_seconds, "
            "peak_cpu_percent, peak_memory_bytes "
            "FROM per_user_alert WHERE last_seen >= ?", (since_utc,)):
        hosts[(row[0], row[1])]["alerts"].append(row[2:])

    if _cols(conn, "per_user_daily"):
        for row in conn.execute(
                f"SELECT {site}, hostname, username, SUM(cpu_seconds), SUM(busy_seconds), "
                "MAX(peak_cpu_percent), MAX(peak_memory_bytes), COUNT(DISTINCT day), "
                "SUM(io_read_bytes), SUM(io_write_bytes) "
                "FROM per_user_daily WHERE day >= ? GROUP BY 1, 2, 3 "
                "ORDER BY SUM(cpu_seconds) DESC", (since_day,)):
            hosts[(row[0], row[1])]["users"].append(row)

    sample_cols = _cols(conn, "per_user_sample")
    if "io_read_bps" in sample_cols:
        for s, h, measured, stored in conn.execute(
                f"SELECT {site}, hostname, COUNT(io_read_bps), COUNT(*) FROM per_user_sample "
                "WHERE timestamp >= ? AND collector_version = '2.0-cron' GROUP BY 1, 2",
                (since_utc,)):
            hosts[(s, h)]["io"] = (measured, stored)

    out = [f"Heavy use of shared hosts, last {days} days"
           + (f" ({db_label})" if db_label else "")]
    if not hosts:
        out.append("")
        out.append("Nothing stored in the window: no flags, no daily totals.")
        return out

    for (s, h), d in sorted(hosts.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        out.append("")
        out.append(f"{s} · {h}" if hub else str(h))

        episodes = _episodes(d["alerts"])
        if episodes:
            n_act = sum(1 for e in episodes if e["actionable"])
            out.append(f"  Flagged: {len(episodes)} ({n_act} actionable), newest first: "
                       "from - last seen, how long")
            for e in episodes[:15]:
                out.append(
                    f"    {_local(e['first'])} – {_local(e['last'])}  {_span(e['span']):>7}  "
                    f"{name('user', e['user']):<12} {name('cmd', e['command']):<16} "
                    f"peak {e['cpu']:.0f}% CPU, {_gb(e['mem'])}  "
                    f"{'!' if e['actionable'] else 'i'} {'; '.join(e['what'])}")
            if len(episodes) > 15:
                out.append(f"    ... and {len(episodes) - 15} more")
        else:
            out.append("  Flagged: none")

        if d["users"]:
            out.append("  Most CPU here (daily totals)")
            for (_s, _h, user, cpu, busy, pcpu, pmem, ndays, rd, wr) in d["users"][:10]:
                io = ""
                if rd is not None or wr is not None:
                    io = f", I/O {((rd or 0) + (wr or 0)) / 1e9:.1f} GB"
                out.append(
                    f"    {name('user', user):<12} {(cpu or 0) / 3600:7.1f} core-hours  "
                    f"busy {_span(busy):>7}  peak {(pcpu or 0) / 100:.1f} cores, "
                    f"{_gb(pmem)}  ({ndays} day{'s' if ndays != 1 else ''}){io}")
            if len(d["users"]) > 10:
                out.append(f"    ... and {len(d['users']) - 10} more")
        if d["io"] is not None:
            measured, stored = d["io"]
            if stored and measured < stored:
                out.append(f"  I/O measured for {measured:,} of {stored:,} stored processes: "
                           "other users' I/O needs the collector to run as root")
    return out
