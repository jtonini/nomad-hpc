# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.47.1: what each compute node's memory holds, from /proc/meminfo.

Slurm's FreeMem is MemFree, which counts the file cache as used: an idle
node after a long job read as 82% in use. The node_disks collector's login
to each node now also reads /proc/meminfo; total less MemAvailable is what
programs hold (node_memory), and the node timeline draws that line.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from nomad.collectors import node_disks as ND
from nomad.db.migrations import ensure_database

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from test_node_disks import answer, df, site, ssh_calls  # noqa: E402,F401

MEMINFO = ("MemTotal:       393216000 kB", "MemFree:          7000000 kB", "MemAvailable:   380000000 kB",
           "Buffers:           100000 kB", "Cached:         320000000 kB", "SReclaimable:     5000000 kB",
           "Shmem:             200000 kB")


def with_memory(*lines, meminfo=MEMINFO):
    return answer(*lines) + ND.MEMORY_MARK + "\n" + "".join(m + "\n" for m in meminfo)


def test_an_idle_node_full_of_file_cache_holds_little():
    got = ND.parse("spdr01", with_memory(df("/localscratch", 10, 990, "/")), "t")
    disk, mem = got
    assert disk["type"] == "node_disk" and mem["type"] == "node_memory"
    assert mem["total_mb"] == 384000 and mem["free_mb"] == 6836                  # MemFree: "98% used"
    assert mem["used_mb"] == 12906                                              # programs: 3%
    assert mem["cache_mb"] == 317285                                            # given back on demand


def test_a_kernel_without_memavailable_says_none_held():
    got = ND.parse("old", with_memory(meminfo=[m for m in MEMINFO if not m.startswith("MemAvailable")]), "t")
    assert got[-1]["used_mb"] is None and got[-1]["total_mb"] == 384000
    assert ND.parse("x", with_memory(meminfo=["garbage"]), "t") == []           # no MemTotal: nothing


def test_the_command_reads_meminfo_here():
    run = subprocess.run(["bash", "-c", ND.remote_command(["/"], memory=True)], capture_output=True, text=True)
    got = ND.parse("here", run.stdout, "t")
    mem = [g for g in got if g["type"] == "node_memory"]
    assert len(mem) == 1 and mem[0]["total_mb"] > 0 and mem[0]["used_mb"] is not None
    assert ND.MEMORY_MARK not in ND.remote_command(["/"])                      # memory = false: as before


def test_a_sweep_stores_each_nodes_memory(site):
    site.answers["cn01"] = SimpleNamespace(returncode=0, stderr="",
                                           stdout=with_memory(df("/localscratch", 9 * 10 ** 11, 10 ** 11, "/")))
    col = ND.NodeDisksCollector({"enabled": True, "alerts_enabled": False}, site.db)
    r = col.run()
    assert r.success and r.records_collected == 2                             # one disk, one memory
    rows = sqlite3.connect(site.db).execute("SELECT node, total_mb, used_mb, cache_mb FROM node_memory").fetchall()
    assert rows == [("cn01", 384000, 12906, 317285)]
    assert sqlite3.connect(site.db).execute(
        "SELECT asked, answered, readings FROM node_disks_sweeps").fetchall() == [(2, 1, 1)]   # readings: disks
    assert "path reading(s) missing" not in (r.note or "")


def test_memory_can_be_left_out(site):
    col = ND.NodeDisksCollector({"enabled": True, "alerts_enabled": False, "memory": False}, site.db)
    col.run()
    cmd = [c for c in ssh_calls(site)][0][-1]
    assert ND.MEMORY_MARK not in cmd


def test_the_table_comes_with_the_database(tmp_path):
    db = tmp_path / "n.db"
    ensure_database(db)
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(node_memory)")}
    assert {"node", "timestamp", "total_mb", "available_mb", "used_mb", "cache_mb"} <= cols


def test_the_timeline_draws_what_programs_hold(tmp_path):
    from nomad.facts.node import node
    db = tmp_path / "c.db"
    ensure_database(db)
    c = sqlite3.connect(db)
    now = datetime(2026, 11, 13, 14, 0)
    for i in range(6):
        t = (now - timedelta(minutes=30 * (5 - i))).isoformat(timespec="seconds")
        c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, memory_total_mb, "
                  "memory_alloc_mb, memory_free_mb, cluster) VALUES (?, 'spdr01', 'IDLE', 52, 0, 384000, 0, 69000, "
                  "'spydur')", (t,))
        c.execute("INSERT INTO node_memory (timestamp, node, total_mb, available_mb, free_mb, cache_mb, used_mb) "
                  "VALUES (?, 'spdr01', 384000, 371000, 69000, 300000, 13000)", (t,))
    c.commit()
    c.row_factory = sqlite3.Row
    got = node(c, None, "spdr01", now - timedelta(hours=3), now, now=now, staff=True)
    assert "mem_used_pct" in got["series"] and "mem_held_pct" not in got["series"]
    assert max(v for _t, v in got["series"]["mem_used_pct"] if v is not None) == pytest.approx(3.4, abs=0.1)
    assert "doesn't allocate memory" in got["series_info"]["mem_used_pct"]["note"]
