"""
NØMAD Diagnostics Module

Provides unified diagnostics for HPC infrastructure:
- Nodes (HPC compute nodes)
- Workstations (departmental machines)
- NAS (storage devices)
"""

from .node import diagnose_node
from .workstation import diagnose_workstation
from .nas import diagnose_nas

__all__ = ['diagnose_node', 'diagnose_workstation', 'diagnose_nas']
