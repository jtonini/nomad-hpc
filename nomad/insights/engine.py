# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Insight Engine — main orchestrator.

Combines signal readers, narrative templates, the Level 2 correlator,
and output formatters into a unified pipeline:

  DB → Signals → Narration → Correlation → Formatting → Output

Usage:
    engine = InsightEngine(db_path)                  # a single site's database
    engine = InsightEngine(combined, site="spydur")  # one site of a combined one
    print(engine.brief())
    data = engine.to_dict()

Alongside the signals the engine keeps a coverage list -- for each source,
whether it was measured, had no data, went stale, or failed -- and the
health it reports rests on it: with nothing measured, health is "unknown",
not "good".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from nomad.db import scope

from .signals import (
    SEVERITY_ORDER,
    Severity,
    Signal,
    read_all_signals_with_coverage,
)
from .templates import narrate
from .correlator import Insight, correlate
from .formatters import (
    format_cli_brief,
    format_cli_detail,
    format_json,
    format_slack,
    format_email_digest,
)

_HEALTH = {
    Severity.INFO: "good",
    Severity.NOTICE: "nominal",
    Severity.WARNING: "degraded",
    Severity.CRITICAL: "impaired",
}


class InsightEngine:
    """
    Main entry point for the NØMAÐ Insight Engine.

    Reads signals from the database, narrates them using templates,
    correlates related signals into multi-signal insights, and
    formats the output for various delivery channels.

    ``site`` limits a combined database to one site. Given a combined
    database and no site, every site is read separately and the findings are
    labelled with their site -- never pooled, since pooling mixes one site's
    filesystems and nodes with another's.
    """

    def __init__(
        self,
        db_path: Path | str,
        hours: int = 24,
        cluster_name: str = "cluster",
        site: Optional[str] = None,
        config: Optional[dict] = None,
    ):
        self.db_path = Path(db_path)
        self.hours = hours
        # An unknown site is an error; no site on a combined database means
        # every site, read separately.
        if site and self.db_path.exists():
            scope.require_site(self.db_path, site)
        self.site = site
        self.cluster_name = site if (site and cluster_name == "cluster") else cluster_name
        self.config = config

        # Run the pipeline
        self._signals: list[Signal] = []
        self._narratives: list[tuple[Signal, str]] = []
        self._insights: list[Insight] = []
        self._coverage: list[dict] = []
        self._run()

    def _run(self) -> None:
        """Execute the full insight pipeline."""
        if not self.db_path.exists():
            self._coverage = [{"source": "database", "label": "Database",
                               "status": "failed", "newest": None, "signals": 0,
                               "detail": f"{self.db_path} not found"}]
            return

        sites = [self.site]
        if self.site is None:
            known = scope.sites(self.db_path)
            if len(known) > 1:
                sites = known

        by_site: list[tuple[Optional[str], list[Signal]]] = []
        for site in sites:
            signals, coverage = read_all_signals_with_coverage(
                self.db_path, hours=self.hours, config=self.config, site=site)
            for entry in coverage:
                entry["site"] = site
            for sig in signals:
                if site and len(sites) > 1:
                    sig.metrics.setdefault("site", site)
            self._coverage.extend(coverage)
            by_site.append((site, signals))

        # Narrate each signal; label it with its site when several are read
        for site, signals in by_site:
            for sig in signals:
                text = narrate(sig)
                label = site if len(sites) > 1 else None
                if label and not text.startswith(label):
                    text = f"{label}: {text}"
                self._narratives.append((sig, text))
                self._signals.append(sig)

            # Correlate within a site only: two sites' signals are not related
            self._insights.extend(correlate(signals))

        # Sort insights by severity
        sev_order = {"critical": 0, "warning": 1, "notice": 2, "info": 3}
        self._insights.sort(key=lambda i: sev_order.get(i.severity.value, 4))

    # ── Health ───────────────────────────────────────────────────────────

    @property
    def coverage(self) -> list[dict]:
        """Per source (and site): measured / stale / no_data / failed."""
        return self._coverage

    @property
    def measured(self) -> bool:
        """True when at least one source had current data to read."""
        return any(c["status"] == "measured" for c in self._coverage)

    @property
    def overall_health(self) -> str:
        """good / nominal / degraded / impaired, or unknown when nothing was measured."""
        if not self.measured:
            return "unknown"
        all_sev = [s.severity for s in self._signals] + [i.severity for i in self._insights]
        if any(c["status"] in ("stale", "failed") for c in self._coverage):
            all_sev.append(Severity.WARNING)
        if not all_sev:
            return "good"
        return _HEALTH[max(all_sev, key=SEVERITY_ORDER.index)]

    # ── Output methods ───────────────────────────────────────────────────

    def brief(self) -> str:
        """CLI brief output (for `nomad insights brief`)."""
        return format_cli_brief(self._narratives, self._insights, self.cluster_name,
                                health=self.overall_health, coverage=self._coverage)

    def detail(self) -> str:
        """CLI detailed output (for `nomad insights detail`)."""
        return format_cli_detail(self._narratives, self._insights, self.cluster_name,
                                 coverage=self._coverage)

    def to_json(self) -> str:
        """JSON output (for API/Console)."""
        return format_json(self._narratives, self._insights, self.cluster_name,
                           health=self.overall_health, coverage=self._coverage,
                           site=self.site, hours=self.hours)

    def to_dict(self) -> dict:
        """Python dict output (for programmatic use)."""
        return json.loads(self.to_json())

    def to_slack(self) -> str:
        """Slack-formatted message."""
        return format_slack(self._narratives, self._insights, self.cluster_name,
                            health=self.overall_health, coverage=self._coverage)

    def to_email(self, period: str = "daily") -> tuple[str, str]:
        """Email digest (subject, body)."""
        return format_email_digest(
            self._narratives, self._insights, self.cluster_name, period,
            health=self.overall_health, coverage=self._coverage,
        )

    # ── Accessors ────────────────────────────────────────────────────────

    @property
    def signals(self) -> list[Signal]:
        return self._signals

    @property
    def insights(self) -> list[Insight]:
        return self._insights

    @property
    def narratives(self) -> list[tuple[Signal, str]]:
        return self._narratives

    @property
    def signal_count(self) -> int:
        return len(self._signals)

    @property
    def insight_count(self) -> int:
        return len(self._insights)
