"""
NØMAÐ Diagnostics Module

Provides unified diagnostics for HPC infrastructure:
- Nodes (HPC compute nodes)
- Workstations (departmental machines)
- NAS (storage devices)
- Network (network paths and performance)
"""

from .network import diagnose_network
from .network import format_diagnostic as format_network_diagnostic
from .node import diagnose_node
from .node import format_diagnostic as format_node_diagnostic
from .storage import diagnose_storage
from .storage import format_diagnostic as format_storage_diagnostic
from .workstation import diagnose_workstation
from .workstation import format_diagnostic as format_workstation_diagnostic

__all__ = [
    'diagnose_node',
    'diagnose_workstation',
    'diagnose_storage',
    'diagnose_network',
    'format_node_diagnostic',
    'format_workstation_diagnostic',
    'format_storage_diagnostic',
    'format_network_diagnostic',
]
