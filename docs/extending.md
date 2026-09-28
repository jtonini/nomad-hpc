# Extending NØMAÐ-HPC

This guide explains how to add new modules to NØMAÐ-HPC, including collectors, diagnostics, and alert backends.

## Architecture Overview
```
┌─────────────────────────────────────────────────────────────────────────┐
│                           NØMAÐ-HPC Architecture                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   Collectors          Analysis           Alerts          Visualization │
│   ───────────         ────────           ──────          ───────────── │
│   collectors/    →    analysis/     →    alerts/    →    viz/          │
│                       diag/                                             │
│                                                                         │
│   Data Sources        Processing         Notification    Dashboard      │
│   - SLURM             - Derivatives      - Email         - Web UI       │
│   - System metrics    - Similarity       - Slack         - CLI          │
│   - Storage           - Trends           - Webhooks                     │
│   - Network           - Diagnostics                                     │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## Adding a New Collector

Collectors gather metrics from various sources. All collectors inherit from `BaseCollector`.

### Step 1: Create the Collector File
```bash
# Create new collector in nomad/collectors/
touch nomad/collectors/my_collector.py
```

### Step 2: Implement the Collector
```python
"""
my_collector.py - Collect metrics from my data source
"""
from dataclasses import dataclass
from typing import Optional, Dict, Any
from .base import BaseCollector

@dataclass
class MyMetrics:
    """Data class for my metrics."""
    timestamp: str
    value: float
    status: str
    # Add your fields here

class MyCollector(BaseCollector):
    """Collector for my data source."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.name = "my_collector"
        # Initialize any state
    
    def collect(self) -> Optional[MyMetrics]:
        """
        Collect metrics from the data source.
        
        Returns:
            MyMetrics object or None if collection fails
        """
        try:
            # Your collection logic here
            # Example: run a command, parse output
            result = self._run_command("my-command --json")
            
            if result is None:
                return None
            
            # Parse and return metrics
            return MyMetrics(
                timestamp=self._get_timestamp(),
                value=float(result.get("value", 0)),
                status=result.get("status", "unknown")
            )
        except Exception as e:
            self.logger.error(f"Collection failed: {e}")
            return None
    
    def collect_to_db(self, db_path: str) -> bool:
        """
        Collect metrics and store in database.
        
        Args:
            db_path: Path to SQLite database
            
        Returns:
            True if successful
        """
        metrics = self.collect()
        if metrics is None:
            return False
        
        # Store in database
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS my_metrics (
                timestamp TEXT,
                value REAL,
                status TEXT
            )
        """)
        
        cursor.execute(
            "INSERT INTO my_metrics VALUES (?, ?, ?)",
            (metrics.timestamp, metrics.value, metrics.status)
        )
        
        conn.commit()
        conn.close()
        return True
```

### Step 3: Register the Collector

Edit `nomad/collectors/__init__.py`:
```python
from .my_collector import MyCollector, MyMetrics

__all__ = [
    # ... existing exports
    "MyCollector",
    "MyMetrics",
]
```

### Step 4: Add Configuration

Update `nomad.toml.example`:
```toml
[collectors.my_collector]
enabled = true
interval = 60  # seconds
# Add your config options
```

---

## Adding a New Diagnostic Module

Diagnostics analyze collected data and provide health assessments.

### Step 1: Create the Diagnostic File
```bash
touch nomad/diag/my_diagnostic.py
```

### Step 2: Implement the Diagnostic
```python
"""
my_diagnostic.py - Diagnostic analysis for my data source
"""
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta

@dataclass
class MyDiagnosticResult:
    """Results from my diagnostic analysis."""
    status: str  # 'healthy', 'warning', 'critical'
    score: float  # 0-100
    metrics: Dict[str, Any]
    issues: List[str]
    recommendations: List[str]

