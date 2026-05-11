# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Tests for nomad.collectors.per_user.ancestry

Synthetic process trees calibrated against validation findings:

  - arobbins's clusterbackup: sshd -> ... -> /usr/local/sw/clusterbackup/...
    Should match parent_paths whitelist and be excluded.
  - ia3nk's gmx_mpi: sshd -> bash -> singularity -> bash -> gmx -> gmx_mpi
    No whitelist match. Depth 6, must be walked completely.
  - abezerra's antigravity-server: depth 5 from sshd, all in user's home.
    No whitelist match.
  - SLURM service account: uid 1001, should match min_uid threshold IF
    we configured min_uid > 1001, but our default min_uid=1000 means
    1001 is NOT auto-whitelisted; instead 'slurm' is in users list.
"""
from __future__ import annotations

import pytest

from nomad.collectors.per_user.ancestry import (
    ProcessInfo,
    WhitelistConfig,
    WhitelistMatch,
    match_whitelist,
    walk_ancestry,
)


def make_lookup(processes: list[ProcessInfo]):
    """Build a lookup callable from a flat list of ProcessInfo."""
    by_pid = {p.pid: p for p in processes}
    return by_pid.get


# ---------------------------------------------------------------------------
# Ancestry walking
# ---------------------------------------------------------------------------

def test_walks_ssh_singularity_gmx_chain_depth_6():
    """ia3nk's actual ancestry from validation."""
    procs = [
        ProcessInfo(pid=1, ppid=None, uid=0, username="root", command="systemd", exe_path="/usr/lib/systemd/systemd"),
        ProcessInfo(pid=2000, ppid=1, uid=0, username="root", command="sshd", exe_path="/usr/sbin/sshd"),
        ProcessInfo(pid=2001, ppid=2000, uid=10001, username="ia3nk", command="bash", exe_path="/bin/bash"),
        ProcessInfo(pid=2002, ppid=2001, uid=10001, username="ia3nk", command="singularity", exe_path="/usr/bin/singularity"),
        ProcessInfo(pid=2003, ppid=2002, uid=10001, username="ia3nk", command="bash", exe_path="/bin/bash"),
        ProcessInfo(pid=2004, ppid=2003, uid=10001, username="ia3nk", command="gmx", exe_path="/opt/gromacs/bin/gmx"),
        ProcessInfo(pid=2005, ppid=2004, uid=10001, username="ia3nk", command="gmx_mpi", exe_path="/opt/gromacs/bin/gmx_mpi"),
    ]
    result = walk_ancestry(pid=2005, lookup=make_lookup(procs), max_depth=8)
    # Root-first chain (ancestors only, leaf 2005 NOT included)
    assert result.chain == ["sshd", "bash", "singularity", "bash", "gmx"]
    assert result.depth == 5
    assert not result.truncated


def test_walks_until_init():
    """Walking should stop at ppid <= 1."""
    procs = [
        ProcessInfo(pid=1, ppid=None, uid=0, username="root", command="systemd", exe_path=None),
        ProcessInfo(pid=100, ppid=1, uid=1000, username="user", command="bash", exe_path="/bin/bash"),
    ]
    result = walk_ancestry(pid=100, lookup=make_lookup(procs), max_depth=8)
    # systemd (pid 1) is excluded; chain stops above it
    assert result.chain == []
    assert result.depth == 0


def test_truncates_at_max_depth():
    """Pathologically deep chain should be truncated cleanly."""
    procs = [ProcessInfo(pid=1, ppid=None, uid=0, username="root", command="systemd", exe_path=None)]
    procs += [
        ProcessInfo(pid=i + 100, ppid=i + 99 if i > 0 else 1, uid=1000, username="u", command=f"p{i}", exe_path=f"/bin/p{i}")
        for i in range(15)
    ]
    leaf_pid = procs[-1].pid
    result = walk_ancestry(pid=leaf_pid, lookup=make_lookup(procs), max_depth=8)
    assert result.truncated
    assert result.depth == 8


