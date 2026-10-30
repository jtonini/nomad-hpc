# Changelog

## v1.21.0 (October 2026)

### Features
- The policy watch reaches GPU workstations and keys on the GPU. `workstations`
  lists GPU boxes to probe over ssh (role "workstation"); the findings travel
  the same pipeline to the hub and the Console. With `watch_gpu` (on by
  default; nothing where there is no GPU), the probe reads GPU **compute**
  processes with `nvidia-smi` — unprivileged, and never graphics (Xorg, a
  browser), so a desktop's ordinary GPU use doesn't show:
  - `gpu` (listed, not alerted): a GPU compute process, with the memory held,
    whether it is detached (no terminal — the shape of an unattended job) and
    whether it is an AI agent.
  - `gpu_served` (warning): a GPU reachable from the network — it listens on a
    port open to the network, or its account also runs a tunnel/relay/forward.
    The shape of reselling or an open inference server.
  - `gpu_min_mem` (256 MiB) drops a trivial compute context; `gpu_serve_hosts`
    names boxes where serving a GPU is sanctioned (then listed, not alerted);
    `ignore_programs`/`ignore_users` apply as everywhere.
  - Under-claim by design: detached only annotates, never alerts on its own;
    per-process GPU attribution is opaque under MIG or in containers, and a
    GPU another account serves is caught through its account's exposure unless
    the probe runs as root. See docs/policy_watch.md.
- Console: the Policy watch page labels the two new kinds (`gpu`, `gpu_served`)
  and names the workstations in its intro. No new route or scoping change.

## v1.20.1 (October 2026)

### Fixes
- Signals place an alert by what it is *about*, not where it was stored. A
  hub that raises a site's node alerts on its behalf (`alerts = "hub"`, 1.17.3)
  stores them in its own rows (`source_site` = the hub) while naming the site
  in `details.site`; the Insight engine attributed them by `source_site`, so
  the hub's Signals (e.g. mingus) showed another site's "node down" and that
  site's own Signals (e.g. vadur) hid it. The alert signal now reads unscoped
  and places each alert by `details.site`, falling back to `source_site`. No
  database change; the Console picks it up once nomad is upgraded on the hub.

## v1.3.1 (April 2026)

### Fixes
- Dashboard blank page regression from NumPy 2.x ABI incompatibility
- Metadata-based probe guards torch_geometric import against C-extension crashes
- Broadened exception handling in ML modules for non-ImportError failures

### Features
- Integrated issue reporting system (`nomad issue report/search/info`)
- Dashboard "Report Issue" tab with structured form and duplicate detection
- GitHub issue templates (bug report, feature request, question)
- Auto-acknowledgment bot via GitHub Actions
- Auto-labeling by source, component, version, and institution
- `nomad ref` entries for issue module and configuration

## v1.3.0 (April 2026)

### Features
- Reference system (`nomad ref`) with 60+ searchable topics
- `nomad ref search` for full-text documentation search
- Reference entries for all commands, configuration, and concepts

### Improvements
- Ruff linting fixes across reference module
- CI configuration excludes generated snippet files

## v1.2.6 (March 2026)

### Features
- System Dynamics module (`nomad dyn`) with 5 ecological/economic metrics
- Insight Engine (`nomad insights`) with multi-signal analysis and narratives
- Cloud monitoring (`nomad cloud`) for AWS, Azure, and GCP
- Dashboard tabs for Insights, Dynamics, and Cloud
- Alert enrichment with insight narratives and recommendations

### Improvements
- Console backend routes for insights and dynamics
- Demo data generator includes stress scenarios for insights
- MkDocs documentation for dynamics, insights, cloud, and reference

## v1.2.1 (February 2026)

### Features
- Proficiency score persistence to database
- Light/dark theme toggle in dashboard
- Combined edu figure in documentation

### Bug Fixes
- Fixed `_resolve_db_path` error in edu commands
- Text wrapping for recommendations output

### Documentation
- Expanded Educational Analytics section in paper
- Added Community Data Sharing details
- Reordered bibliography to match citation order

## v1.2.0 (January 2026)

### Features
- Educational Analytics module (`nomad edu`)
- Community dataset export (`nomad community`)
- Multi-cluster dashboard tabs
- Interactive session monitoring
- 3D force-directed network visualization

### Improvements
- Partition-specific dashboard views
- Node health reflects CPU/memory pressure
- Failed jobs modal with categories

## v1.1.0 (December 2025)

### Features
- ML prediction ensemble (GNN, LSTM, Autoencoder)
- Derivative analysis for trend detection
- Slack and webhook alert backends

## v1.0.0 (November 2025)

Initial release.
