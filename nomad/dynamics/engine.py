# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
DynamicsEngine — orchestrator for the NØMAÐ System Dynamics module.

Runs all dynamics analyses (diversity, niche overlap, carrying capacity,
resilience, externality) and provides unified output methods. Integrates
with the Insight Engine for natural-language narrative generation.

Usage:
    engine = DynamicsEngine(db_path)
    print(engine.full_summary())
    print(engine.diversity_report())
    data = engine.to_json()
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .diversity import DiversityResult, compute_diversity
from .niche import NicheResult, compute_niche_overlap
from .capacity import CapacityResult, compute_capacity
from .resilience import ResilienceResult, compute_resilience
from .externality import ExternalityResult, compute_externalities
from .formatters import (
    format_diversity_cli,
    format_diversity_json,
    format_niche_cli,
    format_niche_json,
    format_capacity_cli,
    format_capacity_json,
    format_resilience_cli,
    format_resilience_json,
    format_externality_cli,
    format_externality_json,
    format_full_summary_cli,
    format_full_summary_json,
)


class DynamicsEngine:
    """
    Main entry point for the NØMAÐ System Dynamics module.

    Orchestrates all dynamics analyses and provides unified output
    for CLI, JSON (Console API), and integration with the Insight Engine.
    """

    def __init__(
        self,
        db_path: Path | str,
        hours: int = 168,
        cluster_name: str = "cluster",
        resilience_hours: int = 720,
    ):
        self.db_path = Path(db_path)
        self.hours = hours
        self.cluster_name = cluster_name
        self.resilience_hours = resilience_hours

        # Results — computed lazily
        self._diversity: DiversityResult | None = None
        self._niche: NicheResult | None = None
        self._capacity: CapacityResult | None = None
        self._resilience: ResilienceResult | None = None
        self._externality: ExternalityResult | None = None

    # ── Lazy computation ──────────────────────────────────────────────

    @property
    def diversity(self) -> DiversityResult:
        if self._diversity is None:
            self._diversity = compute_diversity(
                self.db_path, dimension="group", hours=self.hours
            )
        return self._diversity

    @property
    def diversity_by_user(self) -> DiversityResult:
        if not hasattr(self, '_diversity_user') or self._diversity_user is None:
            self._diversity_user = compute_diversity(
                self.db_path, dimension="user", hours=self.hours
            )
        return self._diversity_user

    @property
    def niche(self) -> NicheResult:
        if self._niche is None:
            self._niche = compute_niche_overlap(
                self.db_path, hours=self.hours
            )
        return self._niche

    @property
    def capacity(self) -> CapacityResult:
        if self._capacity is None:
            self._capacity = compute_capacity(
                self.db_path, hours=self.hours
            )
        return self._capacity

    @property
    def resilience(self) -> ResilienceResult:
        if self._resilience is None:
            self._resilience = compute_resilience(
                self.db_path, hours=self.resilience_hours
            )
        return self._resilience

    @property
    def externality(self) -> ExternalityResult:
        if self._externality is None:
            self._externality = compute_externalities(
                self.db_path, hours=self.hours
            )
        return self._externality

    # ── Run all ───────────────────────────────────────────────────────

    def run_all(self) -> None:
        """Force computation of all dynamics metrics."""
        _ = self.diversity
        _ = self.diversity_by_user
        _ = self.niche
        _ = self.capacity
        _ = self.resilience
        _ = self.externality

    # ── Output methods ────────────────────────────────────────────────

    def full_summary(self) -> str:
        """Full CLI summary combining all dynamics metrics."""
        self.run_all()
        return format_full_summary_cli(
            self.diversity, self.niche, self.capacity,
            self.resilience, self.externality, self.cluster_name,
        )

    def diversity_report(self, dimension: str = "group") -> str:
        """CLI diversity report."""
        if dimension != "group" or self._diversity is None:
            self._diversity = compute_diversity(
                self.db_path, dimension=dimension, hours=self.hours
            )
        return format_diversity_cli(self.diversity)

    def niche_report(self) -> str:
        """CLI niche overlap report."""
        return format_niche_cli(self.niche)

    def capacity_report(self) -> str:
        """CLI carrying capacity report."""
        return format_capacity_cli(self.capacity)

    def resilience_report(self) -> str:
        """CLI resilience report."""
        return format_resilience_cli(self.resilience)

    def externality_report(self) -> str:
        """CLI externality report."""
        return format_externality_cli(self.externality)

    def to_json(self) -> str:
        """Full dynamics report as JSON string."""
        self.run_all()
        return format_full_summary_json(
            self.diversity, self.niche, self.capacity,
            self.resilience, self.externality, self.cluster_name,
        )

    def to_dict(self) -> dict:
        """Full dynamics report as Python dict."""
        result = json.loads(self.to_json())
        # Add user-level diversity alongside group diversity
        du = self.diversity_by_user
        result['diversity_by_user'] = {
            'dimension': 'user',
            'current': {
                'shannon_h': du.current.shannon_h,
                'simpson_d': du.current.simpson_d,
                'richness': du.current.richness,
                'dominant_category': du.current.dominant_category,
                'dominant_proportion': du.current.dominant_proportion,
                'category_counts': dict(du.current.category_counts),
            },
        }
        return result