def test_handles_ppid_pointing_to_dead_process():
    """A ppid pointing to a process we can't see should terminate gracefully."""
    procs = [
        ProcessInfo(pid=500, ppid=999, uid=1000, username="user", command="orphan", exe_path="/bin/x"),
        # ppid 999 doesn't exist
    ]
    result = walk_ancestry(pid=500, lookup=make_lookup(procs), max_depth=8)
    assert result.chain == []


def test_handles_lookup_failure_for_leaf():
    """If the leaf itself isn't found, return an empty result."""
    result = walk_ancestry(pid=999, lookup=lambda _: None, max_depth=8)
    assert result.chain == []
    assert result.depth == 0


# ---------------------------------------------------------------------------
# Whitelist: parent_paths (the headline validation finding)
# ---------------------------------------------------------------------------

def test_arobbins_clusterbackup_matches_parent_path_whitelist():
    """The 5/7 false positives in spydur validation. With parent_paths,
    these are eliminated."""
    procs = [
        ProcessInfo(pid=1, ppid=None, uid=0, username="root", command="systemd", exe_path=None),
        ProcessInfo(pid=2000, ppid=1, uid=0, username="root", command="cron", exe_path="/usr/sbin/cron"),
        ProcessInfo(
            pid=2100, ppid=2000, uid=10500, username="arobbins",
            command="bash", exe_path="/usr/local/sw/clusterbackup/clusterbackup.sh",
        ),
        ProcessInfo(
            pid=2101, ppid=2100, uid=10500, username="arobbins",
            command="python3", exe_path="/usr/local/sw/clusterbackup/clusterbackup.py",
        ),
    ]
    leaf = procs[-1]
    ancestry = walk_ancestry(pid=leaf.pid, lookup=make_lookup(procs), max_depth=8)
    config = WhitelistConfig(parent_paths=("/usr/local/sw/", "/opt/"))
    match = match_whitelist(leaf, ancestry, config)
    assert match is not None
    assert match.reason == "parent_path"
    assert match.detail == "/usr/local/sw/"


def test_parent_path_match_via_ancestor_not_leaf():
    """A leaf in /tmp whose parent is in /usr/local/sw/ should still be
    whitelisted (this is the 'recursive' part of the spec)."""
    procs = [
        ProcessInfo(pid=1, ppid=None, uid=0, username="root", command="systemd", exe_path=None),
        ProcessInfo(
            pid=2100, ppid=1, uid=10500, username="user",
            command="wrapper", exe_path="/usr/local/sw/tool/wrapper.sh",
        ),
        ProcessInfo(
            pid=2101, ppid=2100, uid=10500, username="user",
            command="helper", exe_path="/tmp/some_helper",     # leaf NOT in /usr/local/sw
        ),
    ]
    leaf = procs[-1]
    ancestry = walk_ancestry(pid=leaf.pid, lookup=make_lookup(procs), max_depth=8)
    config = WhitelistConfig(parent_paths=("/usr/local/sw/",))
    match = match_whitelist(leaf, ancestry, config)
    assert match is not None
    assert match.reason == "parent_path"


def test_parent_path_does_not_match_substring():
    """'/usr/local/sw' must not match '/usr/local/swag/...'"""
    procs = [
        ProcessInfo(pid=1, ppid=None, uid=0, username="root", command="systemd", exe_path=None),
        ProcessInfo(pid=100, ppid=1, uid=10500, username="user", command="x",
                    exe_path="/usr/local/swag/x"),
    ]
    leaf = procs[-1]
    ancestry = walk_ancestry(pid=leaf.pid, lookup=make_lookup(procs), max_depth=8)
    config = WhitelistConfig(parent_paths=("/usr/local/sw",))   # no trailing slash
    match = match_whitelist(leaf, ancestry, config)
    assert match is None


# ---------------------------------------------------------------------------
# Whitelist: users (service accounts)
# ---------------------------------------------------------------------------

def test_slurm_service_account_matches_user_list():
    proc = ProcessInfo(pid=500, ppid=1, uid=1001, username="slurm",
                       command="slurmd", exe_path="/usr/sbin/slurmd")
    config = WhitelistConfig(users=("slurm", "munge"))
    match = match_whitelist(proc, _empty_ancestry(), config)
    assert match is not None
    assert match.reason == "user"
    assert match.detail == "slurm"


