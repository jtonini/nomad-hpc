"""
NØMAD Diagnostics Base Module

Provides shared utilities and base classes for the diagnostic system.

Architecture:
- Devices: Collect data from specific sources (nodes, workstations, NAS)
- Analyzers: Detect issues in data (memory, CPU, I/O, network, GPU)
- Formatters: Present results (terminal, JSON, dashboard)

Analyzers are reusable across device types. For example, MemoryAnalyzer
can detect OOM issues on HPC nodes, workstations, or any monitored system.
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional, Any
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Colors and Formatting
# ══════════════════════════════════════════════════════════════════════

class Colors:
    """ANSI color codes for terminal output."""
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    GRAY = '\033[90m'
    
    @classmethod
    def disable(cls):
        """Disable colors (for non-TTY output)."""
        for attr in ['RESET', 'BOLD', 'DIM', 'RED', 'GREEN', 'YELLOW', 
                     'BLUE', 'MAGENTA', 'CYAN', 'GRAY']:
            setattr(cls, attr, '')


# ══════════════════════════════════════════════════════════════════════
# Data Classes
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Issue:
    """Represents a detected issue."""
    category: str          # 'memory', 'cpu', 'io', 'network', 'gpu', 'config'
    severity: str          # 'critical', 'warning', 'info'
    title: str             # Short description
    detail: str            # Detailed explanation
    evidence: dict = field(default_factory=dict)  # Supporting data
    recommendations: list = field(default_factory=list)


@dataclass 
class DeviceInfo:
    """Base device information."""
    name: str
    device_type: str       # 'node', 'workstation', 'nas'
    cluster: Optional[str] = None
    department: Optional[str] = None
    status: str = 'unknown'
    last_seen: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class DiagnosticReport:
    """Complete diagnostic report for a device."""
    device: DeviceInfo
    timestamp: datetime = field(default_factory=datetime.now)
    issues: list = field(default_factory=list)        # List of Issue objects
    metrics: dict = field(default_factory=dict)       # Current metrics
    history: dict = field(default_factory=dict)       # Historical data
    users: list = field(default_factory=list)         # Active/recent users
    jobs: list = field(default_factory=list)          # Recent jobs (for HPC)
    recommendations: list = field(default_factory=list)  # Aggregated recommendations
    
    def add_issue(self, issue: Issue):
        """Add an issue and its recommendations."""
        self.issues.append(issue)
        self.recommendations.extend(issue.recommendations)
    
    @property
    def severity(self) -> str:
        """Overall severity based on worst issue."""
        if any(i.severity == 'critical' for i in self.issues):
            return 'critical'
        elif any(i.severity == 'warning' for i in self.issues):
            return 'warning'
        elif self.issues:
            return 'info'
        return 'healthy'


# ══════════════════════════════════════════════════════════════════════
# Abstract Base Classes
# ══════════════════════════════════════════════════════════════════════

class DeviceCollector(ABC):
    """Base class for device data collectors."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
    
    def get_connection(self):
        """Get database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    @abstractmethod
    def collect(self, name: str, **kwargs) -> DeviceInfo:
        """Collect device information."""
        pass
    
    @abstractmethod
    def get_metrics(self, name: str, hours: int = 24) -> dict:
        """Get device metrics history."""
        pass
    
    @abstractmethod
    def get_history(self, name: str, hours: int = 24) -> list:
        """Get device state history."""
        pass


class Analyzer(ABC):
    """Base class for diagnostic analyzers."""
    
    category: str = 'general'  # Override in subclass
    
    @abstractmethod
    def analyze(self, report: DiagnosticReport) -> list:
        """
        Analyze data and return list of Issues.
        
        Args:
            report: DiagnosticReport with device info, metrics, history
            
        Returns:
            List of Issue objects detected
        """
        pass


class Formatter(ABC):
    """Base class for output formatters."""
    
    @abstractmethod
    def format(self, report: DiagnosticReport) -> str:
        """Format report for output."""
        pass


# ══════════════════════════════════════════════════════════════════════
# Utility Functions
# ══════════════════════════════════════════════════════════════════════

def format_duration(seconds: int) -> str:
    """Format seconds as human-readable duration."""
    if not seconds:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    elif seconds < 86400:
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        return f"{hours}h {mins}m"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days}d {hours}h"


def format_bytes(bytes_val: float) -> str:
    """Format bytes as human-readable size."""
    if not bytes_val:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(bytes_val) < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} PB"


def format_percent(value: float, decimals: int = 1) -> str:
    """Format percentage value."""
    return f"{value:.{decimals}f}%"


def time_ago(dt) -> str:
    """Format datetime as 'X ago' string."""
    if not dt:
        return "unknown"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except ValueError:
            return dt
    
    delta = datetime.now() - dt
    seconds = delta.total_seconds()
    
    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        mins = int(seconds // 60)
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    elif seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        days = int(seconds // 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"


def severity_color(severity: str, c=Colors) -> str:
    """Get color for severity level."""
    return {
        'critical': c.RED,
        'warning': c.YELLOW,
        'info': c.CYAN,
        'healthy': c.GREEN,
    }.get(severity, c.GRAY)


def status_color(status: str, c=Colors) -> str:
    """Get color for device status."""
    status_lower = (status or '').lower()
    if status_lower in ('online', 'idle', 'allocated', 'mixed', 'up', 'healthy', 'ok'):
        return c.GREEN
    elif status_lower in ('offline', 'down', 'drain', 'error', 'failed', 'critical'):
        return c.RED
    elif status_lower in ('draining', 'warning', 'degraded', 'maintenance'):
        return c.YELLOW
    return c.GRAY


# ══════════════════════════════════════════════════════════════════════
# Registry for Analyzers
# ══════════════════════════════════════════════════════════════════════

class AnalyzerRegistry:
    """Registry of available analyzers."""
    
    _analyzers: dict = {}
    
    @classmethod
    def register(cls, name: str, analyzer_class: type):
        """Register an analyzer."""
        cls._analyzers[name] = analyzer_class
    
    @classmethod
    def get(cls, name: str) -> Optional[type]:
        """Get analyzer by name."""
        return cls._analyzers.get(name)
    
    @classmethod
    def all(cls) -> dict:
        """Get all registered analyzers."""
        return cls._analyzers.copy()
    
    @classmethod
    def for_device(cls, device_type: str) -> list:
        """Get analyzers applicable to a device type."""
        # For now, all analyzers apply to all devices
        # Could add device_types attribute to Analyzer class for filtering
        return list(cls._analyzers.values())


def register_analyzer(name: str):
    """Decorator to register an analyzer."""
    def decorator(cls):
        AnalyzerRegistry.register(name, cls)
        return cls
    return decorator
