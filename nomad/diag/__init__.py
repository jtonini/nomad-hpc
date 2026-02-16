"""
NØMAD Diagnostics Module

Provides unified diagnostics for HPC infrastructure:
- Nodes (HPC compute nodes)
- Workstations (departmental machines)
- NAS (storage devices)
"""

from .node import diagnose_node, format_diagnostic as format_node_diagnostic
from .workstation import diagnose_workstation, format_diagnostic as format_workstation_diagnostic
from .storage import diagnose_storage, format_diagnostic as format_storage_diagnostic

__all__ = [
    'diagnose_node',
    'diagnose_workstation', 
    'diagnose_storage',
    'format_node_diagnostic',
    'format_workstation_diagnostic',
    'format_storage_diagnostic',
]
