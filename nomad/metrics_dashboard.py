# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A Grafana dashboard for what `nomad metrics` publishes (1.53.0):
``nomad metrics dashboard`` prints it, for Grafana's Dashboards > Import or
a provisioning folder.

It asks which Prometheus (the ``datasource`` variable) and which sites
(``site``, every one by default). Rows: sites and collectors, nodes, CPU
clock and power, GPUs, queues, filesystems. Lines are not drawn across
missing readings: a gap is a gap (what nomad left out as too old, or a
scrape that failed)."""
from __future__ import annotations

DS = {"type": "prometheus", "uid": "${datasource}"}
SITE = 'site=~"$site"'
UID = "nomad-hpc"


def _target(expr: str, legend: str = "", table: bool = False, ref: str = "A") -> dict:
    return {"refId": ref, "datasource": DS, "editorMode": "code", "expr": expr,
            "legendFormat": legend or "__auto", "range": not table, "instant": table,
            "format": "table" if table else "time_series"}


def _steps(*pairs) -> dict:
    """Thresholds: the first color from nothing up, then (value, color)."""
    first, *rest = pairs
    return {"mode": "absolute", "steps": [{"color": first, "value": None}]
            + [{"color": c, "value": v} for v, c in rest]}


class _Layout:
    def __init__(self):
        self.panels: list = []
        self.y = 0
        self.next_id = 1

    def _id(self) -> int:
        n = self.next_id
        self.next_id += 1
        return n

    def row(self, title: str) -> None:
        self.panels.append({"type": "row", "id": self._id(), "title": title, "collapsed": False,
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}, "panels": []})
        self.y += 1

    def add(self, panel: dict, x: int, w: int, h: int, y: int | None = None) -> None:
        panel["id"] = self._id()
        panel["gridPos"] = {"h": h, "w": w, "x": x, "y": self.y if y is None else y}
        panel.setdefault("datasource", DS)
        self.panels.append(panel)

    def down(self, h: int) -> None:
        self.y += h


def stat(title: str, expr: str, description: str, unit: str, thresholds: dict, legend: str = "",
         no_value: str = "none") -> dict:
    return {"type": "stat", "title": title, "description": description, "targets": [_target(expr, legend)],
            "fieldConfig": {"defaults": {"unit": unit, "thresholds": thresholds, "color": {"mode": "thresholds"},
                                         "noValue": no_value, "mappings": []}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "none", "justifyMode": "auto",
                        "orientation": "auto", "textMode": "value_and_name", "wideLayout": True,
                        "showPercentChange": False}}


def series(title: str, expr: str, description: str, unit: str, legend: str, lo=None, hi=None,
           thresholds: dict | None = None, line_at: bool = False) -> dict:
    defaults = {"unit": unit, "color": {"mode": "palette-classic"},
                "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 0, "showPoints": "never",
                           "spanNulls": False, "axisPlacement": "auto",
                           "thresholdsStyle": {"mode": "dashed" if line_at else "off"}},
                "thresholds": thresholds or _steps("green"), "mappings": []}
    if lo is not None:
        defaults["min"] = lo
    if hi is not None:
        defaults["max"] = hi
    return {"type": "timeseries", "title": title, "description": description,
            "targets": [_target(expr, legend)], "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def table(title: str, expr: str, description: str, value_name: str | None, unit: str = "none",
          thresholds: dict | None = None, sort_desc: bool = True) -> dict:
    hide = {"Time": True, "__name__": True, "job": True, "instance": True}
    rename = {}
    if value_name is None:
        hide["Value"] = True
    else:
        rename["Value"] = value_name
    p = {"type": "table", "title": title, "description": description,
         "targets": [_target(expr, table=True)],
         "transformations": [{"id": "organize", "options": {"excludeByName": hide, "renameByName": rename,
                                                             "indexByName": {}}}],
         "fieldConfig": {"defaults": {"unit": unit, "thresholds": thresholds or _steps("text"),
                                      "custom": {"align": "auto", "cellOptions": {"type": "color-text"}},
                                      "mappings": []}, "overrides": []},
         "options": {"showHeader": True, "cellHeight": "sm",
                     "footer": {"show": False, "reducer": ["sum"], "fields": ""}}}
    if value_name is not None:
        p["options"]["sortBy"] = [{"displayName": value_name, "desc": sort_desc}]
    return p


def bars(title: str, expr: str, description: str, unit: str, legend: str, thresholds: dict,
         lo=None, hi=None, colors: dict | None = None) -> dict:
    defaults = {"unit": unit, "thresholds": thresholds, "color": {"mode": "thresholds"}, "mappings": []}
    if lo is not None:
        defaults["min"] = lo
    if hi is not None:
        defaults["max"] = hi
    overrides = [{"matcher": {"id": "byName", "options": name},
                  "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}
                 for name, color in (colors or {}).items()]
    return {"type": "bargauge", "title": title, "description": description,
            "targets": [_target(expr, legend, table=False) | {"range": False, "instant": True}],
            "fieldConfig": {"defaults": defaults, "overrides": overrides},
            "options": {"displayMode": "basic", "orientation": "horizontal", "valueMode": "color",
                        "namePlacement": "auto", "showUnfilled": True, "sizing": "auto",
                        "minVizHeight": 16, "minVizWidth": 8, "maxVizHeight": 300,
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}


def dashboard() -> dict:
    """The dashboard, as Grafana's JSON model (a dict)."""
    L = _Layout()
    L.row("Sites and collectors")
    L.add(stat("Since the hub last pulled each site", f"time() - nomad_site_last_pull_timestamp_seconds{{{SITE}}}",
               "How old each site's data on the hub is. The hub pulls every few minutes; much longer means "
               "the hub can't reach the site, or the site stopped.", "s",
               _steps("green", (900, "orange"), (3600, "red")), "{{site}}"), x=0, w=8, h=5)
    L.add(stat("Readings left out as too old", f"sum by (site) (nomad_readings_too_old{{{SITE}}})",
               "Readings nomad left out because their next one was due and didn't come: what stopped shows "
               "as a gap, not as old figures. The collectors' table says which.", "short",
               _steps("green", (1, "orange")), "{{site}}"), x=8, w=8, h=5)
    L.add(table("Collectors: since their last success",
                f"time() - nomad_collector_last_success_timestamp_seconds{{{SITE}}}",
                "Each collector at each site, and how long since it last ran without an error.",
                "since last success", "s", _steps("green", (3600, "orange"), (86400, "red"))), x=16, w=8, h=10)
    L.add(table("Collectors whose last run failed",
                f"(nomad_collector_last_run_timestamp_seconds{{{SITE}}} - "
                f"nomad_collector_last_success_timestamp_seconds{{{SITE}}}) > 0",
                "A collector that ran after its last success, so its latest run failed; the number is how "
                "long it has been failing.", "failing for", "s", _steps("orange", (3600, "red"))),
          x=0, w=16, h=5, y=L.y + 5)
    L.down(10)

    L.row("Nodes")
    L.add(bars("Nodes by condition", f"sum by (condition) (nomad_node_condition{{{SITE}}})",
               "Nodes up, draining (no new jobs, its jobs finishing), drained (out of service) and down "
               "(or not responding), as Slurm says.", "short", "{{condition}}", _steps("blue"), lo=0,
               colors={"up": "green", "draining": "yellow", "drained": "orange", "down": "red"}),
          x=0, w=6, h=8)
    L.add(series("Cores allocated, of the cores in service",
                 f"sum by (site) (nomad_node_cpus_allocated{{{SITE}}}) / sum by (site) (nomad_node_cpus{{{SITE}}} "
                 f'* on (site, node) (nomad_node_condition{{{SITE}, condition=~"up|draining"}} == 1))',
                 "The share of the cores on nodes in service that Slurm has allocated to jobs.",
                 "percentunit", "{{site}}", lo=0, hi=1), x=6, w=10, h=8)
    L.add(table("Nodes out of service",
                f'nomad_node_condition{{{SITE}, condition=~"draining|drained|down"}} == 1',
                "Each node not in service now, and how.", None), x=16, w=8, h=8)
    L.down(8)

    L.row("CPU clock and power")
    L.add(stat("CPUs throttled", f"sum(nomad_node_cpu_throttled{{{SITE}}})",
               "Nodes whose CPU clock has read low on reading after reading: braked (a power supply, heat), "
               "every job there runs slower.", "short", _steps("green", (1, "red")), no_value="not read"),
          x=0, w=4, h=6)
    L.add(stat("Power problems", f"sum(nomad_node_power_problems{{{SITE}}})",
               "Problems the nodes' boards report now: power supplies, voltages, fans.", "short",
               _steps("green", (1, "red")), no_value="not read"), x=4, w=4, h=6)
    L.add(stat("Boards not watched", f"count(nomad_node_power_watched{{{SITE}}} == 0) or vector(0)",
               "Nodes whose board can't be read from the system (no /dev/ipmi0): their power supplies "
               "aren't seen. Not a problem found; a place nothing looks.", "short",
               _steps("green", (1, "orange"))), x=8, w=4, h=6)
    L.add(series("CPU clock, of what it should give", f"nomad_node_cpu_clock_ratio{{{SITE}}}",
                 "Each node's CPU clock when busy, as a share of what it should give (its own usual, nodes "
                 "like it, its top clock). Far below the line (half): braked.", "percentunit",
                 "{{site}} {{node}}", lo=0, thresholds=_steps("red", (0.5, "green")), line_at=True),
          x=12, w=12, h=6)
    L.down(6)
    L.add(table("Power problems by node", f"nomad_node_power_problems{{{SITE}}} > 0",
                "Each node with a problem its board reports, by part.", "problems", "short",
                _steps("red")), x=0, w=12, h=6)
    L.add(table("Boards not watched", f"nomad_node_power_watched{{{SITE}}} == 0",
                "Nodes whose board can't be read from the system: their supplies aren't seen.", None),
          x=12, w=12, h=6)
    L.down(6)

    L.row("GPUs")
    L.add(series("GPUs busy (DCGM)", f"avg by (site, node) (nomad_gpu_busy_ratio{{{SITE}}})",
                 "How busy each node's GPUs were, as DCGM measures it (nvidia-smi's figure is not used; a node "
                 "without DCGM has no line).", "percentunit", "{{site}} {{node}}", lo=0, hi=1), x=0, w=12, h=8)
    L.add(series("GPU memory in use",
                 f"sum by (site) (nomad_gpu_memory_used_bytes{{{SITE}}}) / "
                 f"sum by (site) (nomad_gpu_memory_total_bytes{{{SITE}}})",
                 "The share of the site's GPU memory in use.", "percentunit", "{{site}}", lo=0, hi=1),
          x=12, w=6, h=8)
    L.add(series("Hottest GPU", f"max by (site, node) (nomad_gpu_temperature_celsius{{{SITE}}})",
                 "Each node's hottest GPU.", "celsius", "{{site}} {{node}}",
                 thresholds=_steps("green", (85, "red")), line_at=True), x=18, w=6, h=8)
    L.down(8)

    L.row("Queues")
    L.add(series("Jobs waiting", f'sum by (site, partition) (nomad_partition_jobs{{{SITE}, state="pending"}})',
                 "Jobs waiting in each partition.", "short", "{{site}} {{partition}}", lo=0), x=0, w=8, h=8)
    L.add(series("Jobs running", f'sum by (site, partition) (nomad_partition_jobs{{{SITE}, state="running"}})',
                 "Jobs running in each partition.", "short", "{{site}} {{partition}}", lo=0), x=8, w=8, h=8)
    L.add(series("Median wait of the jobs that started lately",
                 f"nomad_partition_wait_median_seconds{{{SITE}}}",
                 "The median wait of the jobs that started in each partition in the last [metrics] wait_hours "
                 "(24 by default).", "s", "{{site}} {{partition}}", lo=0), x=16, w=8, h=8)
    L.down(8)

    L.row("Filesystems")
    L.add(bars("Filesystems, share used",
               f"nomad_filesystem_used_bytes{{{SITE}}} / nomad_filesystem_size_bytes{{{SITE}}}",
               "Each filesystem nomad watches, and how full it is.", "percentunit", "{{site}} {{path}}",
               _steps("green", (0.8, "orange"), (0.9, "red")), lo=0, hi=1), x=0, w=24, h=8)
    L.down(8)

    return {
        "uid": UID,
        "title": "NØMAÐ",
        "description": "What nomad publishes (nomad metrics): sites and collectors, nodes, CPU clock and power, "
                       "GPUs, queues, filesystems. Nothing about one person.",
        "tags": ["nomad", "hpc", "slurm"],
        "editable": True,
        "graphTooltip": 1,
        "schemaVersion": 39,
        "version": 1,
        "timezone": "browser",
        "time": {"from": "now-24h", "to": "now"},
        "refresh": "1m",
        "links": [{"title": "nomad metrics", "type": "link", "url": "https://github.com/jtonini/nomad-hpc",
                   "targetBlank": True, "icon": "doc", "tooltip": "", "asDropdown": False, "includeVars": False,
                   "keepTime": False, "tags": []}],
        "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                                  "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                                  "name": "Annotations & Alerts", "type": "dashboard"}]},
        "templating": {"list": [
            {"type": "datasource", "name": "datasource", "label": "Prometheus", "query": "prometheus",
             "current": {}, "hide": 0, "includeAll": False, "multi": False, "options": [], "refresh": 1,
             "regex": "", "skipUrlSync": False},
            {"type": "query", "name": "site", "label": "Site", "datasource": DS,
             "definition": "label_values(nomad_collector_last_run_timestamp_seconds, site)",
             "query": "label_values(nomad_collector_last_run_timestamp_seconds, site)",
             "current": {}, "hide": 0, "includeAll": True, "allValue": ".*", "multi": True, "options": [],
             "refresh": 2, "regex": "", "sort": 1, "skipUrlSync": False},
        ]},
        "panels": L.panels,
    }


def exprs(d: dict | None = None) -> list[str]:
    """Every query in the dashboard (its panels' and its variables')."""
    d = d or dashboard()
    out = [t["expr"] for p in d["panels"] for t in p.get("targets", [])]
    return out