def diagnose_my_source(
    db_path: str,
    hours: int = 24,
    thresholds: Optional[Dict[str, float]] = None
) -> MyDiagnosticResult:
    """
    Analyze my data source health.
    
    Args:
        db_path: Path to database
        hours: Hours of history to analyze
        thresholds: Custom thresholds (optional)
        
    Returns:
        MyDiagnosticResult with analysis
    """
    import sqlite3
    
    # Default thresholds
    thresholds = thresholds or {
        "warning": 80.0,
        "critical": 95.0,
    }
    
    # Query recent data
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    cursor.execute("""
        SELECT value, status FROM my_metrics
        WHERE timestamp > ?
        ORDER BY timestamp DESC
    """, (cutoff,))
    
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return MyDiagnosticResult(
            status="unknown",
            score=0,
            metrics={},
            issues=["No data available"],
            recommendations=["Ensure collector is running"]
        )
    
    # Analyze data
    values = [r[0] for r in rows]
    avg_value = sum(values) / len(values)
    max_value = max(values)
    
    issues = []
    recommendations = []
    
    # Determine status
    if max_value >= thresholds["critical"]:
        status = "critical"
        score = 20
        issues.append(f"Value exceeded critical threshold: {max_value:.1f}")
        recommendations.append("Immediate investigation required")
    elif max_value >= thresholds["warning"]:
        status = "warning"
        score = 60
        issues.append(f"Value exceeded warning threshold: {max_value:.1f}")
        recommendations.append("Monitor closely")
    else:
        status = "healthy"
        score = 95
    
    return MyDiagnosticResult(
        status=status,
        score=score,
        metrics={
            "average": avg_value,
            "maximum": max_value,
            "samples": len(rows),
        },
        issues=issues,
        recommendations=recommendations
    )


def format_my_diagnostic(result: MyDiagnosticResult) -> str:
    """Format diagnostic result for CLI output."""
    lines = []
    
    # Status header with color
    status_colors = {
        "healthy": "\033[92m",   # Green
        "warning": "\033[93m",   # Yellow
        "critical": "\033[91m",  # Red
        "unknown": "\033[90m",   # Gray
    }
    reset = "\033[0m"
    color = status_colors.get(result.status, "")
    
    lines.append(f"\n{'='*60}")
    lines.append(f"  MY DIAGNOSTIC REPORT")
    lines.append(f"{'='*60}")
    lines.append(f"  Status: {color}{result.status.upper()}{reset}")
    lines.append(f"  Score:  {result.score:.0f}/100")
    
    # Metrics
    lines.append(f"\n  Metrics:")
    for key, value in result.metrics.items():
        if isinstance(value, float):
            lines.append(f"    {key}: {value:.2f}")
        else:
            lines.append(f"    {key}: {value}")
    
    # Issues
    if result.issues:
        lines.append(f"\n  Issues:")
        for issue in result.issues:
            lines.append(f"    ⚠ {issue}")
    
    # Recommendations
    if result.recommendations:
        lines.append(f"\n  Recommendations:")
        for rec in result.recommendations:
            lines.append(f"    → {rec}")
    
    lines.append(f"{'='*60}\n")
    
    return "\n".join(lines)
```

### Step 3: Register the Diagnostic

Edit `nomad/diag/__init__.py`:
```python
from .my_diagnostic import diagnose_my_source, format_my_diagnostic

__all__ = [
    # ... existing exports
    "diagnose_my_source",
    "format_my_diagnostic",
]
```

### Step 4: Add CLI Command

Edit `nomad/cli.py` to add the diagnostic command:
```python
@diag.command()
@click.argument('target')
@click.option('--hours', '-h', default=24, help='Hours of history')
@click.option('--json', 'as_json', is_flag=True, help='JSON output')
def mysource(target, hours, as_json):
    """Diagnose my data source health."""
    from nomad.diag import diagnose_my_source, format_my_diagnostic
    
    result = diagnose_my_source(get_db_path(), hours=hours)
    
    if as_json:
        click.echo(json.dumps(asdict(result), indent=2))
    else:
        click.echo(format_my_diagnostic(result))
```

---

## Adding a New Alert Backend

Alert backends send notifications when thresholds are exceeded.

### Step 1: Create the Backend
```python
# In nomad/alerts/backends.py or new file

