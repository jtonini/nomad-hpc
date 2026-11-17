# Prometheus metrics

`nomad metrics` publishes a small set of numbers for
[Prometheus](https://prometheus.io/) to collect, so Grafana can draw them
beside whatever else your site already collects (node_exporter, DCGM,
Slurm's own). It reads the hub's combined database (or, on a site without a
hub, that site's own) and writes Prometheus's text format.

It never publishes anything about one person: no user, job or lab appears
in a label. Whoever opens a Grafana dashboard sees all of it, and a label
per person or job would make thousands of series. The Console stays the
place where each lab sees only its own.

## What it publishes

| Name | What it is | Labels |
| --- | --- | --- |
| `nomad_build_info` | 1, with the version of nomad that answers | version |
| `nomad_site_last_pull_timestamp_seconds` | When the hub last brought the site's data up to date | site |
| `nomad_collector_last_run_timestamp_seconds` | When each collector last ran (those that ran in the last 7 days) | site, collector |
| `nomad_collector_last_success_timestamp_seconds` | When each collector last ran without an error | site, collector |
| `nomad_readings_too_old` | Readings left out because their next one was due and didn't come | site, source |
| `nomad_node_condition` | 1 for the node's condition now (up, draining, drained, down), 0 for the others | site, node, condition |
| `nomad_node_cpus` | Cores Slurm lists on the node | site, node |
| `nomad_node_cpus_allocated` | Cores allocated to jobs on the node | site, node |
| `nomad_node_cpu_clock_ratio` | The CPU's clock when busy, as a share of what it should give (1: as it should) | site, node |
| `nomad_node_cpu_throttled` | 1 when the clock has read low on reading after reading | site, node |
| `nomad_node_power_problems` | Problems the node's board reports now, by part | site, node, part (power_supply, voltage, fans) |
| `nomad_gpu_busy_ratio` | How busy the GPU was, as DCGM measures it (nvidia-smi's figure is not used) | site, node, gpu |
| `nomad_gpu_memory_used_bytes`, `nomad_gpu_memory_total_bytes` | GPU memory in use, and the GPU's memory | site, node, gpu |
| `nomad_gpu_temperature_celsius`, `nomad_gpu_power_watts` | The GPU's temperature and power draw | site, node, gpu |
| `nomad_partition_jobs` | Jobs in the partition now | site, partition, state (pending, running) |
| `nomad_partition_wait_median_seconds` | The median wait of the jobs that started in the partition in the last `wait_hours` | site, partition |
| `nomad_partition_jobs_started` | How many jobs started there in that time | site, partition |
| `nomad_filesystem_size_bytes`, `_used_bytes`, `_avail_bytes` | Each watched filesystem's size, use and free space | site, path |

All are gauges. `nomad metrics --catalog` prints the same list.

## Gaps instead of old figures

A reading counts until its next one was due, plus a grace period: 1.5 of
its usual interval (as its own readings show it, or a collector's
`every_minutes`), plus `grace_minutes` (by default the hub's
`sync_interval_minutes` and 10 more, or 30). After that it is left out
rather than repeated. When a collector or a site stops, Grafana shows a gap
instead of a flat line that looks fine, and `nomad_readings_too_old` and the
collectors' last runs say what stopped. A node Slurm no longer lists (missing
from its site's latest reading) is left out too. The partitions' waits are
given only for a site whose node readings are current.

## Trying it

```
nomad metrics --summary
```

prints counts only: how many of each name, for which sites, and what was
left out as too old. It names no node, partition or path, so it is safe to
share. `nomad metrics` prints the page itself, as Prometheus would get it.

## Serving it

Serving is off until you turn it on:

```toml
[metrics]
enabled = true                    # nomad metrics serve answers only when true
# listen = "127.0.0.1:10062"      # another address needs token_file
# token_file = "/home/nomad/.config/nomad/metrics_token"
# grace_minutes = 30              # default: [hub] sync_interval_minutes + 10
# wait_hours = 24                 # the median wait: jobs that started in this many hours
# cache_seconds = 30              # asks within this get the same page
# site = "mycluster"              # a site's own database: its name (default: the host's)
```

On `127.0.0.1` only Prometheus on the same host can ask. To let a
Prometheus on another host ask, listen on an address it can reach and set
`token_file`; `nomad metrics serve` refuses another address without one.

```
sudo -u nomad nomad metrics token --apply
sudo nomad metrics service --apply
```

The first writes a new token (mode 600, never printed; its fingerprint is).
The second installs `nomad-metrics.service`, which runs `nomad metrics serve`
as the nomad account and starts it again if it stops. Restart it after
upgrading nomad: `sudo systemctl restart nomad-metrics`.

Prometheus's side, with the token copied to its host:

```yaml
scrape_configs:
  - job_name: nomad
    scrape_interval: 60s
    authorization:
      credentials_file: /etc/prometheus/nomad_token
    static_configs:
      - targets: ["hub.example.edu:10062"]
```

The numbers change every few minutes (as the sites collect and the hub
pulls), so a scrape every minute is plenty.

## node_exporter instead

Where node_exporter already runs with its textfile collector, nomad can
write the page there from cron, and no new port is opened:

```
*/5 * * * * nomad metrics --out /var/lib/node_exporter/textfile/nomad.prom
```

The file is replaced whole, never half written.
