# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Is each site still collecting? Read on the hub after each sync (1.14.5).

The hub alerts when a site stops syncing, but a site can keep syncing a
database nothing writes to any more: its collection stopped (cron gone after
maintenance), one collector stopped, fails every run or finds nothing, a GPU
node went quiet, or DCGM stopped answering on a node and only nvidia-smi was
left. The pages then show old or partial data with nothing said anywhere.
These checks read the merged database and say so:

- **collection** -- a site where nothing at all was collected for
  ``[hub] alert_after_minutes`` (120);
- **collector** -- a collector that ran regularly and has not run, has failed
  every run, or (one that nearly always collects something) has collected
  nothing, for three of its usual intervals and at least alert_after_minutes;
- **gpu node** -- a GPU node with no samples while the site's other GPU nodes
  report (all of them quiet: one alert for the site);
- **dcgm** -- a GPU node whose DCGM samples stopped ``gpu_fallback_hours``
  (24) ago while nvidia-smi still answers: GPU utilization in scores comes
  only from DCGM.

Each site is judged as of the hub's last live pull from it
(``sync_sites.last_success``), not the clock: a site whose pull failed, or a
combined database the hub held back, is not taken for a site that stopped.
Collectors and nodes are judged on the last WINDOW_DAYS days; one silent
longer is taken as retired. Collectors disabled in a site's own settings (the
copy the hub keeps of them) are not watched. ``quiet_collectors`` and
``quiet_nodes`` in ``[hub]`` leave some out (``name``, ``site:name``, with
shell wildcards); ``collection_alerts = false`` turns them all off.
"""
from __future__ import annotations

import fnmatch
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

WINDOW_DAYS = 30        # collectors and GPU nodes are judged on this many days
REGULAR_RUNS = 6        # fewer runs (or samples) in the window: not watched
MIN_SPAN_HOURS = 24     # nor runs that all fall within a day (tries by hand)
RECENT_GAPS = 20        # the usual interval: from this many latest gaps...
INTERVALS = 3           # ...missed this many times over: stopped
SAME_RUN_SECONDS = 60   # runs this close together count as one


@dataclass
class Problem:
    key: str                 # the condition, for sending once
    site: str | None         # the site as stored (source_site)
    label: str               # the site as shown
    subject: str             # what at the site it is about
    severity: str
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class Settings:
    enabled: bool = True
    after_minutes: float = 120.0
    fallback_hours: float = 24.0
    quiet_collectors: tuple = ()
    quiet_nodes: tuple = ()


def settings(config: dict | None) -> Settings:
    hub = (config or {}).get("hub")
    hub = hub if isinstance(hub, dict) else {}

    def num(key, default):
        try:
            return float(hub.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def names(key):
        value = hub.get(key) or []
        return tuple(str(v) for v in (value if isinstance(value, (list, tuple)) else [value]))
    return Settings(enabled=hub.get("collection_alerts", True) is not False,
                    after_minutes=num("alert_after_minutes", 120),
                    fallback_hours=num("gpu_fallback_hours", 24),
                    quiet_collectors=names("quiet_collectors"),
                    quiet_nodes=names("quiet_nodes"))


def _quiet(patterns, site, name) -> bool:
    return any(fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(f"{site}:{name}", p)
               for p in patterns)


def _time(value) -> datetime | None:
    text = str(value or "").strip().replace(" ", "T")
    try:
        t = datetime.fromisoformat(text) if text else None
    except ValueError:
        try:
            t = datetime.fromisoformat(text[:19])
        except ValueError:
            return None
    return t.replace(tzinfo=None) if t is not None else None


def _shown(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M")


def _span(minutes: float) -> str:
    if minutes < 1:
        return "under a minute"
    hours = minutes / 60
    if hours < 1:
        return f"{minutes:.0f} min"
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} days"


def _severity(minutes: float) -> str:
    return "critical" if minutes >= 24 * 60 else "warning"


_PATHS = re.compile(r"(/(?:home|scratch|localscratch|work|Users|u|data|storage|projects|mnt/\w+)/)"
                    r"[^/\s'\":,;)]+")


def masked(message, names=()) -> str:
    """A collector's message with what could name a person taken out: the
    folder after /home, /scratch and the like, user@host, ~user, user=...,
    and every username the hub knows. Short."""
    text = str(message or "").strip()
    text = _PATHS.sub(r"\1…", text)
    text = re.sub(r"\b[\w.+-]+@([\w-]+(?:\.[\w-]+)*)", r"…@\1", text)
    text = re.sub(r"~[\w.-]+", "~…", text)
    text = re.sub(r"(?i)\b(user(?:name)?|owner|login|account)(\s*[=:]\s*|\s+)(['\"]?)[\w.-]+\3",
                  r"\1\2\3…\3", text)
    if names:
        text = re.sub(r"\b(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
                      + r")\b", "…", text)
    return text[:200] + ("…" if len(text) > 200 else "")


def _columns(conn, table) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _usernames(conn, tables) -> set:
    if "group_membership" not in tables:
        return set()
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT username FROM group_membership")
                if r[0] and len(str(r[0])) >= 3}
    except sqlite3.Error:
        return set()


def _pull_times(conn, tables) -> dict:
    """Each site's last live pull: what its data is as of."""
    if "sync_sites" not in tables:
        return {}
    try:
        return {name: _time(when) for name, when in
                conn.execute("SELECT name, last_success FROM sync_sites") if _time(when)}
    except sqlite3.Error:
        return {}


