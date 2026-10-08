# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""``nomad usage-report``: the administrators' period report.

How much a cluster was used over a period, by whom (as counts, never names),
how long work waited, what was held and not used, what ran on the GPUs, how
storage grows, and when demand passes capacity. Every figure is a *fact* with
its source, period and kind (measured, estimated, projected), so the same
numbers feed the Markdown report, the JSON file and anything built later on
them.

Layers, each seeing less than the one before:

- ``sources``: reads nomad's database (or an sacct export) and turns people
  into numbers, job names and working directories into application
  families, and partitions into classes. Nothing past it holds a name.
- ``sections``: the fourteen sections, each a finding, its facts and tables.
- ``render``: Markdown and JSON; ``guard`` refuses to write a report that
  contains a username, group, job name or condo partition from the data.

Site settings (node tiers, partition classes, application families,
capacity) live in a local ``report.toml`` that is never committed; see
docs/report.example.toml.
"""