class MyAlertBackend:
    """Send alerts to my notification system."""
    
    def __init__(self, config: Dict[str, Any]):
        self.api_url = config.get("api_url")
        self.api_key = config.get("api_key")
    
    def send(self, alert: Alert) -> bool:
        """
        Send alert notification.
        
        Args:
            alert: Alert object with severity, message, etc.
            
        Returns:
            True if sent successfully
        """
        import urllib.request
        import json
        
        payload = {
            "severity": alert.severity,
            "title": alert.title,
            "message": alert.message,
            "timestamp": alert.timestamp,
        }
        
        req = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
        )
        
        try:
            urllib.request.urlopen(req)
            return True
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")
            return False
```

### Step 2: Register the Backend
```python
# In nomad/alerts/dispatcher.py

BACKENDS = {
    "email": EmailBackend,
    "slack": SlackBackend,
    "webhook": WebhookBackend,
    "my_backend": MyAlertBackend,  # Add your backend
}
```

### Step 3: Add Configuration
```toml
# In nomad.toml
[alerts.backends.my_backend]
enabled = true
api_url = "https://api.myservice.com/alerts"
api_key = "your-api-key"
```

---

## Adding a Dashboard Tab

### Step 1: Add API Endpoint

In `nomad/viz/server.py`, add a new endpoint:
```python
elif parsed.path == '/api/my_data':
    data = self._get_my_data()
    self._send_json(data)
```

### Step 2: Add Tab Content

Add the tab in the React component within `server.py`:
```javascript
// In renderTabs() or similar
case 'my_tab':
    return React.createElement(MyTabComponent, { data: this.state.myData });
```

---

## Testing Your Module

### Unit Tests

Create `tests/test_my_collector.py`:
```python
import pytest
from nomad.collectors.my_collector import MyCollector, MyMetrics

def test_collect_returns_metrics():
    collector = MyCollector()
    result = collector.collect()
    assert isinstance(result, MyMetrics)
    
def test_collect_handles_error():
    collector = MyCollector(config={"invalid": True})
    result = collector.collect()
    assert result is None or isinstance(result, MyMetrics)
```

### Integration Tests
```python
def test_collector_to_db(tmp_path):
    db_path = tmp_path / "test.db"
    collector = MyCollector()
    
    success = collector.collect_to_db(str(db_path))
    assert success
    
    # Verify data was stored
    import sqlite3
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM my_metrics")
    count = cursor.fetchone()[0]
    assert count > 0
```

### Run Tests
```bash
pytest tests/test_my_collector.py -v
```

---

## Best Practices

### 1. Use Dataclasses for Metrics
```python
from dataclasses import dataclass, asdict

@dataclass
class MyMetrics:
    timestamp: str
    value: float
    
    def to_dict(self):
        return asdict(self)
```

### 2. Handle Errors Gracefully
```python
def collect(self):
    try:
        # Collection logic
        pass
    except FileNotFoundError:
        self.logger.warning("Data source not found")
        return None
    except PermissionError:
        self.logger.error("Permission denied")
        return None
    except Exception as e:
        self.logger.error(f"Unexpected error: {e}")
        return None
```

### 3. Use Configuration
```python
def __init__(self, config=None):
    config = config or {}
    self.interval = config.get("interval", 60)
    self.threshold = config.get("threshold", 90.0)
```

### 4. Add Logging
```python
import logging
logger = logging.getLogger(__name__)

logger.debug("Starting collection")
logger.info("Collected 100 samples")
logger.warning("Value near threshold")
logger.error("Collection failed")
```

### 5. Document Your Module
```python
"""
my_collector.py - Collect metrics from XYZ

This collector gathers:
- Metric A: Description
- Metric B: Description

Configuration:
    [collectors.my_collector]
    enabled = true
    interval = 60

Example:
    collector = MyCollector()
    metrics = collector.collect()
"""
```

---

## Checklist for New Modules

- [ ] Create module file in appropriate directory
- [ ] Implement core functionality
- [ ] Add to `__init__.py` exports
- [ ] Add configuration options to `nomad.toml.example`
- [ ] Add CLI command if needed
- [ ] Write unit tests
- [ ] Add documentation
- [ ] Update CHANGELOG.md

---

## Getting Help

- **GitHub Issues**: [Report bugs or request features](https://github.com/jtonini/nomad-hpc/issues)
- **Architecture Guide**: See [ARCHITECTURE.md](ARCHITECTURE.md)
- **Contributing Guide**: See [CONTRIBUTING.md](CONTRIBUTING.md)
