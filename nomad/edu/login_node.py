# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A person's own heavy use of a login node, told to them (1.38.0).

The per_user collector watches a cluster's login node (role ``headnode``)
and records what its rules caught in ``per_user_alert``: a process using
CPU for a while, someone's processes together keeping cores busy, memory
held, a large transfer. Until now only the admins' insights brief read it;
the person who ran the work never heard of it, so they could not do it
another way.

This picks, from those rows, what is worth telling the person, and says it
without blame: what ran, where, when, for how long and how much; why it
matters on a machine everyone logs in to; and the better way (a job: the
Console's Submit a job, or an ``srun`` session). The Console's My activity
and Home show it, and so does ``nomad edu me``; staff see the same through
View as.

What is told (``[notices.login_node]``):

- one process holding ``min_cores`` (0.5) of a core or more for
  ``min_minutes`` (15) or more: a CPU rule whose threshold is at least that
  much (the collector's 10%-for-5-minutes rule never is: a ``pip install``
  reaches it);
- someone's processes together keeping cores busy, or holding memory (the
  collector's rules for the person as a whole: two cores, 32 GB, ten
  minutes);
- one process holding ``min_memory_gb`` (16) GB or more;
- a large transfer (50 MB/s for ten minutes by default) by a program that
  is not moving data in or out: ``skip_programs`` (scp, sftp-server,
  rsync, and sshd with newer OpenSSH's sshd-session and sshd-auth) are
  what a login node is for, and are never told, whatever kind (1.39.1).

One entry per process: of the rules one process tripped, the strongest; a
process inside a time when the person's processes together were caught is
part of that entry ("mostly python3"). The last ``days`` (30), newest first.
Times are the reader's local time (the collector writes UTC).

Only ``per_user_alert`` is read: the policy watch's own heavy-use flag (two
cores) stays an admin's signal, and the per_user rules already cover it
where per_user runs.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOGIN_ROLES = ("headnode", "login", "head", "login_node")
# sshd-session and sshd-auth: what newer OpenSSH (9.8+) runs for each login and transfer.
SKIP_PROGRAMS = ("scp", "sftp-server", "rsync", "sshd", "sshd-session", "sshd-auth")
KEYS = {"show", "days", "min_minutes", "min_cores", "min_memory_gb", "skip_programs"}
ONGOING_MINUTES = 15            # last seen this recently: still going at the last reading
FOLD_SLACK = 300                # seconds: a process this close to a together-window is part of it
SHOWN = 20                      # entries at most; the rest counted
GB = 1024 ** 3

# Strongest first: the one an entry is named after when a process tripped several.
KINDS = ("together_memory", "memory", "together_cpu", "cpu", "transfer")

_TEXT = {
    "cpu": (
        "A long computation on the login node",
        "One program kept the processor busy for a long time on the login node, the "
        "machine everyone logs in to. While it runs, others' logins, editing and job "
        "submissions slow down, and the cluster's compute nodes, which are there for "
        "this, sit unused by it.",
    ),
    "together_cpu": (
        "Many programs busy at once on the login node",
        "Your programs together kept several processor cores busy on the login node: "
        "a parallel build, a pool of workers or a loop starting many programs. Each may "
        "look small; together they slow the machine for everyone logged in.",
    ),
    "memory": (
        "A program holding a lot of memory on the login node",
        "One program held a large amount of memory on the login node. When that machine "
        "runs out of memory, logins and job submissions fail for everyone, and the "
        "program may be stopped.",
    ),
    "together_memory": (
        "Your programs holding a lot of memory on the login node",
        "Your programs together held a large amount of memory on the login node. When "
        "that machine runs out of memory, logins and job submissions fail for everyone.",
    ),
    "transfer": (
        "Large data movement through the login node",
        "A program read or wrote data quickly for a long time on the login node: a large "
        "copy, or results written to shared storage while an analysis ran. That slows the "
        "login node and the file server for everyone.",
    ),
}

_BUILD = ("make", "gmake", "cmake", "ninja", "gcc", "g++", "cc", "c++", "cc1", "cc1plus", "gfortran",
          "f951", "ld", "ld.bfd", "ld.gold", "nvcc", "cargo", "rustc", "javac", "mvn", "gradle")
