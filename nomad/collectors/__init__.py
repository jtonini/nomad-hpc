# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD Collectors

Data collectors for monitoring HPC infrastructure.
"""

from .base import (
    BaseCollector,
    CollectionError,
    CollectionResult,
    CollectorRegistry,
    registry,
)
from .disk import DiskCollector
from .gpu import GPUCollector
from .interactive import InteractiveCollector
from .groups import GroupCollector
from .iostat import IOStatCollector
from .job_metrics import JobMetricsCollector
from .mpstat import MPStatCollector
from .nfs import NFSCollector
from .node_state import NodeStateCollector
from .slurm import SlurmCollector
from .storage import StorageCollector
from .vmstat import VMStatCollector
from .workstation import WorkstationCollector



__all__ = [
    'BaseCollector',
    'CollectionError',
    'CollectionResult',
    'CollectorRegistry',
    'registry',
    'DiskCollector',
    'SlurmCollector',
    'JobMetricsCollector',
    'IOStatCollector',
    'MPStatCollector',
    'VMStatCollector',
    'NodeStateCollector',
    'GPUCollector',
    'NFSCollector',
    'GroupCollector',
    'WorkstationCollector',
    'StorageCollector',
    'NetworkPerfCollector',
]
from .network_perf import NetworkPerfCollector
