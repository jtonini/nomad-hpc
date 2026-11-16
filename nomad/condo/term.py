# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Each lab's term report: what a lab used of the clusters and of research
storage over a term, for the PI who leads it (nomad 1.48.0).

Usage policies commit research computing to a report each term for each
lab's sponsor: the lab's core-hours, GPU-hours, storage, waits, and how much
of the cores and memory its jobs asked for they used. Condo owners already
get a monthly report (nomad.condo.figures); this one is for every other lab,
and for condo owners as well with ``term_condo_owners = true``.

- **Labs and members** are the lab check's (nomad.lab_check.labs): every lab
  by [console.labs] in nomad.toml that has a PI, with its members as the
  groups collector reads them now, and the PI. Someone who left during the
  term isn't counted; someone in two labs counts in each.
- **Terms** are [report.labs] ``terms``: the day each begins (MM-DD), each
  running to the next one's first day. A term's reports are made the day the
  next one begins (the timer), and cover the term that ended.
- **Core-hours and GPU-hours** are counted as the condo reports count them:
  each job's cores (or GPUs) times the hours it ran inside the term.
- **How jobs ended, and what they used** are for the jobs that ended in the
  term: CPU kept busy of the cores held, peak memory of the memory asked for
  (job_summary), as nomad.facts.jobs.summarize works them out.
