# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Which group a job belongs to -- and when that can't be said.

The group analyses (diversity by group, niche overlap, externalities) count
jobs per group. They used to join jobs to ``group_membership`` by username,
so a job was counted once for every group its owner belongs to: on spydur
7,946 jobs became 38,971 rows, groups sharing one busy member looked
identical (overlap 1.00), and correlations between groups were drawn from
the same jobs counted twice.

A job can be placed in one group when:

1. it records one itself -- a Slurm ``account`` column, or ``group_name``
   when that names more than one group and no single value covers most of
   the people (a catch-all Unix group such as ``people`` says nothing); or
2. membership is unambiguous: every person who ran jobs belongs to at most
   one group, leaving out umbrella groups that hold most users.

Otherwise the analyses are not run, and the reason says why.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from nomad.db import scope

# A group holding more than this share of the people is an umbrella
# ("people", "student"), not a research group.
UMBRELLA_SHARE = 0.8
# Groups must hold at least this share of the jobs: if most jobs fall under
# "none" or "ungrouped", comparing the few placed ones says little.
MIN_PLACED_SHARE = 0.8


@dataclass
class Attribution:
    available: bool
    method: str        # "account", "group_name", "membership", "none"
    group_expr: str    # SQL for a job's group (alias j, and gm when joined)
    join: str          # JOIN clause to add after "FROM jobs j", or ""
    reason: str        # how jobs were placed, or why they can't be
    people: int = 0            # people with jobs in the window
    ambiguous_people: int = 0  # of them, in more than one group

    def as_dict(self) -> dict:
        return {"available": self.available, "method": self.method,
                "reason": self.reason, "people": self.people,
                "ambiguous_people": self.ambiguous_people}


def _unavailable(reason: str, people: int = 0, ambiguous: int = 0) -> Attribution:
    return Attribution(False, "none", "NULL", "", reason, people, ambiguous)


def job_attribution(conn: sqlite3.Connection, since: str,
                    mode: str = "auto") -> Attribution:
    """How to place each job submitted since ``since`` in one group.

    ``mode="membership"`` forces the old join to group_membership, counting
    a job once per group of its owner's (for callers that want it knowingly).
    """
    jobs_cols = scope.table_columns(conn, "jobs")
    if not jobs_cols:
        return _unavailable("No jobs table.")
    jobs_of = {r[0]: r[1] for r in conn.execute(
        "SELECT user_name, COUNT(*) FROM jobs WHERE submit_time >= ? "
        "AND user_name IS NOT NULL GROUP BY user_name", (since,))}
    users = list(jobs_of)
    if not users:
        return _unavailable("No jobs in this window.")
    n_users = len(users)
    n_jobs = sum(jobs_of.values())

    if mode == "auto":
        for col in ("account", "group_name"):
            if col not in jobs_cols:
                continue
            rows = conn.execute(
                f"SELECT {col} AS g, COUNT(DISTINCT user_name) AS people, COUNT(*) AS jobs "
                f"FROM jobs WHERE submit_time >= ? AND {col} IS NOT NULL AND {col} != '' "
                f"GROUP BY {col}", (since,)).fetchall()
            # Real groups: two or more people each (a user-private group is a
            # person, not a group), no umbrella, and most of the jobs.
            shared = [r for r in rows if r["people"] >= 2]
            if (len(shared) >= 2
                    and max(r["people"] for r in rows) / n_users <= UMBRELLA_SHARE
                    and sum(r["jobs"] for r in shared) / n_jobs >= MIN_PLACED_SHARE):
                what = "Slurm account" if col == "account" else "group recorded with it"
                return Attribution(
                    True, col, f"COALESCE(NULLIF(j.{col}, ''), 'none')", "",
                    f"Each job counted once, under the {what}.", n_users, 0)

    if not scope.table_columns(conn, "group_membership"):
        return _unavailable(
            "Jobs don't record a group, and no group membership is collected.", n_users)
    members = conn.execute(
        "SELECT COUNT(DISTINCT username) FROM group_membership").fetchone()[0] or 0
    if not members:
        return _unavailable(
            "Jobs don't record a group, and no group membership is collected.", n_users)
    umbrella = {r[0] for r in conn.execute(
        "SELECT group_name, COUNT(DISTINCT username) AS n FROM group_membership "
        "GROUP BY group_name") if r[1] / members > UMBRELLA_SHARE}

    groups_all: dict[str, set[str]] = {}
    for u, g in conn.execute("SELECT username, group_name FROM group_membership"):
        if g not in umbrella:
            groups_all.setdefault(u, set()).add(g)
    members_of: dict[str, int] = {}
    for gs in groups_all.values():
        for g in gs:
            members_of[g] = members_of.get(g, 0) + 1
    # A one-member group is a person, not a group: it neither places a job
    # nor makes its member ambiguous. (Forcing membership keeps them all.)
    groups_of = groups_all if mode == "membership" else {
        u: {g for g in gs if members_of[g] >= 2} for u, gs in groups_all.items()}
    ambiguous = sum(1 for u in users if len(groups_of.get(u, ())) > 1)

    # The filtered membership as a TEMP table, so the analyses can join it.
    conn.execute("DROP TABLE IF EXISTS temp._nomad_membership")
    conn.execute("CREATE TEMP TABLE _nomad_membership (username TEXT, group_name TEXT)")
    conn.executemany("INSERT INTO temp._nomad_membership VALUES (?, ?)",
                     [(u, g) for u, gs in groups_of.items() for g in gs])
    join = "LEFT JOIN temp._nomad_membership gm ON j.user_name = gm.username"
    expr = "COALESCE(gm.group_name, 'ungrouped')"

    if mode == "membership":
        note = (f" {ambiguous} of {n_users} people belong to more than one group, "
                f"so their jobs are counted once per group." if ambiguous else "")
        return Attribution(True, "membership", expr, join,
                           "Jobs placed by their owner's group membership." + note,
                           n_users, ambiguous)
    if ambiguous:
        return _unavailable(
            f"Jobs don't record a group, and {ambiguous} of the {n_users} people who "
            f"ran jobs belong{'s' if ambiguous == 1 else ''} to more than one group, so "
            f"their jobs can't be placed in one. Group views need a group per job, "
            f"such as a Slurm account.",
            n_users, ambiguous)
    groups_used = {g for u in users for g in groups_of.get(u, ())
                   if members_of.get(g, 0) >= 2}
    if len(groups_used) < 2:
        return _unavailable(
            f"The {n_users} people who ran jobs here belong to "
            f"{len(groups_used) or 'no'} research group{'' if len(groups_used) == 1 else 's'} "
            f"of two or more people; group views need at least two.", n_users, 0)
    placed = sum(n for u, n in jobs_of.items()
                 if any(members_of.get(g, 0) >= 2 for g in groups_of.get(u, ())))
    if placed / n_jobs < MIN_PLACED_SHARE:
        return _unavailable(
            f"Only {placed / n_jobs:.0%} of the jobs were run by people in a research "
            f"group; comparing groups would leave most of the work out.", n_users, 0)
    return Attribution(True, "membership", expr, join,
                       "Jobs placed by their owner's group; everyone who ran jobs "
                       "belongs to at most one group.", n_users, 0)
