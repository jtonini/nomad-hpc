# NØMAÐ Sample Data

Pre-collected metrics for testing NØMAÐ without running a VM.

## Files

| File | Description | Duration | Interval |
|------|-------------|----------|----------|
| `overnight-metrics.json` | Real data from VM simulation | ~12 hours | 5 sec |
| `minimal-1h.json` | Simulated minimal cluster | 1 hour | 5 sec |
| `minimal-24h.json` | Simulated minimal cluster | 24 hours | 60 sec |
| `small-1h.json` | Simulated small cluster | 1 hour | 5 sec |
| `large-24h.json` | Simulated large cluster | 24 hours | 60 sec |

## Usage

### Test analysis without VM

```python
import json
from nomad.analysis.derivatives import DerivativeAnalyzer
from datetime import datetime

# Load sample data
with open('sample-data/overnight-metrics.json') as f:
    data = json.load(f)

# Analyze patterns
analyzer = DerivativeAnalyzer(window_size=10)
for point in data:
    ts = datetime.fromisoformat(point['timestamp'])
    analyzer.add_point(ts, point['used_pct'])
    
    vel = analyzer.first_derivative() or 0
    if abs(vel) > 1:  # >1%/sec change
        print(f"High velocity at {ts}: {vel:.2f}%/s")
```

### Replay through NØMAÐ

```bash
# Future: nomad --replay sample-data/overnight-metrics.json
```

## Data Format

Each record contains:

```json
{
  "timestamp": "2025-12-09T02:15:58.957111",
  "used_pct": 45.2,
  "velocity": 0.35,
  "acceleration": 0.02
}
```

For simulated cluster data (from metrics-generator.py):

```json
{
  "timestamp": "2025-12-09T12:00:00",
  "cluster": "large",
  "nodes": {
    "cpu001": {
      "localscratch_used_mb": 1234.5,
      "localscratch_used_pct": 12.3,
      "running_jobs": 2
    }
  },
  "storage": {
    "/scratch": {
      "total_tb": 5000,
      "used_pct": 42.1
    }
  },
  "jobs": {
    "running": 145,
    "bad_io": 12
  }
}
```

## Generating New Sample Data

```bash
cd vm-simulation/simulation

# Generate for all configs
python metrics-generator.py --config minimal --generate-samples
python metrics-generator.py --config small --generate-samples
python metrics-generator.py --config large --generate-samples

# Custom duration
python metrics-generator.py --config large --duration 86400 --output large-24h.json
```

## Collecting Real Data

From a running VM:

```bash
vagrant ssh
# Overnight data is in /tmp/nomad-metrics.log
cat /tmp/nomad-metrics.log | python3 -c "
import sys, json
data = [json.loads(l) for l in sys.stdin]
print(json.dumps(data, indent=2))
" > overnight-metrics.json
```