_PACK = ("tar", "gzip", "gunzip", "pigz", "zip", "unzip", "xz", "bzip2", "pbzip2", "zstd", "7z", "7za")


@dataclass
class Settings:
    show: bool = True
    days: int = 30
    min_minutes: float = 15.0
    min_cores: float = 0.5
    min_memory_gb: float = 16.0
    skip_programs: tuple = SKIP_PROGRAMS


def _section(config) -> dict:
    notices = (config or {}).get("notices") if isinstance(config, dict) else None
    sec = notices.get("login_node") if isinstance(notices, dict) else None
    return sec if isinstance(sec, dict) else {}


def _number(sec, key, default, low=0.0, high=None):
    v = sec.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < low:
        return default
    return min(float(v), high) if high is not None else float(v)


def settings(config) -> Settings:
    """``[notices.login_node]``, with the defaults for anything not set or
    not usable (``nomad config check`` says which)."""
    sec = _section(config)
    skip = sec.get("skip_programs", SKIP_PROGRAMS)
    if not isinstance(skip, (list, tuple)) or not all(isinstance(x, str) for x in skip):
        skip = SKIP_PROGRAMS
    return Settings(
        show=sec.get("show", True) is not False,
        days=int(_number(sec, "days", 30, low=1, high=400)),
        min_minutes=_number(sec, "min_minutes", 15.0),
        min_cores=_number(sec, "min_cores", 0.5),
        min_memory_gb=_number(sec, "min_memory_gb", 16.0),
        skip_programs=tuple(x.strip().lower() for x in skip if x.strip()),
    )


def config_problems(config) -> list[str]:
    """What is wrong in ``[notices.login_node]``, for ``nomad config check``."""
    notices = (config or {}).get("notices") if isinstance(config, dict) else None
    if notices is None:
        return []
    if not isinstance(notices, dict):
        return ["[notices] is not a table"]
    sec = notices.get("login_node")
    if sec is None:
        return []
    if not isinstance(sec, dict):
        return ["[notices.login_node] is not a table"]
    out = []
    for key in sorted(set(sec) - KEYS):
        out.append(f"[notices.login_node] {key}: not a setting nomad knows ({', '.join(sorted(KEYS))})")
    if "show" in sec and not isinstance(sec["show"], bool):
        out.append(f"[notices.login_node] show = {sec['show']!r}: true or false, without quotes")
    for key, low in (("days", 1), ("min_minutes", 0), ("min_cores", 0), ("min_memory_gb", 0)):
        v = sec.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < low):
            out.append(f"[notices.login_node] {key} = {v!r}: a number, at least {low}")
    skip = sec.get("skip_programs")
    if skip is not None and (not isinstance(skip, list) or not all(isinstance(x, str) for x in skip)):
        out.append("[notices.login_node] skip_programs: a list of program names, e.g. [\"scp\", \"rsync\"]")
    return out


# -- what was caught ----------------------------------------------------------------------------

@dataclass
class Finding:
    kind: str                     # a key of KINDS
    title: str
    why: str
    better: list[str]             # lines: what to do instead, commands as they are typed
    site: str                     # the cluster (as the hub names it), else the host's short name
    host: str
    program: str                  # the process's name ("" for the person's processes together)
    command: str                  # its command line, cut to 300 characters (the person's own)
    started: str                  # local time, ISO, to the minute
    last_seen: str
    minutes: int                  # how long it held
    cores: float | None           # the most it used at once, in cores
    memory_gb: float | None
    ongoing: bool                 # still so at the last reading
    also: list[str] = field(default_factory=list)   # other programs of theirs in the same time
    rules: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _utc(text) -> datetime | None:
    """A time as the collector writes it ("2026-11-10 21:35:30", UTC)."""
    try:
        t = datetime.fromisoformat(str(text).strip().replace("T", " ")[:19])
    except (TypeError, ValueError):
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def _local(t: datetime) -> str:
    return t.astimezone().replace(tzinfo=None).isoformat(timespec="minutes")


def _short(host) -> str:
    return str(host or "").split(".")[0]


def _program(command) -> str:
    c = str(command or "").strip()
    if not c or c.startswith("("):
        return ""
    return c.split("/")[-1].split()[0]


