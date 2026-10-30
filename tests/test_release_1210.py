# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.21: the policy watch for GPU workstations. GPU compute processes
(nvidia-smi, unprivileged) placed by owner and by whether each is served to
the network: listed (gpu), or a warning (gpu_served) when it listens on a
port open to the network or its account also runs a tunnel/relay/forward --
the shape of reselling or an open inference server. Graphics (Xorg, a
browser) are not compute apps, so they don't show; nothing where there is no
GPU; never kills."""
import os
import tempfile

import pytest

from nomad.collectors import watch_probe as probe
from nomad.collectors.policy_watch import (PolicyWatchCollector, SEVERITY, LABELS,
                                           config_problems, findings_of, gpu_note)


# ── a fake /proc ────────────────────────────────────────────────────────────

def mkpid(proc, pid, uid, ppid, comm, cmdline, tty="0", listen_port=None):
    d = os.path.join(proc, pid)
    os.makedirs(os.path.join(d, "fd"), exist_ok=True)
    with open(os.path.join(d, "comm"), "w") as f:
        f.write(comm + "\n")
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write(b"\0".join(c.encode() for c in cmdline) + b"\0")
    with open(os.path.join(d, "status"), "w") as f:
        f.write(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nPPid:\t{ppid}\n")
    with open(os.path.join(d, "stat"), "w") as f:
        f.write(f"{pid} ({comm}) S {ppid} {pid} {pid} {tty} -1 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
    if listen_port is not None:
        inode = str(500000 + int(pid))
        os.symlink(f"socket:[{inode}]", os.path.join(d, "fd", "3"))
        netdir = os.path.join(proc, "net")
        os.makedirs(netdir, exist_ok=True)
        row = (f"   0: 00000000:{listen_port:04X} 00000000:0000 0A 00000000:00000000 "
               f"00:00000000 00000000  {uid} 0 {inode} 1 0000\n")
        with open(os.path.join(netdir, "tcp"), "a") as f:
            if os.path.getsize(os.path.join(netdir, "tcp")) == 0:
                f.write("  sl  local_address rem_address st ...\n")
            f.write(row)


@pytest.fixture
def proc():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "net"), exist_ok=True)
    open(os.path.join(d, "net", "tcp"), "w").close()
    open(os.path.join(d, "net", "tcp6"), "w").close()
    # pid 1 is init/systemd, for the detached check
    os.makedirs(os.path.join(d, "1"), exist_ok=True)
    with open(os.path.join(d, "1", "status"), "w") as f:
        f.write("Name:\tsystemd\nUid:\t0\t0\t0\t0\nPPid:\t0\n")
    return d


def gpu(monkeypatch, per_pid, gpus=((0, 90, 20000, 24000),)):
    monkeypatch.setattr(probe, "gpu_compute", lambda: (dict(per_pid), list(gpus)))


# ── the probe: gpu / gpu_served ─────────────────────────────────────────────

def test_a_plain_gpu_process_is_listed(proc, monkeypatch):
    mkpid(proc, "100", 1000, "1", "python3", ["python3", "train.py"])   # detached (ppid 1)
    gpu(monkeypatch, {"100": 8000})
    rows = probe.gpu_findings(proc, 1000)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "gpu" and r["program"] == "python3" and r["detail"] == ""
    assert r["gpu"]["mem_mb"] == 8000 and r["gpu"]["detached"] is True and r["gpu"]["served"] is False


def test_a_gpu_that_listens_on_the_network_is_served(proc, monkeypatch):
    mkpid(proc, "200", 1001, "900", "ollama", ["ollama", "serve"], listen_port=11434)
    mkpid(proc, "900", 1001, "1", "bash", ["bash"], tty="34816")        # a login shell: not detached
    gpu(monkeypatch, {"200": 12000})
    rows = probe.gpu_findings(proc, 1000)
    r = next(r for r in rows if r["program"] == "ollama")
    assert r["kind"] == "gpu_served" and r["detail"] == "11434"
    assert r["gpu"]["served"] is True and r["gpu"]["agent"] == "ollama" and r["gpu"]["detached"] is False
    assert r["gpu"]["via"] == "listen" and r["gpu"]["ephemeral"] is False     # 11434 is a fixed port


def test_served_through_the_account_when_the_listener_cant_be_read(proc, monkeypatch):
    # No listener on the GPU process itself, but its account runs a tunnel: served.
    mkpid(proc, "300", 1002, "1", "python3", ["python3", "infer.py"])
    gpu(monkeypatch, {"300": 9000})
    plain = probe.gpu_findings(proc, 1000)
    assert plain[0]["kind"] == "gpu"
    served = probe.gpu_findings(proc, 1000, exposure_uids={1002})
    assert served[0]["kind"] == "gpu_served" and served[0]["gpu"]["via"] == "account"


def test_a_small_context_is_not_a_workload(proc, monkeypatch):
    mkpid(proc, "400", 1000, "1", "python3", ["python3", "probe.py"])
    gpu(monkeypatch, {"400": 50})                                       # 50 MiB < 256
    assert probe.gpu_findings(proc, 1000) == []


def test_min_mem_is_configurable(proc, monkeypatch):
    mkpid(proc, "401", 1000, "1", "python3", ["python3"])
    gpu(monkeypatch, {"401": 300})
    assert probe.gpu_findings(proc, 1000, min_mem=256) != []
    assert probe.gpu_findings(proc, 1000, min_mem=512) == []


def test_no_gpu_no_findings(proc, monkeypatch):
    mkpid(proc, "500", 1000, "1", "python3", ["python3"])
    gpu(monkeypatch, {})                                                # nvidia-smi says nothing
    assert probe.gpu_findings(proc, 1000) == []


def test_ignore_programs_leaves_a_gpu_process_out(proc, monkeypatch):
    mkpid(proc, "600", 1001, "900", "ollama", ["ollama", "serve"], listen_port=11434)
    mkpid(proc, "900", 1001, "1", "bash", ["bash"])
    gpu(monkeypatch, {"600": 12000})
    assert probe.gpu_findings(proc, 1000, ignore_programs={"ollama"}) == []


def test_system_accounts_below_min_uid_are_not_counted(proc, monkeypatch):
    mkpid(proc, "700", 0, "1", "nvidia-cuda-mps", ["nvidia-cuda-mps-control"])   # a root helper
    gpu(monkeypatch, {"700": 1000})
    assert probe.gpu_findings(proc, 1000) == []


def test_nvidia_smi_absent_is_empty(monkeypatch):
    monkeypatch.setattr(probe, "NVIDIA", ("/nonexistent/nvidia-smi",))
    assert probe._nvidia_smi("--query-gpu=count") == []
    assert probe.gpu_compute() == ({}, [])


# ── the collector: notes, severities, allowlists ────────────────────────────

def test_gpu_kinds_have_severities_and_labels():
    assert SEVERITY["gpu"] == "info" and SEVERITY["gpu_served"] == "warning"
    assert "GPU" in LABELS["gpu_served"]


def test_gpu_note_reads_the_facts():
    n = gpu_note({"mem_mb": 12000, "served": True, "agent": "ollama", "detached": False})
    assert "GB on the GPU" in n and "reachable from the network" in n and "ollama" in n
    assert gpu_note({"mem_mb": 300, "detached": True}) == "~300 MB on the GPU; detached, no terminal"
    assert gpu_note({}) == "" and gpu_note(None) == ""


def _probe_answer(kind, program, detail, g):
    return {"findings": [{"uid": 1001, "user": "bob", "kind": kind, "program": program,
                          "detail": detail, "aka": [program], "processes": 1, "gpu": g}],
            "listeners": [], "root": []}


def test_findings_of_places_gpu_served_as_a_warning():
    probe_ans = _probe_answer("gpu_served", "ollama", "11434",
                              {"mem_mb": 12000, "served": True, "agent": "ollama", "detached": False})
    rows = findings_of(probe_ans, "ml-ws01", "workstation")
    assert len(rows) == 1 and rows[0]["kind"] == "gpu_served" and rows[0]["severity"] == "warning"
    assert "reachable from the network" in rows[0]["note"]


def test_a_serve_host_downgrades_served_to_listed():
    probe_ans = _probe_answer("gpu_served", "ollama", "11434", {"mem_mb": 12000, "served": True})
    rows = findings_of(probe_ans, "infer01", "workstation", gpu_serve_hosts=["infer01"])
    assert rows[0]["kind"] == "gpu" and rows[0]["severity"] == "info"
    assert "serving allowed here" in rows[0]["note"]


def test_distributed_training_on_a_compute_node_is_listed_not_warned():
    # A GPU job listening on an ephemeral rendezvous port on a compute node is
    # not reselling; it is listed, as a plain listener there would be.
    g = {"mem_mb": 20000, "served": True, "via": "listen", "ephemeral": True}
    rows = findings_of(_probe_answer("gpu_served", "python3", "46001", g), "node07", "node")
    assert rows[0]["kind"] == "gpu" and rows[0]["severity"] == "info"
    # On a workstation the same thing warns -- that's the box the signal is for.
    ws = findings_of(_probe_answer("gpu_served", "python3", "46001", g), "ml-ws01", "workstation")
    assert ws[0]["kind"] == "gpu_served" and ws[0]["severity"] == "warning"


def test_a_fixed_port_on_a_node_warns_only_when_node_listeners_are_on():
    g = {"mem_mb": 20000, "served": True, "via": "listen", "ephemeral": False}
    off = findings_of(_probe_answer("gpu_served", "vllm", "8000", g), "node07", "node")
    assert off[0]["kind"] == "gpu"                                   # node listeners off: listed
    on = findings_of(_probe_answer("gpu_served", "vllm", "8000", g), "node07", "node",
                     listeners_on_nodes=True)
    assert on[0]["kind"] == "gpu_served" and on[0]["severity"] == "warning"


def test_a_gpu_served_through_a_tunnel_warns_even_on_a_compute_node():
    g = {"mem_mb": 20000, "served": True, "via": "account", "ephemeral": False}
    rows = findings_of(_probe_answer("gpu_served", "python3", "", g), "node07", "node")
    assert rows[0]["kind"] == "gpu_served" and rows[0]["severity"] == "warning"


def test_config_validates_the_gpu_keys():
    ok, _ = config_problems({"enabled": True, "workstations": ["ml-ws01"], "watch_gpu": True,
                             "gpu_min_mem": 512, "gpu_serve_hosts": ["infer01"]})
    assert ok == []
    bad, _ = config_problems({"gpu_min_mem": "lots", "watch_gpu": "yes"})
    assert any("gpu_min_mem" in p for p in bad) and any("watch_gpu" in p for p in bad)


def test_the_probe_args_carry_the_gpu_flag():
    c = PolicyWatchCollector({"enabled": True, "gpu_min_mem": 512}, ":memory:")
    args = c._args()
    assert "--gpu" in args and args[args.index("--gpu-min-mem") + 1] == "512"
    off = PolicyWatchCollector({"enabled": True, "watch_gpu": False}, ":memory:")
    assert "--gpu" not in off._args()
    default = PolicyWatchCollector({"enabled": True}, ":memory:")
    assert "--gpu" in default._args() and "--gpu-min-mem" not in default._args()   # 256 is the default


def test_workstations_are_configured_and_validated():
    c = PolicyWatchCollector({"enabled": True, "workstations": ["ml-ws01", "ml-ws02", "bad host!"]}, ":memory:")
    assert c.workstations == ["ml-ws01", "ml-ws02"]                     # the invalid name is dropped
