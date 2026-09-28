# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ System Dynamics — ``nomad dyn``

Quantitative frameworks from community ecology, economics, and
governance theory applied to research computing environments.

Submodules
----------
diversity   : Workload diversity indices (Shannon, Simpson)
niche       : Resource-usage overlap between user communities
capacity    : Multi-dimensional carrying-capacity utilization
resilience  : Recovery time after disturbance events
externality : Inter-user/group impact quantification
engine      : Orchestrator combining all metrics into a narrative
"""

from .engine import DynamicsEngine

__all__ = ["DynamicsEngine"]