def collection_problems(conn: sqlite3.Connection, now: datetime | None = None,
                        config: dict | None = None, disabled: dict | None = None) -> list[Problem]:
    """What has stopped, from a hub's combined database (site by site) or a
    site's own. disabled: {site: collector names turned off in its settings}."""
    s = settings(config)
    now = now or datetime.now()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    pulls = _pull_times(conn, tables)
    names = _usernames(conn, tables)
    disabled = disabled or {}
    problems: list[Problem] = []
    stopped: set = set()
    gpu_limit: dict = {}
    if "collection_log" in tables:
        problems += _collectors(conn, now, s, pulls, names, disabled, stopped, gpu_limit)
    if "gpu_stats" in tables:
        flagged = {p.site for p in problems if p.details.get("collector") == "gpu"}
        problems += _gpus(conn, now, s, pulls, stopped, flagged, gpu_limit)
    return problems


def _site_list(conn, table, cols, pulls) -> list:
    if "source_site" not in cols:
        return [None]
    if pulls:
        return sorted(pulls)
    return [r[0] for r in conn.execute(f"SELECT DISTINCT source_site FROM {table}")]


def _as_of(site, now, pulls) -> datetime:
    t = pulls.get(site)
    return min(t, now) if t else now


def _when(minutes, as_of, now) -> str:
    """'3 h ago', or '3 h before the hub's last pull from it, at ...'."""
    if (now - as_of).total_seconds() > 30 * 60:
        return f"{_span(minutes)} before the hub's last pull from it, at {_shown(as_of)}"
    return f"{_span(minutes)} ago"


def _runs(rows):
    """(time, ok, message, records) rows in time order, runs within
    SAME_RUN_SECONDS of the last kept one counting as one (the latest kept)."""
    out = []
    for r in rows:
        if out and (r[0] - out[-1][0]).total_seconds() < SAME_RUN_SECONDS:
            out[-1] = r
        else:
            out.append(r)
    return out


def _quantile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0.0


def _collectors(conn, now, s, pulls, names, disabled, stopped, gpu_limit) -> list[Problem]:
    cols = _columns(conn, "collection_log")
    if not {"collector", "started_at"} <= cols:
        return []
    has_site = "source_site" in cols
    out = []
    after = s.after_minutes
    for site in _site_list(conn, "collection_log", cols, pulls):
        label = site if has_site else "this host"
        label = label if label is not None else "(no site)"
        as_of = _as_of(site, now, pulls)
        since = (as_of - timedelta(days=WINDOW_DAYS)).isoformat()
        where, args = ("source_site IS ?", [site]) if has_site else ("1", [])
        newest = _time(conn.execute(f"SELECT MAX(started_at) FROM collection_log WHERE {where}",
                                    args).fetchone()[0])
        if newest is None:
            continue
        age = (as_of - newest).total_seconds() / 60
        if age > after:
            stopped.add(site)
            out.append(Problem(
                f"collection:{site}", site, label, "collection", _severity(age),
                f"Site {label}: nothing collected since {_shown(newest)} ({_when(age, as_of, now)}). "
                "It still syncs, so the hub shows its data as of then without saying so; "
                "check nomad's cron or service there.",
                {"metric": "collection", "site": site, "last_run": newest.isoformat()}))
            continue
        ok = "success" if "success" in cols else "1"
        msg = "error_message" if "error_message" in cols else "NULL"
        recs = "records_collected" if "records_collected" in cols else "NULL"
        raw: dict[str, list] = {}
        for name, started, good, message, count in conn.execute(
                f"SELECT collector, started_at, {ok}, {msg}, {recs} FROM collection_log "
                f"WHERE {where} AND started_at >= ? ORDER BY started_at", args + [since]):
            t = _time(started)
            if t is not None and name and t <= as_of:
                raw.setdefault(name, []).append((t, bool(good), message, count))
        for name, rows in sorted(raw.items()):
            rows = _runs(rows)
            if (_quiet(s.quiet_collectors, site, name) or name in disabled.get(site, ())
                    or len(rows) < REGULAR_RUNS
                    or (rows[-1][0] - rows[0][0]).total_seconds() < MIN_SPAN_HOURS * 3600):
                continue
            times = [r[0] for r in rows]
            gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
            usual = _quantile(gaps[-RECENT_GAPS:], 0.9)
            week = [g for t, g in zip(times[1:], gaps) if t >= times[-1] - timedelta(days=7)]
            limit = max(after, INTERVALS * usual, 1.5 * max(week, default=0))
            if name == "gpu":
                gpu_limit[site] = limit
            p = _collector_problem(site, label, name, rows, usual, limit, as_of, now, names)
            if p:
                out.append(p)
    return out