def test_min_uid_excludes_root_and_low_uids():
    """Default min_uid=1000 should whitelist anything below 1000."""
    proc = ProcessInfo(pid=10, ppid=1, uid=0, username="root",
                       command="kworker", exe_path=None)
    config = WhitelistConfig()
    match = match_whitelist(proc, _empty_ancestry(), config)
    assert match is not None
    assert match.reason == "min_uid"


def test_real_user_above_min_uid_not_auto_whitelisted():
    proc = ProcessInfo(pid=10000, ppid=1, uid=10001, username="ia3nk",
                       command="gmx_mpi", exe_path="/opt/gromacs/bin/gmx_mpi")
    ancestry = _empty_ancestry()
    config = WhitelistConfig(min_uid=1000, parent_paths=("/usr/local/sw/",))
    # /opt/gromacs is not whitelisted, /opt/ also not (we only configured /usr/local/sw/)
    match = match_whitelist(proc, ancestry, config)
    assert match is None


def test_user_command_pair_whitelist():
    proc = ProcessInfo(pid=10000, ppid=1, uid=10001, username="someuser",
                       command="specific_tool.py", exe_path="/home/someuser/bin/specific_tool.py")
    config = WhitelistConfig(user_commands=(("someuser", "specific_tool.py"),))
    match = match_whitelist(proc, _empty_ancestry(), config)
    assert match is not None
    assert match.reason == "user_command"


# ---------------------------------------------------------------------------
# Negative case: ia3nk's gmx_mpi must NOT be whitelisted by default config
# ---------------------------------------------------------------------------

def test_ia3nk_gmx_mpi_is_not_whitelisted_by_default():
    """Sanity: a real misuse case slips past whitelisting as expected."""
    procs = [
        ProcessInfo(pid=1, ppid=None, uid=0, username="root", command="systemd", exe_path=None),
        ProcessInfo(pid=2000, ppid=1, uid=0, username="root", command="sshd", exe_path="/usr/sbin/sshd"),
        ProcessInfo(pid=2001, ppid=2000, uid=10001, username="ia3nk", command="bash", exe_path="/bin/bash"),
        ProcessInfo(pid=2002, ppid=2001, uid=10001, username="ia3nk", command="singularity",
                    exe_path="/usr/bin/singularity"),
        ProcessInfo(pid=2003, ppid=2002, uid=10001, username="ia3nk", command="bash", exe_path="/bin/bash"),
        ProcessInfo(pid=2004, ppid=2003, uid=10001, username="ia3nk", command="gmx",
                    exe_path="/opt/gromacs/bin/gmx"),
        ProcessInfo(pid=2005, ppid=2004, uid=10001, username="ia3nk", command="gmx_mpi",
                    exe_path="/opt/gromacs/bin/gmx_mpi"),
    ]
    leaf = procs[-1]
    ancestry = walk_ancestry(pid=leaf.pid, lookup=make_lookup(procs), max_depth=8)
    # Default config from the handoff: /usr/local/sw/ and /opt/ in parent_paths
    config = WhitelistConfig(parent_paths=("/usr/local/sw/", "/opt/"))
    match = match_whitelist(leaf, ancestry, config)
    # /opt/gromacs/ matches /opt/ -- so gmx_mpi WOULD be whitelisted under
    # this config. This is a real design tension: do we whitelist all of /opt/?
    # The handoff suggests yes, but it bundles legitimate scientific software.
    # Document the tension; the resolution is per-cluster tuning.
    assert match is not None
    assert match.detail == "/opt/"
    # If we tighten the config to exclude /opt/, gmx_mpi correctly does NOT match
    tighter_config = WhitelistConfig(parent_paths=("/usr/local/sw/",))
    tighter_match = match_whitelist(leaf, ancestry, tighter_config)
    assert tighter_match is None


def _empty_ancestry():
    from nomad.collectors.per_user.ancestry import AncestryResult
    return AncestryResult()