def _max(a, b):
    nums = [x for x in (a, b) if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return max(nums) if nums else None


def _kind(row, s: Settings) -> str | None:
    """What a row is, for the person; None when it is not one to tell."""
    rtype = str(row["rule_type"] or "")
    try:
        threshold = float(row["threshold_value"] or 0)
    except (TypeError, ValueError):
        threshold = 0.0
    minutes = (row["sustained_for_seconds"] or 0) / 60
    if rtype == "cpu":
        if threshold < s.min_cores * 100 or minutes < s.min_minutes:
            return None
        return "cpu"
    if rtype == "user_cpu":
        return "together_cpu"
    if rtype == "user_memory":
        return "together_memory"
    if rtype == "memory":
        return "memory" if threshold >= s.min_memory_gb else None
    if rtype == "io":
        return "transfer"
    return None


def better_way(kind: str, program: str) -> list[str]:
    """What to do instead, for this kind of finding and program."""
    p = program.lower()
    if kind == "transfer":
        return ["Run large copies as a job, not on the login node:",
                "sbatch --time=4:00:00 --wrap \"cp -r SOURCE DESTINATION\"",
                "In an analysis, write to the node's local scratch and copy the results once, at the end."]
    if p in _BUILD:
        return ["Build inside a job, with the cores asked for:",
                "srun --pty -c 8 --mem=16G --time=2:00:00 bash",
                "then run the same make (e.g. make -j8) in that session."]
    if p in _PACK:
        return ["Pack or compress large folders as a job:",
                "sbatch --time=2:00:00 --wrap \"tar czf results.tar.gz results/\""]
    mem = "64G" if kind in ("memory", "together_memory") else "16G"
    cores = "8" if kind == "together_cpu" else "4"
    return ["Run it as a job: in the Console, Submit a job; or start a session on a compute node",
            f"srun --pty -c {cores} --mem={mem} --time=4:00:00 bash",
            "and run the same command there."]


def from_rows(rows, s: Settings | None = None, now: datetime | None = None) -> list[Finding]:
    """The findings to tell, from per_user_alert rows (mappings), newest
    first. Pure: what ``findings`` reads goes through here."""
    s = s or Settings()
    now = (now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.astimezone()
    skip = set(s.skip_programs)
    entries: list[dict] = []
    for row in rows:
        kind = _kind(row, s)
        if kind is None:
            continue
        program = _program(row["command"])
        if program.lower() in skip:
            continue
        end = _utc(row["last_seen"])
        if end is None:
            continue
        held = max(int(row["sustained_for_seconds"] or 0), 0)
        start = end - timedelta(seconds=held)
        site = ""
        try:
            site = str(row["source_site"] or "")
        except (IndexError, KeyError):
            site = ""
        entries.append({
            "kind": kind, "host": str(row["hostname"] or ""), "site": site or _short(row["hostname"]),
            "key": str(row["process_session_id"] or ""), "start": start, "end": end,
            "program": program, "command": str(row["cmdline"] or row["command"] or "")[:300],
            "cpu": row["peak_cpu_percent"], "mem": row["peak_memory_bytes"], "rule": str(row["rule_id"] or ""),
        })

    # One per process, and one per time the person's processes together were
    # caught: the rows of one process (or of the person as a whole) on one
    # host whose times overlap are one entry, named after the strongest kind.
    def group(e):
        return (e["host"], "together" if e["kind"].startswith("together") else e["key"])
    merged: list[dict] = []
    for e in sorted(entries, key=lambda x: (group(x), x["start"])):
        cur = merged[-1] if merged else None
        if cur is not None and group(cur) == group(e) \
                and e["start"] <= cur["end"] + timedelta(seconds=FOLD_SLACK):
            if KINDS.index(e["kind"]) < KINDS.index(cur["kind"]):
                cur["kind"] = e["kind"]
            if e["program"] and not cur["program"]:
                cur["program"], cur["command"] = e["program"], e["command"]
            cur["end"] = max(cur["end"], e["end"])
            cur["cpu"] = _max(cur["cpu"], e["cpu"])
            cur["mem"] = _max(cur["mem"], e["mem"])
            if e["rule"] not in cur["rules"]:
                cur["rules"].append(e["rule"])
            continue
        merged.append(dict(e, rules=[e["rule"]], also=[]))

    # A process inside a time the person's processes together were caught is part of that.
    together = [m for m in merged if m["kind"].startswith("together")]
    kept = []
    for m in merged:
        if not m["kind"].startswith("together"):
            home = next((t for t in together if t["host"] == m["host"]
                         and m["start"] >= t["start"] - timedelta(seconds=FOLD_SLACK)
                         and m["end"] <= t["end"] + timedelta(seconds=FOLD_SLACK)), None)
            if home is not None:
                if m["program"] and m["program"] != home["program"] and m["program"] not in home["also"]:
                    home["also"].append(m["program"])
                home["cpu"] = _max(home["cpu"], m["cpu"]) if home["kind"] == "together_cpu" else home["cpu"]
                continue
        kept.append(m)

    out = []
    for m in sorted(kept, key=lambda x: x["end"], reverse=True):
        title, why = _TEXT[m["kind"]]
        together_ = m["kind"].startswith("together")
        cpu = m["cpu"]
        mem = m["mem"]
        out.append(Finding(
            kind=m["kind"], title=title, why=why, better=better_way(m["kind"], m["program"]),
            site=m["site"], host=_short(m["host"]),
            program="" if together_ and not m["program"] else m["program"],
            command="" if together_ else m["command"],
            started=_local(m["start"]), last_seen=_local(m["end"]),
            minutes=round((m["end"] - m["start"]).total_seconds() / 60),
            cores=round(cpu / 100, 1) if isinstance(cpu, (int, float)) and cpu > 0 else None,
            memory_gb=round(mem / GB, 1) if isinstance(mem, (int, float)) and mem > 0 else None,
            ongoing=(now - m["end"]).total_seconds() <= ONGOING_MINUTES * 60,
            also=list(m.get("also") or []), rules=list(m["rules"]),
        ))
    return out


def findings(db_path, username: str, config=None, now: datetime | None = None) -> list[Finding]:
    """What to tell ``username`` about their use of login nodes, from the
    database at ``db_path`` (a site's own, or the hub's combined one). []
    when switched off, when there is no such table, or when it can't be
    read: this never stops a page."""
    s = settings(config)
    if not s.show or not username:
        return []
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.astimezone()
    since = (now - timedelta(days=s.days)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        p = Path(str(db_path))
        if not p.is_file():
            return []
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    except (sqlite3.Error, OSError, ValueError):
        return []
    try:
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(per_user_alert)")}
        if not cols:
            return []
        site = "source_site" if "source_site" in cols else "'' AS source_site"
        marks = ",".join("?" * len(LOGIN_ROLES))
        rows = conn.execute(
            f"SELECT hostname, role, rule_id, rule_type, threshold_value, sustained_for_seconds, command, "
            f"cmdline, peak_cpu_percent, peak_memory_bytes, process_session_id, last_seen, {site} "
            f"FROM per_user_alert WHERE username = ? AND last_seen >= ? AND role IN ({marks})",
            (username, since, *LOGIN_ROLES)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return from_rows(rows, s, now)


def summary(found: list[Finding], days: int) -> dict:
    """For the Console: the findings (at most SHOWN), how many more, the window."""
    return {"days": days, "findings": [f.as_dict() for f in found[:SHOWN]],
            "more": max(len(found) - SHOWN, 0)}


def _span(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def text(found: list[Finding], days: int) -> str:
    """For ``nomad edu me``: the findings as lines, or "" when there are none."""
    if not found:
        return ""
    lines = ["", f"On the login nodes (last {days} days)", ""]
    for f in found[:SHOWN]:
        what = f.program or "your programs"
        size = ", ".join(x for x in (f"{f.cores:g} cores" if f.cores else "",
                                     f"{f.memory_gb:g} GB" if f.memory_gb else "") if x)
        lines.append(f"  {f.title}")
        lines.append(f"    {what} on {f.site}, {f.started.replace('T', ' ')}, for {_span(f.minutes)}"
                     + (f" ({size})" if size else "") + (", still going at the last reading" if f.ongoing else ""))
        if f.also:
            lines.append(f"    also: {', '.join(f.also)}")
        if f.command and f.command != f.program:
            lines.append(f"    {f.command}")
        lines.append(f"    {f.why}")
        for b in f.better:
            lines.append(f"      {b}")
        lines.append("")
    if len(found) > SHOWN:
        lines.append(f"  ... and {len(found) - SHOWN} more")
    return "\n".join(lines).rstrip() + "\n"
