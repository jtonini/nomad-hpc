# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a Slurm node state means, and what node diagnosis says about it."""

from click.testing import CliRunner

from nomad.cli import cli
from nomad.collectors.node_state import NodeState, NodeStateCollector, node_is_available
from nomad.diag.node import generate_recommendations


def _node(state):
    return NodeState(node_name="n1", state=state, cpus_total=32, cpus_alloc=0, cpu_load=0.0,
                     memory_total_mb=1000, memory_alloc_mb=0, memory_free_mb=1000,
                     partitions="basic", reason=None, features=None, gres=None)


def test_available_states():
    for s in ("IDLE", "MIXED", "ALLOCATED", "IDLE+CLOUD+POWERED_DOWN", "IDLE~",
              "MIXED+COMPLETING", "IDLE+RESERVED", "ALLOCATED+PLANNED"):
        assert node_is_available(s), s


def test_unavailable_states():
    for s in ("DOWN", "DOWN+NOT_RESPONDING", "IDLE*", "DOWN*+DRAIN", "IDLE+DRAIN",
              "MIXED+DRAIN", "DRAINING", "FAIL", "IDLE+FAIL", "UNKNOWN", "", None):
        assert not node_is_available(s), s


def test_is_healthy_reads_tokens_not_substrings():
    assert _node("IDLE+CLOUD+POWERED_DOWN").is_healthy      # was unhealthy: "DOWN" substring
    assert not _node("IDLE*").is_healthy                    # was healthy: not responding
    assert not _node("MIXED+NOT_RESPONDING").is_healthy     # was healthy
    assert not _node("MIXED+DRAIN").is_healthy


def test_not_responding_mark_is_critical():
    c = NodeStateCollector({}, ":memory:")
    assert c._classify_state("IDLE*") == "critical"
    assert c._classify_state("MIXED+DRAIN") == "warning"


def test_a_drained_node_is_not_called_healthy():
    drained = {"state": "IDLE+DRAIN", "reason": "bad DIMM"}
    recs = generate_recommendations(
        [{"cause": "Admin Drain", "confidence": "medium", "detail": "Node was drained: bad DIMM"}],
        drained, {})
    assert not any("appears healthy" in r for r in recs)
    assert any("scontrol show node" in r for r in recs)
    assert recs[-1].startswith("Resume node")


def test_no_cause_found_on_an_unavailable_node():
    recs = generate_recommendations(
        [{"cause": "No obvious issues detected", "confidence": "low", "detail": ""}],
        {"state": "DOWN+NOT_RESPONDING"}, {})
    assert len(recs) == 1 and "no cause" in recs[0]


def test_healthy_node_and_no_resume_for_a_working_node():
    assert generate_recommendations(
        [{"cause": "No obvious issues detected", "confidence": "low", "detail": ""}],
        {"state": "MIXED"}, {}) == ["Node appears healthy - no action required"]
    recs = generate_recommendations(
        [{"cause": "Memory Pressure", "confidence": "medium", "detail": ""}], {"state": "MIXED"}, {})
    assert recs and not any(r.startswith("Resume") for r in recs)


def test_nomad_version_is_the_installed_version():
    from importlib.metadata import version
    out = CliRunner().invoke(cli, ["version"]).output
    assert f"v{version('nomad-hpc')}" in out and "v0.2.0" not in out
