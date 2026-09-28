# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Insight Engine — main orchestrator.

Combines signal readers, narrative templates, the Level 2 correlator,
and output formatters into a unified pipeline:

  DB → Signals → Narration → Correlation → Formatting → Output

Usage:
    engine = InsightEngine(db_path)
    print(engine.brief())
    print(engine.detail())
    data = engine.to_json()
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .signals import Signal, read_all_signals
from .templates import narrate
from .correlator import Insight, correlate
from .formatters import (
    format_cli_brief,
    format_cli_detail,
    format_json,
    format_slack,
    format_email_digest,
)


class InsightEngine:
    """
    Main entry point for the NØMAÐ Insight Engine.

    Reads signals from the database, narrates them using templates,
    correlates related signals into multi-signal insights, and
    formats the output for various delivery channels.
    """

    def __init__(
        self,
        db_path: Path | str,
        hours: int = 24,
        cluster_name: str = "cluster",
    ):
        self.db_path = Path(db_path)
        self.hours = hours
        self.cluster_name = cluster_name

        # Run the pipeline
        self._signals: list[Signal] = []
        self._narratives: list[tuple[Signal, str]] = []
        self._insights: list[Insight] = []
        self._run()

    def _run(self) -> None:
        """Execute the full insight pipeline."""
        # Step 1: Read all signals from DB
        self._signals = read_all_signals(self.db_path, hours=self.hours)

        # Step 2: Narrate each signal
        self._narratives = []
        for sig in self._signals:
            text = narrate(sig)
            cluster = sig.metrics.get('cluster', '')
            if cluster and not text.startswith(cluster):
                text = f'{cluster}: {text}'
            self._narratives.append((sig, text))

        # Step 3: Correlate signals into multi-signal insights (Level 2)
        self._insights = correlate(self._signals)

        # Step 4: Sort insights by severity
        sev_order = {"critical": 0, "warning": 1, "notice": 2, "info": 3}
        self._insights.sort(key=lambda i: sev_order.get(i.severity.value, 4))

    # ── Output methods ───────────────────────────────────────────────────

    def brief(self) -> str:
        """CLI brief output (for `nomad insights brief`)."""
        return format_cli_brief(self._narratives, self._insights, self.cluster_name)

    def detail(self) -> str:
        """CLI detailed output (for `nomad insights detail`)."""
        return format_cli_detail(self._narratives, self._insights, self.cluster_name)

    def to_json(self) -> str:
        """JSON output (for API/Console)."""
        return format_json(self._narratives, self._insights, self.cluster_name)

    def to_dict(self) -> dict:
        """Python dict output (for programmatic use)."""
        return json.loads(self.to_json())

    def to_slack(self) -> str:
        """Slack-formatted message."""
        return format_slack(self._narratives, self._insights, self.cluster_name)

    def to_email(self, period: str = "daily") -> tuple[str, str]:
        """Email digest (subject, body)."""
        return format_email_digest(
            self._narratives, self._insights, self.cluster_name, period
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

    @property
    def overall_health(self) -> str:
        """Return overall health as a string."""
        from .signals import Severity
        all_sev = [s.severity for s in self._signals] + [i.severity for i in self._insights]
        if not all_sev:
            return "good"
        worst = max(all_sev,
                    key=lambda s: [Severity.INFO, Severity.NOTICE, Severity.WARNING, Severity.CRITICAL].index(s))
        return {
            Severity.INFO: "good",
            Severity.NOTICE: "nominal",
            Severity.WARNING: "degraded",
            Severity.CRITICAL: "impaired",
        }[worst]
