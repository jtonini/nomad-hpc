# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Insight Engine — translates analytical output into actionable narratives.

Levels:
  1. Template-based narratives (signal → sentence)
  2. Context-aware summaries (multi-signal → correlated insight)
  3. LLM-powered interpretation (future, funded work)
"""
from .engine import InsightEngine
from .signals import Signal, SignalType, Severity
from .correlator import Insight

__all__ = ["InsightEngine", "Signal", "SignalType", "Severity", "Insight"]
