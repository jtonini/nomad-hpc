# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD edu hooks for per-user alerts.

Reads recent per_user_alert rows for a given user and renders them as
educational guidance. Designed to be called from `nomad edu me`'s existing
trajectory engine — it returns DimensionInsight-shaped objects that the
existing report formatter already knows how to display.

Templates
---------
Each rule has an `edu_template_id`. We map those ids to text plus a
concrete remediation command. Templates are deliberately discipline-neutral
and avoid blame language ('you broke X' becomes 'X happened; here's a
better path').

The ia3nk Singularity case from the validation report is the canonical
example — the educational fix is concrete and copy-pasteable:

    srun --pty -n1 -c4 --mem=16G --time=4:00:00 bash
    singularity shell ~/gromacs.sif
    gmx trjconv ...
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Template catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Template:
    template_id: str
    title: str
    explanation: str                      # short — 1-2 sentences
    remediation: str                      # multi-line, copy-pasteable
    severity: str                         # 'actionable' | 'informational'


TEMPLATES: dict[str, Template] = {
    "head_node_cpu_sustained": Template(
        template_id="head_node_cpu_sustained",
        title="Sustained CPU on the head node",
        explanation=(
            "A process you ran on the head node held substantial CPU for "
            "several minutes. Head nodes are shared infrastructure for editing "
            "and submitting jobs; sustained compute belongs on a compute node."
        ),
        remediation=(
            "# Run an interactive compute session instead:\n"
            "srun --pty -n1 -c4 --mem=16G --time=4:00:00 bash\n"
            "# Then run your command from inside that session."
        ),
        severity="actionable",
    ),
    "head_node_cpu_high": Template(
        template_id="head_node_cpu_high",
        title="High CPU burst on the head node",
        explanation=(
            "A short burst of high CPU from your processes was visible on "
            "the head node. Even brief bursts can interfere with other users' "
            "ssh, file editing, and job submission."
        ),
        remediation=(
            "# For one-off compute, use srun directly:\n"
            "srun -n1 -c4 --mem=8G --time=00:30:00 your_command"
        ),
        severity="actionable",
    ),
    "head_node_memory_moderate": Template(
        template_id="head_node_memory_moderate",
        title="Memory held on the head node",
        explanation=(
            "A long-running process from your session is holding several GB "
            "of memory on the head node. If this is your IDE's remote-development "
            "server (language server, indexer, file watcher), it's better placed "
            "on a compute node so it doesn't compete with other users."
        ),
        remediation=(
            "# For IDE remote-dev workflows, target a persistent compute session:\n"
            "salloc -n1 -c4 --mem=16G --time=8:00:00\n"
            "# Then point your IDE's remote target to that node."
        ),
        severity="informational",
    ),
    "head_node_memory_high": Template(
        template_id="head_node_memory_high",
        title="Significant memory on the head node",
        explanation=(
            "A process from your session held more than 16 GB of RAM on the "
            "head node. This is the kind of workload compute nodes exist for."
        ),
        remediation=(
            "# Run on a compute node with explicit memory request:\n"
            "srun --pty -n1 -c8 --mem=64G --time=4:00:00 bash"
        ),
        severity="actionable",
    ),
}


# ---------------------------------------------------------------------------
# Insight shape (matches nomad/edu/progress.py DimensionInsight)
# ---------------------------------------------------------------------------

@dataclass
class PerUserEduInsight:
    """One educational item derived from per_user_alert rows.

    Field names align with the existing DimensionInsight contract in the
    edu module so the existing CLI formatter renders these without changes.
    """
    dimension: str = "head_node_use"
    title: str = ""
    body: str = ""
    severity: str = "actionable"
    occurrences: int = 1
    last_seen: str = ""
    related_command: str | None = None    # the `command` column from the alert
    remediation: str = ""


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

def insights_for_user(
    db_path: str,
    username: str,
    lookback_days: int = 30,
) -> list[PerUserEduInsight]:
    """Build a list of insights for a given user from the per_user_alert table.

    Groups by template_id (so multiple firings of the same rule collapse into
    one insight with the total occurrences). Returns most-recent-first.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT edu_template_id,
                   command,
                   severity,
                   SUM(occurrences) AS total_occurrences,
                   MAX(last_seen) AS most_recent
            FROM per_user_alert
            WHERE username = ? AND fired_at >= ? AND edu_template_id IS NOT NULL
            GROUP BY edu_template_id, command
            ORDER BY most_recent DESC
            """,
            (username, cutoff),
        ).fetchall()

    insights: list[PerUserEduInsight] = []
    for r in rows:
        tid = r["edu_template_id"]
        tpl = TEMPLATES.get(tid)
        if tpl is None:
            continue
        insights.append(PerUserEduInsight(
            dimension="head_node_use",
            title=tpl.title,
            body=tpl.explanation,
            severity=tpl.severity,
            occurrences=int(r["total_occurrences"] or 1),
            last_seen=r["most_recent"] or "",
            related_command=r["command"],
            remediation=tpl.remediation,
        ))
    return insights


def render_insight_text(insight: PerUserEduInsight) -> str:
    """Render one insight as multi-line text for `nomad edu me` CLI output.

    Format follows the existing nomad edu output style: title + indented body,
    with a clear remediation block below.
    """
    sev_marker = "[!]" if insight.severity == "actionable" else "[i]"
    parts = [f"  {sev_marker} {insight.title}"]
    parts.append(f"      {insight.body}")
    if insight.related_command:
        parts.append(f"      Triggering command: {insight.related_command}  "
                     f"(seen {insight.occurrences}x, last {insight.last_seen})")
    if insight.remediation:
        parts.append("")
        for line in insight.remediation.splitlines():
            parts.append(f"        {line}")
    return "\n".join(parts)