def _collector_problem(site, label, name, rows, usual, limit, as_of, now, names):
    last = rows[-1]
    oks = [r for r in rows if r[1]]
    last_ok = oks[-1][0] if oks else None
    ran = (as_of - last[0]).total_seconds() / 60
    details = {"metric": "collector", "site": site, "collector": name,
               "last_run": last[0].isoformat(),
               "last_success": last_ok.isoformat() if last_ok else None,
               "usual_minutes": round(usual, 1)}
    key, subject = f"collector:{site}:{name}", f"collector {name}"
    if ran > limit:
        return Problem(
            key, site, label, subject, _severity(ran),
            f"Site {label}: the {name} collector has not run since {_shown(last[0])} "
            f"({_when(ran, as_of, now)}; it usually runs every {_span(usual)}), while the "
            "site's other collectors run. If it was turned off on purpose, list it under "
            "[hub] quiet_collectors.",
            details)
    if last_ok is None:
        return None              # never worked in the window: not a stop (`nomad collectors` says why)
    since_ok = (as_of - last_ok).total_seconds() / 60
    if since_ok > limit:
        text = masked(last[2], names)
        return Problem(
            key, site, label, subject, _severity(since_ok),
            f"Site {label}: the {name} collector has failed on every run since {_shown(last_ok)} "
            f"({_when(since_ok, as_of, now)}). Its last message: {text or '(none)'}",
            {**details, "error": text})
    # Succeeds, but a collector that nearly always collects something has
    # collected nothing (every workstation stopped answering, say).
    counted = [r for r in oks if r[3] is not None]
    before = [r for r in counted if (as_of - r[0]).total_seconds() / 60 > limit]
    recent = [r for r in counted if (as_of - r[0]).total_seconds() / 60 <= limit]
    if (len(before) >= REGULAR_RUNS and len(recent) >= 2
            and sum(1 for r in before if (r[3] or 0) > 0) >= 0.8 * len(before)
            and all((r[3] or 0) == 0 for r in recent)):
        with_data = [r[0] for r in counted if (r[3] or 0) > 0]
        t = with_data[-1]
        gone = (as_of - t).total_seconds() / 60
        if gone > limit:
            text = masked(last[2], names)
            return Problem(
                key, site, label, subject, _severity(gone),
                f"Site {label}: the {name} collector runs but has collected nothing since "
                f"{_shown(t)} ({_when(gone, as_of, now)})."
                + (f" Its last note: {text}" if text else ""),
                {**details, "metric": "collector_empty", "last_data": t.isoformat(), "error": text})
    return None


