# Data Readiness

The `nomad readiness` command helps administrators determine when sufficient data has been collected for reliable ML predictions.

## Quick Start
```bash
nomad readiness              # Basic readiness report
nomad readiness -v           # Verbose with feature details
nomad readiness --db mydata.db   # Specify database
```

## What It Assesses

The readiness estimator evaluates four dimensions:

### 1. Sample Size

| Level | Jobs | Score |
|-------|------|-------|
| Minimum | 100+ | 40% |
| Recommended | 500+ | 70% |
| Optimal | 1000+ | 100% |

### 2. Class Balance

Compares successful vs failed jobs. Ideal ratio is between 60:40 and 90:10. Extremely imbalanced data (>95% one class) reduces prediction reliability.

### 3. Feature Coverage

Checks that all 17 feature dimensions have sufficient variance:

- CPU efficiency
- Memory utilization
- I/O wait percentage
- NFS write ratio
- Runtime characteristics
- Exit signals
- And more...

Features with zero variance (all same value) don't contribute to predictions.

### 4. Data Recency

Recent data is weighted more heavily. The estimator checks:

- Jobs from last 24 hours
- Jobs from last 7 days
- Jobs from last 30 days

Stale data (>30 days old only) triggers a warning.

## Output Example
```
======================================================================
                       NØMAÐ Data Readiness
======================================================================
  Overall Score: 72% (Recommended)
======================================================================

  Sample Size       ================....  847 jobs (85%)
  Class Balance     ==============......  82:18 ratio (70%)
  Feature Coverage  ====================  17/17 features
  Data Recency      ==========..........  3 days old (50%)

======================================================================
  Forecast: At 125 jobs/day, optimal threshold (1000)
            will be reached in approximately 2 days.
======================================================================
```

## Verbose Mode

With `-v` or `--verbose`, see per-feature statistics:
```bash
nomad readiness -v
```

Shows coefficient of variation (CV%) for each feature, helping identify which metrics contribute most to predictions.

## Recommendations

The command provides actionable recommendations:

- **Low sample size**: "Continue collecting data. Run `nomad collect` to gather more jobs."
- **Class imbalance**: "Dataset is heavily skewed toward successful jobs. Consider longer collection period to capture more failure modes."
- **Missing features**: "GPU metrics unavailable. Install nvidia-smi for GPU-enabled nodes."
- **Stale data**: "Most recent job is 15 days old. Ensure collectors are running."
