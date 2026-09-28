# NØMAÐ Cloud Collector Modules

Cloud collector modules that ingest metrics from cloud provider APIs and
normalize them into the same schema that on-prem collectors use. Everything
downstream — analysis engines, TESSERA, alerting, dashboards, insights —
works unchanged.

## Architecture

```
Cloud Provider APIs                    NØMAÐ Pipeline (unchanged)
─────────────────                      ─────────────────────────
                                       
AWS CloudWatch ──┐                     ┌── derivatives
                 │    ┌──────────┐     ├── TESSERA
Azure Monitor ───┼───>│ cloud_   │────>├── alerting
                 │    │ metrics  │     ├── dashboards
GCP Monitoring ──┘    │ table    │     └── community detection
                      └──────────┘
                      (normalized)
```

**Key design decision:** Cloud collectors normalize metrics into the
`cloud_metrics` table using the same field names (`cpu_util`, `mem_util`,
`net_recv_bytes`, etc.) as on-prem collectors. Downstream analysis sees
no difference between a metric from a Slurm node and one from an EC2
instance.

## Current Status

| Provider | Status | SDK Required |
|----------|--------|-------------|
| AWS      | Implemented | `boto3` |
| Azure    | Planned | `azure-monitor-query` |
| GCP      | Planned | `google-cloud-monitoring` |

## Quick Start

### 1. Install the AWS SDK

```bash
pip install boto3
```

### 2. Configure credentials

Add to your `nomad.toml`:

```toml
[collectors.cloud.aws]
enabled = true
region = "us-east-1"
profile = "research-account"
account_alias = "research-aws"
```

Or use environment variables:

```bash
export NOMAD_AWS_ACCESS_KEY_ID="AKIA..."
export NOMAD_AWS_SECRET_ACCESS_KEY="..."
export NOMAD_AWS_REGION="us-east-1"
```

See `config/cloud_collectors.toml.example` for all options.

### 3. Verify connectivity

```bash
nomad cloud status
```

### 4. Collect metrics

```bash
nomad collect cloud          # all enabled providers
nomad collect cloud aws      # AWS only
```

### 5. View instances

```bash
nomad cloud instances
```

## What NØMAÐ Adds Over Native Cloud Monitoring

- **Research workload awareness** — job communities, user groups, TESSERA
  regime detection applied to cloud workloads
- **Predictive analytics** — derivative-based trend detection, failure risk
  scoring, not just threshold alerts
- **Cross-environment visibility** — cloud and on-prem in the same dashboard,
  same analysis pipeline
- **Cost-aware insights** — spending patterns correlated with workload
  behavior ("spending is accelerating because user X's jobs are scaling
  inefficiently")

## What NØMAÐ Does NOT Try To Do

- Replace cloud-native metric collection (CloudWatch does this better on
  its own platform)
- Manage cloud infrastructure (scaling, provisioning, cost optimization)
- Compete with cloud-native alerting on basic thresholds

## Module Structure

```
nomad/collectors/cloud/
├── __init__.py          # Package init, conditional provider imports
├── cloud_base.py        # CloudBaseCollector (abstract base)
├── aws.py               # AWS CloudWatch + Cost Explorer collector
├── cli_integration.py   # CLI subcommands (collect, status, instances)
└── (azure.py)           # Planned
└── (gcp.py)             # Planned
```

## Extending: Adding a New Cloud Provider

The architecture is designed so that adding Azure or GCP follows the same
pattern as the AWS collector. A new provider needs to:

1. **Create `nomad/collectors/cloud/<provider>.py`**
2. **Subclass `CloudBaseCollector`** and implement:
   - `_authenticate()` — set up the SDK client
   - `_list_instances()` — enumerate monitored resources
   - `_collect_metrics(start, end)` — fetch metrics for the time window
   - `_normalize_metric(raw, instance)` — map to `CloudMetric`
   - `_collect_cost(start, end)` — (optional) fetch billing data
3. **Add the provider's metric name mapping** (like `AWS_METRIC_MAP`)
4. **Register with `@registry.register`**
5. **Add conditional import in `__init__.py`**
6. **Add the provider in `cli_integration.py._get_collector()`**

Everything else — database schema, downstream analysis, dashboards — works
automatically.

## Authentication Methods

### AWS

| Method | Config Key | When to Use |
|--------|-----------|-------------|
| Named profile | `profile` | Development, local workstations |
| Explicit keys | `access_key_id` + `secret_access_key` | Service accounts |
| IAM instance role | (none needed) | Running on EC2 |
| Assume role | `role_arn` | Cross-account monitoring |
| Environment vars | `NOMAD_AWS_*` | CI/CD, containers |

Environment variables always override config-file values (12-factor convention).

## Database Schema

All cloud metrics are stored in a single `cloud_metrics` table:

```sql
CREATE TABLE cloud_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    node_name TEXT NOT NULL,       -- instance name or ID
    cluster TEXT NOT NULL,         -- account alias / subscription / project
    metric_name TEXT NOT NULL,     -- NØMAÐ canonical name (cpu_util, etc.)
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    source TEXT NOT NULL,          -- "aws", "azure", "gcp"
    instance_type TEXT,
    availability_zone TEXT,
    tags TEXT,
    cost_usd REAL
);
```

The `source` column distinguishes providers. The `metric_name` column uses
NØMAÐ canonical names (same as on-prem), enabling cross-environment queries.
