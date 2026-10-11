"""One person's work: their jobs over a window, what is running and waiting
now, and how they are doing -- the same score and advice as `nomad edu me`
and the Console's My Activity, from the same code.

Only ever about the one person named; the caller decides who may ask
(the Console answers for whoever is signed in, and nobody else).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from nomad.facts import jobs as jobfacts
from nomad.facts._common import envelope, site_label as _site, window

MAX_ADVICE = 3




def _issue(i) -> dict:
    return {
        "kind": i.kind,
        "dimension": i.dimension,
        "severity": i.severity,
        # "--mem from 64G to 8G", say; a verdict's advice is its rationale
        "advice": i.directive_label or i.rationale,
        "why": i.rationale if i.directive_label else (i.suggestion_rationale or ""),
        "jobs_affected": i.affected_jobs,
        "of_jobs": i.total_applicable,
        "cluster": i.cluster,
        "partition": i.partition,
    }


def now_counts(conn: sqlite3.Connection, active: dict, now: datetime) -> dict:
    """Running and waiting, by site, from jobfacts.active(). A site that has
    stopped reporting keeps its line, marked stale, but is left out of the
    totals: its jobs may have ended long ago."""
    from nomad.facts.sites import queue_current
    by_site = {}
    for site, counts in sorted(active.items(), key=lambda kv: str(kv[0])):
        if not counts["running"] and not counts["pending"]:
            continue
        as_of, stale = queue_current(conn, site, now)
        by_site[_site(site)] = {**counts, "as_of": as_of, "stale": stale}
    current = [c for c in by_site.values() if not c["stale"]]
    return {"running": sum(c["running"] for c in current),
            "pending": sum(c["pending"] for c in current),
            "by_site": by_site,
            "stale_sites": sorted(k for k, c in by_site.items() if c["stale"])}


def score(db_path, username: str, days: int, config: dict | None) -> tuple[dict | None, list]:
    """(score, advice) from nomad.edu: the same as My Activity. (None, [])
    when nothing of theirs was measured."""
    from nomad.edu.insights import user_insights
    ui = user_insights(db_path=str(db_path), username=username, days=days, config=config)
    if ui is None:
        return None, []
    s = None
    if ui.overall_score is not None:
        s = {"overall": round(ui.overall_score, 1),
             "jobs_scored": ui.job_count,
             "trend": ui.overall_trajectory,
             "change": None if ui.overall_change is None else round(ui.overall_change, 1),
             "dimensions": {k: round(v, 1) for k, v in (ui.dimensions or {}).items()}}
    return s, [_issue(i) for i in ui.issues[:MAX_ADVICE]]


def person(conn: sqlite3.Connection, username: str, days: int = 30,
           now: datetime | None = None, config: dict | None = None, db_path=None) -> dict:
    """What `username` ran in the last ``days``, is running now, and how it
    went. With db_path (the same database), also their score and advice."""
    from nomad.edu.progress import memory_ceilings
    now = now or datetime.now()
    since, until, win = window(days, now)
    rows = jobfacts.finished_rows(conn, since, until, users=[username])
    ceilings = memory_ceilings(conn)
    out = envelope("me", now, win)
    out["person"] = {"username": username}
    out["jobs"] = jobfacts.summarize(rows, ceilings)
    if rows:
        by_site: dict = {}
        for r in rows:
            by_site.setdefault(r.get("site"), []).append(r)
        out["jobs_by_site"] = {_site(site): jobfacts.summarize(rs, ceilings)
                               for site, rs in sorted(by_site.items(), key=lambda kv: str(kv[0]))}
    out["now"] = now_counts(conn, jobfacts.active(conn, users=[username]), now)
    out["score"], out["advice"] = (score(db_path, username, win["days"], config)
                                   if db_path is not None else (None, []))
    out["notes"] = [
        "CPU and memory use are over the jobs nomad measured; the rest are counted, not scored.",
        "Running and waiting are as each site last reported; a site that stopped reporting is "
        "listed but left out of the totals.",
    ]
    return out