def _gpus(conn, now, s, pulls, stopped, flagged, gpu_limit) -> list[Problem]:
    cols = _columns(conn, "gpu_stats")
    if not {"timestamp", "node_name"} <= cols:
        return []
    has_site = "source_site" in cols
    dcgm = "data_source" in cols and "real_util_pct" in cols
    last_dcgm = ("MAX(CASE WHEN data_source = 'dcgm' AND real_util_pct IS NOT NULL "
                 "THEN timestamp END)") if dcgm else "NULL"
    out = []
    for site in _site_list(conn, "gpu_stats", cols, pulls):
        if site in stopped:
            continue
        label = site if has_site else "this host"
        label = label if label is not None else "(no site)"
        as_of = _as_of(site, now, pulls)
        since = (as_of - timedelta(days=WINDOW_DAYS)).isoformat()
        after = gpu_limit.get(site, s.after_minutes)
        where, args = ("source_site IS ? AND ", [site]) if has_site else ("", [])
        found = {}
        for node, first, last, last_d, seen in conn.execute(
                f"SELECT COALESCE(node_name, ''), MIN(timestamp), MAX(timestamp), {last_dcgm}, "
                f"COUNT(DISTINCT substr(timestamp, 1, 16)) FROM gpu_stats "
                f"WHERE {where}timestamp >= ? GROUP BY 1", args + [since]):
            f, t = _time(first), _time(last)
            if t is not None:
                found[node] = [f, min(t, as_of), _time(last_d), seen]
        # Rows without a node name (older data, or a host collecting its own
        # GPUs) belong to the one named node when there is just one.
        named = [n for n in found if n]
        if "" in found and len(named) == 1:
            u, n = found.pop(""), found[named[0]]
            n[0] = min((x for x in (u[0], n[0]) if x), default=None)
            n[1] = max(u[1], n[1])
            n[2] = max((x for x in (u[2], n[2]) if x), default=None)
            n[3] += u[3]
        nodes = {(n or "(no node name)"): v for n, v in found.items()
                 if v[3] >= REGULAR_RUNS and v[0] is not None
                 and (v[1] - v[0]).total_seconds() >= MIN_SPAN_HOURS * 3600}
        watched = {n: v for n, v in nodes.items() if not _quiet(s.quiet_nodes, site, n)}
        if not watched:
            continue
        newest = max(v[1] for v in nodes.values())
        quiet = sorted(n for n, v in watched.items() if (as_of - v[1]).total_seconds() / 60 > after)
        if quiet and len(quiet) == len(watched):
            if site in flagged:
                continue                   # the gpu collector's own alert says why
            age = (as_of - newest).total_seconds() / 60
            out.append(Problem(
                f"gpu:{site}", site, label, "gpu nodes", _severity(age),
                f"Site {label}: no GPU samples from any node since {_shown(newest)} "
                f"({_when(age, as_of, now)}).",
                {"metric": "gpu_samples", "site": site, "nodes": quiet,
                 "last_sample": newest.isoformat()}))
            continue
        for n in quiet:
            t = watched[n][1]
            age = (as_of - t).total_seconds() / 60
            out.append(Problem(
                f"gpu-node:{site}:{n}", site, label, n, _severity(age),
                f"Site {label}: GPU node {n} has sent no samples since {_shown(t)} "
                f"({_when(age, as_of, now)}), while the site's other GPU nodes do: down, "
                "unreachable, or its GPUs not answering. If it was retired or renamed, list it "
                "under [hub] quiet_nodes.",
                {"metric": "gpu_samples", "site": site, "node": n, "last_sample": t.isoformat()}))
        if not dcgm:
            continue
        for n, (_, t, d, _) in sorted(watched.items()):
            if d is None or n in quiet:
                continue
            gone = (as_of - d).total_seconds() / 3600
            if gone < s.fallback_hours:
                continue
            out.append(Problem(
                f"dcgm:{site}:{n}", site, label, n,
                "critical" if gone >= 7 * 24 else "warning",
                f"Site {label}: no DCGM samples from GPU node {n} since {_shown(d)} "
                f"({_when(gone * 60, as_of, now)}); only nvidia-smi answers there. Its GPU jobs get "
                "no utilization figure (scores say \"not measured\") until DCGM is back "
                "(nv-hostengine on the node).",
                {"metric": "dcgm", "site": site, "node": n, "last_dcgm": d.isoformat()}))
    return out


def disabled_collectors(cache_dir) -> dict:
    """{site: collectors turned off in its own settings}, from the copies of
    the sites' nomad.toml the hub's sync keeps (``sync_cache/<site>.toml``)."""
    out: dict = {}
    if cache_dir is None:
        return out
    try:
        from nomad.collectors.plan import plan
        from nomad.config import read_toml
    except Exception:
        return out
    for path in Path(cache_dir).glob("*.toml"):
        try:
            out[path.stem] = {p.spec.logged_as for p in plan(read_toml(path)) if not p.enabled}
        except Exception:
            continue
    return out


def collection_alerts(combined_path, cache_dir=None, failing=(), now=None,
                      config: dict | None = None) -> list[dict]:
    """Check the hub's combined database and raise what is due (source
    'collection'), sent once, again when worse, then every reminder_hours.
    failing: sites whose pull failed this run; what was raised about them is
    kept (not forgotten) until they are read again. Returns what was sent."""
    if config is None:
        from nomad.config import load_config
        config = load_config() or {}
    if not settings(config).enabled:
        return []
    now = now or datetime.now()
    combined_path = Path(combined_path)
    try:
        conn = sqlite3.connect(f"file:{combined_path}?mode=ro", uri=True, timeout=30)
        try:
            problems = collection_problems(conn, now, config, disabled_collectors(cache_dir))
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    from nomad.alerts.once import send_once
    stamp = (Path(cache_dir) if cache_dir is not None else combined_path.parent) / "collection_alerts.json"
    due = [{"key": p.key, "site": p.site, "host": p.label, "subject": p.subject,
            "severity": p.severity, "message": p.message, "details": p.details} for p in problems]
    return send_once(due, stamp, "collection", config, now, hold_sites=set(failing or ()))
