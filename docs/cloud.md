# Cloud Monitoring

NØMAÐ supports hybrid cloud monitoring alongside on-premises HPC clusters.

## Supported Providers

- **AWS** — EC2 instances via CloudWatch
- **Azure** — Virtual machines via Azure Monitor
- **GCP** — Compute instances via Cloud Monitoring

## Commands

```bash
nomad cloud status              # Check provider connectivity
nomad cloud instances           # List discovered instances
nomad cloud instances --provider aws  # Filter by provider
```

## Configuration

```toml
[collectors.cloud.aws]
enabled = true
region = "us-east-1"
credentials = "profile"

[collectors.cloud.azure]
enabled = false

[collectors.cloud.gcp]
enabled = false
```

## Console Integration

The NØMAÐ Console provides three cloud views:

- **Monitoring** — real-time instance metrics
- **Analytics** — cost and utilization analysis
- **Forecast** — projected spend and capacity