- **Waits** are for the jobs that started in the term: from when each could
  start (Slurm's Eligible time, else its submission) to its start.
- **Storage** is each server path's reading of who holds its space, the
  latest before the term ended and the latest before it began; paths that
  hold copies of other paths are left out.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

from nomad.condo import discover
from nomad.condo.config import Settings
from nomad.condo.content import Foot, H, P, Table, Tiles, Title, _brand, minutes, n, pct, plural
from nomad.condo.figures import attach_measured, load_jobs, summary_row
from nomad.facts import jobs as jobfacts
from nomad.facts._common import median, quantile

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December")
LAB = "#2563eb"


# --- terms -----------------------------------------------------------------------

def _starts(terms, year: int) -> list[tuple[datetime, str | None]]:
    return [(datetime(y, m, d), name) for y in (year - 1, year, year + 1) for m, d, name in terms]


def term_of(terms, when: datetime) -> tuple[datetime, datetime, str | None]:
    """The term ``when`` falls in: (its first day, the next term's first day, its name)."""
    starts = _starts(terms, when.year)
    for i in range(len(starts) - 1):
        if starts[i][0] <= when < starts[i + 1][0]:
            return starts[i][0], starts[i + 1][0], starts[i][1]
    raise ValueError(f"no term holds {when:%Y-%m-%d}")  # pragma: no cover -- three years always do


def last_term(terms, now: datetime) -> tuple[datetime, datetime, str | None]:
    """The latest term that has ended by ``now``."""
    t0, _, _ = term_of(terms, now)
    return term_of(terms, t0 - timedelta(seconds=1))


def day(t: datetime) -> str:
    return f"{t.day} {_MONTHS[t.month - 1]} {t.year}"


def span(t0: datetime, t1: datetime) -> str:
    """'25 August 2026 to 9 January 2027': the last day is the one before t1."""
    return f"{day(t0)} to {day(t1 - timedelta(days=1))}"


def label(t0: datetime, t1: datetime, name: str | None) -> str:
    return f"{name} {t0.year}" if name else span(t0, t1)


def key(t0: datetime, t1: datetime) -> str:
    """How a term's files are named: 2026-08-25_2027-01-10."""
    return f"{t0:%Y-%m-%d}_{t1:%Y-%m-%d}"


# --- figures ---------------------------------------------------------------------

def _ended(j, t0, t1) -> bool:
    return j.ended and j.end is not None and t0 <= j.end < t1


def _use(jobs, t0, t1, ceilings) -> dict:
    """Core-hours, GPU-hours, people, how jobs ended, what they used, waits."""
    ran = [j for j in jobs if j.nodes and j.hours_in(t0, t1) > 0]
    ended = [j for j in jobs if _ended(j, t0, t1)]
    st = jobfacts.summarize([summary_row(j) for j in ended], ceilings) or {}
    waits = [j.wait_s / 60 for j in jobs if j.start is not None and t0 <= j.start < t1 and j.wait_s is not None]
    return {
        # to 4 places: a lab's few seconds of jobs are "under 1", not 0
        "core_hours": round(sum(j.cpus * j.hours_in(t0, t1) for j in ran), 4),
        "gpu_hours": round(sum(j.gpus * j.hours_in(t0, t1) for j in ran), 4),
        "people": len({j.user for j in ran}),
        "jobs_started": sum(1 for j in jobs if j.start is not None and t0 <= j.start < t1),
        "jobs_ended": len(ended),
        "outcomes": st.get("outcomes") or dict.fromkeys(jobfacts.OUTCOME_KEYS, 0),
        "failed": sum((st.get("outcomes") or {}).get(k, 0) for k in jobfacts.PROBLEMS),
        "failed_pct": st.get("failed_pct"),
        # what the failed share is of: the jobs that ran to an end, less node failures
        "judged": (st.get("ran_to_an_end") or 0) - (st.get("outcomes") or {}).get("node_fail", 0),
        "cpu_used_pct": (st.get("core_hours") or {}).get("used_pct"),
        "cpu_jobs_measured": (st.get("core_hours") or {}).get("jobs_measured", 0),
        "memory_used_pct": (st.get("memory") or {}).get("used_pct"),
        "memory_jobs_measured": (st.get("memory") or {}).get("jobs_measured", 0),
        "memory_allocated": (st.get("memory") or {}).get("allocated"),
        "wait_median_minutes": round(median(waits), 1) if waits else None,
        "wait_p90_minutes": round(quantile(waits, 0.9), 1) if waits else None,
        "waits": len(waits),
    }


def _records_begin(conn) -> datetime | None:
    """When nomad's job records begin (the first job submitted)."""
    from nomad.facts._common import columns, parse_time
    if "submit_time" not in columns(conn, "jobs"):
        return None
    first = conn.execute("SELECT MIN(submit_time) FROM jobs WHERE submit_time > '1990'").fetchone()[0]
    return parse_time(first)


def _reading(conn, where, before: datetime, inclusive: bool = False):
    """The latest reading of who holds a path's space before ``before``."""
    server, path, sql, args = where
    op = "<=" if inclusive else "<"
    return conn.execute(
        f"SELECT MAX(timestamp) FROM storage_owners WHERE server = ? AND path = ? AND kind = 'user' "
        f"AND timestamp {op} ?{sql}", (server, path, before.isoformat(), *args)).fetchone()[0]


def _held(conn, where, when, names: set) -> dict:
    """What each of ``names`` held on a path at one reading."""
    server, path, sql, args = where
    got: dict = defaultdict(int)
    for name, used in conn.execute(
            "SELECT name, SUM(used_bytes) FROM storage_owners WHERE server = ? AND path = ? AND "
            f"kind = 'user' AND timestamp = ?{sql} GROUP BY name", (server, path, when, *args)):
        if name in names:
            got[name] += int(used or 0)
    return got


def lab_storage(conn, names: set, t0: datetime, t1: datetime) -> dict:
    """Each server path the lab's people hold space on: the reading before
    the term ended and the one before it began. {measured, paths, by_person}."""
    from nomad.storage import views
    if not names or not views.available(conn) or not {"server", "path", "name", "timestamp"} <= views._cols(
            conn, "storage_owners"):
        return {"measured": False, "paths": [], "by_person": {}}
    scope = views._Scope(conn)
    by_person: dict = defaultdict(int)
    out = []
    read = False                     # any path read before the term ended
    for p in views.paths(conn):
        if p.get("copy_of"):
            continue
        server, path = p["server"], p["path"]
        sql, args = scope.of(server)
        where = (server, path, sql, args)
        end, begin = _reading(conn, where, t1), _reading(conn, where, t0, inclusive=True)
        if not end:
            continue
        read = True
        now = _held(conn, where, end, names)
        was = _held(conn, where, begin, names) if begin and begin != end else None
        used = sum(now.values())
        if not used and not (was and sum(was.values())):
            continue
        for name, b in now.items():
            by_person[name] += b
        out.append({"server": server, "path": path, "used_bytes": used,
                    "was_bytes": None if was is None else sum(was.values()),
                    "change_bytes": None if was is None else used - sum(was.values()),
                    "people": sum(1 for b in now.values() if b > 0), "read_at": end,
                    "compared_with": begin if was is not None else None,
                    "total_bytes": p.get("total_bytes")})
    if not read:
        return {"measured": False, "paths": [], "by_person": {}}
    return {"measured": True, "paths": sorted(out, key=lambda r: -r["used_bytes"]), "by_person": dict(by_person)}


def lab_figures(lab, people: set, jobs: list, t0, t1, now, *, ceilings, names, labels, storage, previous,
                generated, period, policy, name_members=True) -> dict:
    """One lab's term in figures."""
    hi = min(t1, now)
    mine = [j for j in jobs if j.user in people]
    by_site: dict = defaultdict(list)
    for j in mine:
        by_site[j.site].append(j)
    clusters = []
    for site, js in by_site.items():
        u = _use(js, t0, hi, ceilings)
        if not (u["core_hours"] or u["jobs_ended"] or u["jobs_started"]):
            continue
        cname = names.get(site, site or "?")
        clusters.append({"cluster": cname, "label": labels.get(cname, cname), **u})
    clusters.sort(key=lambda r: (-r["core_hours"], r["label"]))
    total = _use(mine, t0, hi, ceilings)
    total["clusters"] = len(clusters)
    each = []
    by_user: dict = defaultdict(list)
    for j in mine:
        by_user[j.user].append(j)
    for u in sorted(people):
        x = _use(by_user.get(u, []), t0, hi, ceilings)
        each.append({"user": u, "pi": u in lab.leaders, "core_hours": x["core_hours"], "gpu_hours": x["gpu_hours"],
                     "jobs_ended": x["jobs_ended"], "failed": x["failed"], "cpu_used_pct": x["cpu_used_pct"],
                     "memory_used_pct": x["memory_used_pct"], "wait_median_minutes": x["wait_median_minutes"],
                     "storage_bytes": storage["by_person"].get(u) if storage["measured"] else None})
    each.sort(key=lambda r: (-r["core_hours"], -(r["storage_bytes"] or 0), r["user"]))
    coverage = ["Members as the lab's groups list them now: someone who left during the term isn't counted, "
                "and someone in two labs counts in each.",
                "CPU used: the share of the cores jobs held that they kept busy. Memory used: the share of the "
                "memory jobs asked for that they used at their peak. Both for the jobs that ended in the term "
                "and were measured."]
    if total.get("memory_allocated") is False:
        coverage.append("On a cluster whose Slurm doesn't hold memory for jobs, memory used is against what "
                        "jobs asked for, which may not be what they were given.")
    if not storage["measured"]:
        coverage.append("Storage: not measured (no reading of who holds the space on the storage servers "
                        "before the term ended).")
    if hi < t1:
        coverage.append(f"The term so far: to {day(hi)}.")
    return {
        "lab": {"group": lab.group, "name": lab.name, "leaders": list(lab.leaders),
                "members": len(people), "condos": [list(c) for c in lab.condos]},
        "period": period, "generated": generated,
        "total": total, "clusters": clusters,
        "members": {"named": name_members, "each": each if name_members else [],
                    "ran": sum(1 for r in each if r["core_hours"] or r["jobs_ended"]),
                    "count": len(people)},
        "storage": {k: v for k, v in storage.items() if k != "by_person"},
        "previous": previous,
        "policy": {"use_below": policy.use_below},
        "coverage": coverage,
    }


def _local_name(settings, nomad_config: dict | None) -> str:
    """What a site's own database (no source_site) is called: as the condo
    reports call it when report.toml names it, else nomad.toml's cluster."""
    name = discover.local_name(settings, nomad_config)
    if name != discover.LOCAL or discover.LOCAL in (getattr(settings, "clusters", {}) or {}):
        return name
    cfg = nomad_config or {}
    for got in (((cfg.get("general") or {}).get("cluster_name")), ((cfg.get("cluster") or {}).get("name"))):
        if isinstance(got, str) and got.strip():
            return got.strip()
    return name


def build(conn: sqlite3.Connection, settings: Settings, t0: datetime, t1: datetime, *,
          name: str | None = None, nomad_config: dict | None = None, only: list[str] | None = None,
          condo_owners: bool | None = None, now: datetime | None = None) -> dict:
    """Every lab's figures for the term [t0, t1) and the summary's.
    ``only``: these labs (group or name; the summary is then partial).
    ``condo_owners``: report on labs that hold condos too (default: the
    setting, term_condo_owners)."""
    from nomad import lab_check
    from nomad.edu.progress import memory_ceilings
    now = now or datetime.now()
    if t1 <= t0:
        raise ValueError("the term ends before it begins")
    hi = min(t1, now)
    with_condos = settings.delivery.term_condo_owners if condo_owners is None else condo_owners
    labs = lab_check.labs(conn, nomad_config or {}, settings, now=now, alone=True)
    if only:
        wanted = set(only)
        unknown = wanted - {lab.group for lab in labs} - {lab.name for lab in labs}
        if unknown:
            raise ValueError(f"no lab {', '.join(sorted(unknown))} (labs: "
                             f"{', '.join(lab.group for lab in labs) or 'none'})")
        labs = [lab for lab in labs if lab.group in wanted or lab.name in wanted]
    local = _local_name(settings, nomad_config)
    sites = discover.node_sites(conn, since=min(t0, now - timedelta(days=60)))
    names = {s: (s if s is not None else local) for s in sites} | {None: local}
    labels = {c: settings.cluster(c).shown for c in set(names.values())}
    ceilings = memory_ceilings(conn)
    # The term before, for comparison: by the terms, else as long again.
    terms = settings.delivery.terms
    try:
        p0, p1, pname = term_of(terms, t0 - timedelta(seconds=1))
        if p1 != t0:
            raise ValueError
    except ValueError:
        p0, p1, pname = t0 - (t1 - t0), t0, None
    begins = _records_begin(conn)
    period = {"from": t0.isoformat(), "to": t1.isoformat(), "until": hi.isoformat(), "name": name,
              "label": label(t0, t1, name), "span": span(t0, t1), "so_far": hi < t1, "key": key(t0, t1)}
    out = {"period": period, "generated": now.isoformat(timespec="seconds"), "reports": [], "left_out": [],
           "partial": bool(only),
           "previous": {"from": p0.isoformat(), "to": p1.isoformat(), "label": label(p0, p1, pname),
                        "records_begin": begins.isoformat() if begins else None}}
    floor = settings.delivery.term_min_space
    out["min_space"] = floor
    reported: dict = defaultdict(int)          # person -> reports they count in
    for lab in labs:
        if lab.condos and not with_condos:
            out["left_out"].append({"lab": lab.group, "name": lab.name, "why": "condo"})
            continue
        people = set(lab.members) | set(lab.leaders)
        jobs = load_jobs(conn, None, t0, hi, now, users=people) if people else []
        attach_measured(conn, [j for j in jobs if j.ended])
        if begins is None or begins >= p1:
            previous = {"label": label(p0, p1, pname), "measured": False,
                        "why": "NØMAÐ has no job records from then"}
        else:
            before = load_jobs(conn, None, p0, p1, now, users=people) if people else []
            prev = _use(before, p0, p1, {})
            previous = {"label": label(p0, p1, pname), "measured": True, "partial": begins > p0,
                        "records_begin": begins.isoformat() if begins > p0 else None,
                        "core_hours": prev["core_hours"], "gpu_hours": prev["gpu_hours"],
                        "jobs_ended": prev["jobs_ended"], "people": prev["people"]}
        fig = lab_figures(lab, people, jobs, t0, t1, now, ceilings=ceilings, names=names, labels=labels,
                          storage=lab_storage(conn, people, t0, hi), previous=previous,
                          generated=out["generated"], period=period, policy=settings.policy,
                          name_members=settings.delivery.name_members)
        t = fig["total"]
        held = sum(p["used_bytes"] for p in fig["storage"]["paths"])
        little = held < floor if floor else not fig["storage"]["paths"]
        if not (t["core_hours"] or t["jobs_started"] or t["jobs_ended"]) and little and not only:
            # No job all term and little or no space: no report (a group
            # named for someone who doesn't compute is a lab by nomad's rules).
            out["left_out"].append({"lab": lab.group, "name": lab.name, "why": "quiet"})
            continue
        out["reports"].append(fig)
        for u in people:
            reported[u] += 1
    out["shared_people"] = sum(1 for k in reported.values() if k > 1)
    return out


# --- what the reports say --------------------------------------------------------

def _bytes(b) -> str:
    if b is None:
        return "-"
    x = float(b)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(x) < 1000 or unit == "PB":
            return f"{x:,.0f} {unit}" if unit == "B" or abs(x) >= 100 else f"{x:,.1f} {unit}"
        x /= 1000
    return f"{x:,.1f} PB"  # pragma: no cover


def hours(x) -> str:
    """Core- or GPU-hours: a little use is "under 1", not 0."""
    if x is None:
        return "-"
    return "under 1" if 0 < x < 1 else n(x)


def failed_line(u) -> str:
    """How a lab's jobs ended, in a line: '6 jobs ended; 4 failed of the 5
    that ran to an end (80%)' (the failed share leaves out cancelled jobs
    and node failures)."""
    ended, judged, failed = u["jobs_ended"], u.get("judged"), u["failed"]
    if not ended:
        return "no job ended"
    head = plural(ended, "job") + " ended"
    if judged is None or judged == ended:
        return f"{head}, {n(failed)} failed ({pct(u['failed_pct'])})"
    if not judged:
        return f"{head}; none ran to an end"
    return f"{head}; {n(failed)} failed of the {n(judged)} that ran to an end ({pct(u['failed_pct'])})"


def _ended_tile(u) -> str:
    """The Jobs ended tile's line, short: '2 of 4 failed'; '4 failed, 1
    cancelled' when some didn't run to an end (the table below has them all)."""
    ended = u["jobs_ended"]
    if not ended:
        return ""
    if u.get("judged") == ended:
        return f"{n(u['failed'])} of {n(ended)} failed"
    oc = u.get("outcomes") or {}
    bits = [f"{n(u['failed'])} failed"]
    if oc.get("cancelled"):
        bits.append(f"{n(oc['cancelled'])} cancelled")
    if oc.get("node_fail"):
        bits.append(plural(oc["node_fail"], "node failure"))
    return ", ".join(bits)


def _space_words(b) -> str:
    return _bytes(b).replace(".0 ", " ")


def _signed(b) -> str:
    if b is None:
        return "-"
    return ("+" if b > 0 else "") + _bytes(b) if b else "no change"


def _change(now, then) -> str:
    if then is None or now is None:
        return "-"
    if not then:
        return "new" if now else "-"
    d = 100.0 * (now - then) / then
    return "about the same" if abs(d) < 5 else (f"up {d:.0f}%" if d > 0 else f"down {-d:.0f}%")


def quiet_words(data: dict, labs: int = 2) -> str:
    """What the labs with no report held: 'hold no space', 'hold under 1 GB'
    ('holds' for one)."""
    floor = data.get("min_space") or 0
    verb = "holds" if labs == 1 else "hold"
    return f"{verb} under {_space_words(floor)}" if floor else f"{verb} no space"


def lab_doc(fig: dict, delivery, brand=None) -> list:
    lab, per, t, m, s = fig["lab"], fig["period"], fig["total"], fig["members"], fig["storage"]
    title = f"{per['label']}" + (" so far" if per["so_far"] else "")
    when = per["span"] if not per["so_far"] else f"{day(datetime.fromisoformat(per['from']))} to " \
                                                  f"{day(datetime.fromisoformat(per['until']))}"
    doc = [Title(delivery.institution, lab["name"], f"Term report: {title}" if per["name"] else
                 f"Report: {when}" + (" so far" if per["so_far"] else ""),
                 when if per["name"] else "Lab report", **_brand(brand))]
    leaders = lab["leaders"]
    who = (f"PI: {leaders[0]}." if len(leaders) == 1 else f"PIs: {', '.join(leaders[:-1])} and {leaders[-1]}.")
    if t["people"]:
        doc.append(P(f"Your lab's use of the research computing clusters and storage from {when}: "
                     f"{plural(t['people'], 'person', 'people')} of {n(m['count'])} ran jobs, on "
                     f"{plural(t['clusters'], 'cluster')}. {who}"))
    else:
        doc.append(P(f"No one in your lab ran a job on the research computing clusters from {when} "
                     f"({plural(m['count'], 'member')}, the PI included). {who}"))
    prev = fig.get("previous") or {}
    if t["people"] or t["jobs_ended"]:
        ch = (f"{_change(t['core_hours'], prev.get('core_hours'))} on {prev['label']}"
              if prev.get("measured") and not prev.get("partial") else "")
        doc.append(Tiles([
            ("Core-hours", hours(t["core_hours"]), ch, LAB),
            ("GPU-hours", hours(t["gpu_hours"]) if t["gpu_hours"] else "-", "", LAB),
            ("Jobs ended", n(t["jobs_ended"]), _ended_tile(t), LAB),
            ("Median wait", minutes(t["wait_median_minutes"]), f"{plural(t['waits'], 'job')} started"
             if t["waits"] else "", LAB)]))

        doc.append(H("By cluster"))
        rows = [[c["label"], hours(c["core_hours"]), hours(c["gpu_hours"]) if c["gpu_hours"] else "-", n(c["people"]),
                 n(c["jobs_ended"]), pct(c["failed_pct"]), pct(c["cpu_used_pct"]), pct(c["memory_used_pct"]),
                 minutes(c["wait_median_minutes"])] for c in fig["clusters"]]
        doc.append(Table(["Cluster", "Core-hours", "GPU-hours", "People", "Jobs ended", "Failed", "CPU used",
                          "Memory used", "Median wait"], rows, "lrrrrrrrr",
                         note="Failed: of the jobs that ran to an end (not cancelled, and not stopped by a "
                              "node failure), the share that failed, ran out of memory or hit their time limit. "
                              "Median wait: from when a job could start to its start."))
        oc = t["outcomes"]
        if t["jobs_ended"]:
            doc.append(Table(["Jobs ended", "Completed", "Failed", "Out of memory", "Timed out", "Cancelled",
                              "Node failure"],
                             [[n(t["jobs_ended"]), n(oc.get("completed")), n(oc.get("failed")),
                               n(oc.get("out_of_memory")), n(oc.get("timeout")), n(oc.get("cancelled")),
                               n(oc.get("node_fail"))]], "rrrrrrr"))
        cpu, mem = t["cpu_used_pct"], t["memory_used_pct"]
        if cpu is not None or mem is not None:
            bits = []
            if cpu is not None:
                bits.append(f"kept {pct(cpu)} of the cores they held busy")
            if mem is not None:
                bits.append(f"used {pct(mem)} of the memory they asked for, at their peak")
            line = "Your lab's jobs " + " and ".join(bits) + "."
            low = fig["policy"]["use_below"] * 100
            if (cpu is not None and cpu < low) or (mem is not None and mem < low):
                line += (" Asking for nearer what jobs use lets them start sooner, and leaves the cores and "
                         "memory for everyone else's. Each person's page in NØMAÐ shows which jobs and what "
                         "to ask for.")
            doc.append(P(line))

    doc.append(H("Members"))
    if m["named"] and m["each"]:
        used = [r for r in m["each"] if r["core_hours"] or r["jobs_ended"] or r["storage_bytes"]]
        quiet = len(m["each"]) - len(used)
        rows = [[r["user"] + (" (PI)" if r["pi"] else ""), hours(r["core_hours"]),
                 hours(r["gpu_hours"]) if r["gpu_hours"] else "-", n(r["jobs_ended"]), n(r["failed"]),
                 pct(r["cpu_used_pct"]), pct(r["memory_used_pct"]),
                 _bytes(r["storage_bytes"]) if r["storage_bytes"] else "-"] for r in used]
        note = "This term's figures for each person."
        if quiet:
            note += f" {plural(quiet, 'other member')} ran nothing and hold{'s' if quiet == 1 else ''} no space."
        if rows:
            doc.append(Table(["NetID", "Core-hours", "GPU-hours", "Jobs ended", "Failed", "CPU used",
                              "Memory used", "Storage"], rows, "lrrrrrrr", note=note))
        else:
            doc.append(P(f"No member ran anything or holds space ({plural(m['count'], 'member')})."))
    else:
        doc.append(P(f"{plural(m['count'], 'member')}, {n(m['ran'])} of whom ran jobs."))

    doc.append(H("Storage"))
    if not s["measured"]:
        doc.append(P("Not measured: NØMAÐ has no reading of who holds the space on the storage servers from "
                     "before the term ended.", muted=True))
    elif not s["paths"]:
        doc.append(P("Your lab's people hold no space on the storage servers NØMAÐ reads."))
    else:
        rows = [[f"{p['server']}:{p['path']}", _bytes(p["used_bytes"]),
                 _signed(p["change_bytes"]) if p["compared_with"] else "no reading from before",
                 n(p["people"])] for p in s["paths"]]
        doc.append(Table(["Where", "Your lab's space", "Change over the term", "People with space"], rows, "lrrr",
                         note="The latest reading before the term ended, against the one before it began. "
                              "Copies of other servers' paths aren't counted."))

    if prev:
        doc.append(H(f"Compared with {prev['label']}"))
        if not prev.get("measured"):
            doc.append(P(f"Not measured: {prev.get('why', 'no figures')}.", muted=True))
        else:
            rows = [["Core-hours", hours(t["core_hours"]), hours(prev["core_hours"]),
                     _change(t["core_hours"], prev["core_hours"])],
                    ["GPU-hours", hours(t["gpu_hours"]), hours(prev["gpu_hours"]),
                     _change(t["gpu_hours"], prev["gpu_hours"])],
                    ["Jobs ended", n(t["jobs_ended"]), n(prev["jobs_ended"]),
                     _change(t["jobs_ended"], prev["jobs_ended"])],
                    ["People who ran jobs", n(t["people"]), n(prev["people"]), _change(t["people"], prev["people"])]]
            note = ""
            if prev.get("partial"):
                note = (f"NØMAÐ's job records begin on {day(datetime.fromisoformat(prev['records_begin']))}: "
                        f"{prev['label']} is counted from then.")
            doc.append(Table(["", per["label"], prev["label"], "Change"], rows, "lrrr", note=note))

    doc.append(Foot(fig["coverage"] + ([delivery.contact] if delivery.contact else [])
                    + [f"Figures from NØMAÐ (nomad-hpc.com), {fig['generated'][:10]}."]))
    return doc


def summary_doc(data: dict, delivery, brand=None) -> list:
    per = data["period"]
    title = per["label"] + (" so far" if per["so_far"] else "")
    doc = [Title(delivery.institution, "Lab reports", f"Term summary: {title}" if per["name"] else
                 f"Summary: {title}", plural(len(data["reports"]), "lab"), **_brand(brand))]
    when = per["span"] if not per["so_far"] else (f"{day(datetime.fromisoformat(per['from']))} to "
                                                  f"{day(datetime.fromisoformat(per['until']))}, so far")
    doc.append(P(f"{when}. A row per lab that got a report, largest use first."))
    rows = []
    for f in sorted(data["reports"], key=lambda f: (-f["total"]["core_hours"], f["lab"]["name"])):
        t = f["total"]
        lab = f["lab"]
        space = sum(p["used_bytes"] for p in f["storage"]["paths"]) if f["storage"]["measured"] else None
        rows.append([lab["name"], ", ".join(lab["leaders"]), f"{n(t['people'])} of {n(f['members']['count'])}",
                     hours(t["core_hours"]), hours(t["gpu_hours"]) if t["gpu_hours"] else "-", n(t["jobs_ended"]),
                     pct(t["failed_pct"]), pct(t["cpu_used_pct"]), pct(t["memory_used_pct"]),
                     minutes(t["wait_median_minutes"]), _bytes(space) if space else "-"])
    if rows:
        shared = data.get("shared_people") or 0
        note = ("Failed: of the jobs that ran to an end (not cancelled, and not stopped by a node failure), the "
                "share that failed, ran out of memory or hit their time limit.")
        if shared:
            note += (f" {plural(shared, 'person is', 'people are')} in more than one of these labs and "
                     f"count{'s' if shared == 1 else ''} in each, so the rows add up to more than the clusters' use.")
        doc.append(Table(["Lab", "PI", "Ran jobs", "Core-hours", "GPU-hours", "Jobs ended", "Failed", "CPU used",
                          "Memory used", "Median wait", "Storage"], rows, "llrrrrrrrrr", note=note))
    else:
        doc.append(P("No lab to report on: none has a PI by [console.labs] in nomad.toml.", muted=True))
    condo = [x for x in data["left_out"] if x["why"] == "condo"]
    if condo:
        doc.append(P("Not here: " + ", ".join(x["name"] for x in condo) + (
            " (they hold condos and get a monthly report; term_condo_owners = true adds them)."), muted=True))
    quiet = [x for x in data["left_out"] if x["why"] == "quiet"]
    if quiet:
        doc.append(P(f"No report, as they ran no job all term and {quiet_words(data)}: "
                     + ", ".join(x["name"] for x in quiet) + ".", muted=True))
    doc.append(Foot(([delivery.contact] if delivery.contact else [])
                    + [f"Figures from NØMAÐ (nomad-hpc.com), {data['generated'][:10]}."]))
    return doc


# --- files and mail --------------------------------------------------------------

def default_out(settings: Settings, nomad_config: dict | None):
    from pathlib import Path
    if settings.delivery.term_out_dir:
        return settings.delivery.term_out_dir
    default = Path.home() / ".local" / "share" / "nomad"
    data = Path(((nomad_config or {}).get("general") or {}).get("data_dir") or default).expanduser()
    return data / "reports" / "labs"


def build_all(data: dict, settings: Settings, formats: list[str], brand=None) -> tuple[list, object]:
    from nomad.condo.run import Built
    labs = [Built(f["lab"]["group"], lab_doc(f, settings.delivery, brand), f, formats, brand)
            for f in data["reports"]]
    summary = Built("summary", summary_doc(data, settings.delivery, brand),
                    {k: v for k, v in data.items() if k != "reports"}, formats, brand)
    return labs, summary


def plan_mail(labs: list, summary, settings: Settings, data: dict) -> list[dict]:
    """Who each report goes to, or why it doesn't: [{key, subject, to, cc,
    review_of, skip}], as nomad.condo.run.plan_mail for the condos."""
    d = settings.delivery
    per = data["period"]
    out = []
    for b in labs:
        f = b.facts
        subject = f"{f['lab']['name']}: research computing, {per['label']}"
        to = [f"{x}@{d.mail_domain}" for x in f["lab"]["leaders"]] if d.mail_domain else []
        entry = {"key": b.key, "subject": subject, "to": [], "cc": [], "review_of": None, "skip": None}
        if d.reviewing:
            entry.update(to=list(d.review_to), review_of=", ".join(to) or "no one yet (no mail_domain)",
                         subject=f"[Review] {subject}")
        elif not to:
            entry["skip"] = "no address: set mail_domain in [report.labs]"
        else:
            entry["to"] = to
        out.append(entry)
    if summary is not None:
        to = d.summary_recipients()
        entry = {"key": "summary", "subject": f"Lab reports, {per['label']}", "to": to, "cc": [],
                 "review_of": None, "skip": None if to else "no summary_to or review_to"}
        if d.reviewing and list(to) != list(d.review_to):
            entry.update(to=list(d.review_to), review_of=", ".join(to),
                         subject=f"[Review] Lab reports, {per['label']}")
        out.append(entry)
    return out
