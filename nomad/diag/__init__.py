"""
NØMAD Diagnostics Module

Provides unified diagnostics for HPC infrastructure:
- Nodes (HPC compute nodes)
- Workstations (departmental machines) - coming soon
- NAS (storage devices) - coming soon
"""

from .node import diagnose_node

__all__ = ['diagnose_node']

# Future imports when modules are ready:
# from .workstation import diagnose_workstation
# from .nas import diagnose_nas
