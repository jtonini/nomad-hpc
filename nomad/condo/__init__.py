# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Monthly condo reports: what each lab-owned set of nodes did for its owner.

A condo is a partition of nodes a lab bought, lent to everyone else through a
guest partition when the lab isn't using them. Each month, each owner gets a
report on their own nodes (hours in service, the core-hours their lab used,
guests used and nobody used, how fast their jobs started) and on their lab's
use of the shared nodes, with each member of the lab by NetID. Research
computing gets a summary of every condo, grouped by cluster.

Unlike `nomad usage-report`, these reports name people: the owner's own lab
members (``name_members``). Guests on the owner's nodes are only ever totals.
The files are written readable only by the account that writes them.

    nomad usage-report condos init        draft the condos into report.toml
    nomad usage-report condos             build last month's reports
    nomad usage-report condos --send      ... and mail them

Settings: docs/condo-reports.md, and [report.labs], [report.policy] and
[report.clusters.NAME.condos.CONDO] in report.toml.
"""
