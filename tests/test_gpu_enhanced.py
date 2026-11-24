# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Tests for enhanced GPU collector (Idea 13):
  - Real Utilization computation
  - Workload classification
  - Health status determination
  - DCGM enrichment path
  - Schema migration (new columns tolerated on existing DB)
  - SLURM/CUDA GPU visibility filter
  - nvidia-smi fallback sets None for DCGM fields
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from nomad.collectors.gpu import (
    GPUCollector,
    DCGMStats,
    _classify_workload,
    _compute_real_util,
    _determine_health_status,
    REAL_UTIL_PRESETS,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NVIDIA_SMI_OUTPUT = """\
0, NVIDIA A100-SXM4-40GB, 85, 60, 24000, 40000, 16000, 72, 280.5, 400.0
1, NVIDIA A100-SXM4-40GB, 5,  2,  1000, 40000, 39000, 45,  50.0, 400.0
2, NVIDIA RTX 6000 Ada Generation, 0, 0, 0, 49140, 49140, 35, 15.0, 300.0
"""


@pytest.fixture
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        path = f.name
    yield path
    os.unlink(path)


@pytest.fixture
def collector(tmp_db):
    return GPUCollector({'enabled': True}, tmp_db)


@pytest.fixture
def ssh_collector(tmp_db):
    return GPUCollector(
        {'enabled': True, 'gpu_nodes': ['node51', 'node52'], 'ssh_user': 'monitor'},
        tmp_db,
    )


# ---------------------------------------------------------------------------
# Real Utilization
# ---------------------------------------------------------------------------

class TestRealUtil:
    def test_ai_preset_values(self):
        weights = REAL_UTIL_PRESETS['ai']
        result = _compute_real_util(80, 70, 50, 60, weights)
        # Expected: 0.35*80 + 0.35*70 + 0.20*50 + 0.10*60 = 28+24.5+10+6 = 68.5
        assert abs(result - 68.5) < 0.1

    def test_hpc_preset_values(self):
        weights = REAL_UTIL_PRESETS['hpc']
        result = _compute_real_util(80, 10, 60, 40, weights)
        # 0.45*80 + 0.15*10 + 0.25*60 + 0.15*40 = 36+1.5+15+6 = 58.5
        assert abs(result - 58.5) < 0.1

    def test_clamps_at_100(self):
        weights = REAL_UTIL_PRESETS['ai']
        result = _compute_real_util(100, 100, 100, 100, weights)
        assert result == 100.0

    def test_clamps_at_0(self):
        weights = REAL_UTIL_PRESETS['ai']
        result = _compute_real_util(0, 0, 0, 0, weights)
        assert result == 0.0

    def test_weights_auto_normalized(self):
        """Un-normalized weights give same result as normalized."""
        raw = [70, 70, 40, 20]  # sum = 200
        norm = [0.35, 0.35, 0.20, 0.10]  # sum = 1.0
        r1 = _compute_real_util(60, 50, 40, 30, raw)
        r2 = _compute_real_util(60, 50, 40, 30, norm)
        assert abs(r1 - r2) < 0.01

    def test_custom_weights_in_collector_config(self, tmp_db):
        c = GPUCollector({'real_util_weights': [0.4, 0.3, 0.2, 0.1]}, tmp_db)
        assert c._real_util_weights == [0.4, 0.3, 0.2, 0.1]

    def test_preset_name_resolves(self, tmp_db):
        c = GPUCollector({'real_util_weights': 'hpc'}, tmp_db)
        assert c._real_util_weights == REAL_UTIL_PRESETS['hpc']

    def test_unknown_preset_falls_back_to_ai(self, tmp_db):
        c = GPUCollector({'real_util_weights': 'nonexistent'}, tmp_db)
        assert c._real_util_weights == REAL_UTIL_PRESETS['ai']


# ---------------------------------------------------------------------------
# Workload classification
# ---------------------------------------------------------------------------

class TestWorkloadClassification:
    def test_idle(self):
        assert _classify_workload(2, 1, 2, 1, 0, 2) == "idle"

    def test_tensor_heavy(self):
        assert _classify_workload(sm=80, tensor=60, dram=30, gr=70, fp64=0, real_util=75) == "tensor-heavy compute"

    def test_tensor_moderate(self):
        assert _classify_workload(sm=55, tensor=20, dram=20, gr=40, fp64=0, real_util=40) == "tensor compute"

    def test_fp64_hpc(self):
        assert _classify_workload(sm=70, tensor=5, dram=20, gr=50, fp64=30, real_util=50) == "FP64 / HPC compute"

    def test_io_loading(self):
        # tensor < 15, sm < 30, but memcpy active and PCIe high
        assert _classify_workload(sm=15, tensor=5, dram=20, gr=20, fp64=0, real_util=20,
                                   memcpy_active=True, pcie_gbps=2.0) == "I/O or data-loading"

    def test_memory_bound(self):
        assert _classify_workload(sm=30, tensor=5, dram=60, gr=30, fp64=0, real_util=40) == "memory-bound"

    def test_compute_heavy(self):
        assert _classify_workload(sm=90, tensor=5, dram=30, gr=70, fp64=5, real_util=70) == "compute-heavy"

    def test_compute_active(self):
        assert _classify_workload(sm=60, tensor=5, dram=20, gr=40, fp64=5, real_util=45) == "compute-active"

    def test_memory_active(self):
        assert _classify_workload(sm=30, tensor=5, dram=45, gr=30, fp64=0, real_util=30) == "memory-active"

    def test_busy_low_sm(self):
        assert _classify_workload(sm=20, tensor=5, dram=20, gr=50, fp64=0, real_util=25) == "busy, low SM use"

    def test_low_utilization(self):
        assert _classify_workload(sm=10, tensor=2, dram=8, gr=10, fp64=0, real_util=8) == "low utilization"

    def test_mixed_fallthrough(self):
        # Nothing matches a specific category
        assert _classify_workload(sm=25, tensor=10, dram=25, gr=20, fp64=5, real_util=22) == "mixed / moderate"


# ---------------------------------------------------------------------------
# Health status
# ---------------------------------------------------------------------------

class TestHealthStatus:
    def test_ok(self):
        assert _determine_health_status(0.0, 0, 0, 70, "A100") == "OK"

    def test_warn_pcie_replay(self):
        assert _determine_health_status(0.5, 0, 0, 70, "A100") == "WARN"

    def test_hot_temperature(self):
        assert _determine_health_status(0.0, 0, 0, 94, "A100") == "HOT"

    def test_hot_rtx_lower_threshold(self):
        assert _determine_health_status(0.0, 0, 0, 93, "RTX 6000 Ada Generation") == "HOT"

    def test_crit_remap_failure(self):
        assert _determine_health_status(0.0, 1, 0, 70, "A100") == "CRIT"

    def test_crit_ecc_uncorrectable(self):
        assert _determine_health_status(0.0, 0, 3, 70, "A100") == "CRIT"

    def test_crit_beats_hot(self):
        # Both CRIT condition and high temp — CRIT wins
        assert _determine_health_status(0.0, 1, 0, 95, "A100") == "CRIT"


# ---------------------------------------------------------------------------
# Collector: DCGM enrichment
# ---------------------------------------------------------------------------

class TestDCGMEnrichment:
    def test_enrich_adds_dcgm_fields(self, collector):
        records = [
            {'type': 'gpu', 'gpu_index': 0, 'gpu_util_percent': 85.0,
             'data_source': 'nvidia-smi', 'node_name': 'node01',
             'memory_used_mb': 20000, 'memory_total_mb': 40000}
        ]
        dcgm_data = {
            0: DCGMStats(
                gpu_index=0,
                sm_active_pct=75.0, tensor_active_pct=60.0,
                dram_active_pct=40.0, gr_engine_active_pct=70.0,
                fp64_active_pct=2.0,
            )
        }
        enriched = collector._enrich_with_dcgm(records, dcgm_data)
        r = enriched[0]
        assert r['data_source'] == 'dcgm'
        assert r['sm_active_pct'] == 75.0
        assert r['tensor_active_pct'] == 60.0
        assert 'real_util_pct' in r
        assert r['real_util_pct'] > 0
        assert r['workload_class'] == 'tensor-heavy compute'

    def test_enrich_without_dcgm_sets_none(self, collector):
        records = [
            {'type': 'gpu', 'gpu_index': 0, 'gpu_util_percent': 85.0,
             'data_source': 'nvidia-smi', 'node_name': 'node01'}
        ]
        enriched = collector._enrich_with_dcgm(records, {})
        r = enriched[0]
        assert r['sm_active_pct'] is None
        assert r['real_util_pct'] is None
        assert r['workload_class'] is None
        assert r['data_source'] == 'nvidia-smi'


# ---------------------------------------------------------------------------
# Collector: GPU visibility filter
# ---------------------------------------------------------------------------

class TestGPUVisibility:
    def test_cuda_visible_devices(self, monkeypatch):
        monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '0,2')
        monkeypatch.delenv('NVIDIA_VISIBLE_DEVICES', raising=False)
        monkeypatch.delenv('SLURM_STEP_GPUS', raising=False)
        monkeypatch.delenv('SLURM_JOB_GPUS', raising=False)
        assert GPUCollector.visible_gpu_indices() == [0, 2]

    def test_slurm_job_gpus_fallback(self, monkeypatch):
        monkeypatch.delenv('CUDA_VISIBLE_DEVICES', raising=False)
        monkeypatch.delenv('NVIDIA_VISIBLE_DEVICES', raising=False)
        monkeypatch.delenv('SLURM_STEP_GPUS', raising=False)
        monkeypatch.setenv('SLURM_JOB_GPUS', '1,3')
        assert GPUCollector.visible_gpu_indices() == [1, 3]

    def test_no_env_returns_none(self, monkeypatch):
        for var in ('CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES',
                    'SLURM_STEP_GPUS', 'SLURM_JOB_GPUS'):
            monkeypatch.delenv(var, raising=False)
        assert GPUCollector.visible_gpu_indices() is None

    def test_nodevfiles_returns_none(self, monkeypatch):
        monkeypatch.setenv('CUDA_VISIBLE_DEVICES', 'NoDevFiles')
        assert GPUCollector.visible_gpu_indices() is None


# ---------------------------------------------------------------------------
# Collector: parsing and store
# ---------------------------------------------------------------------------

class TestGPUCollectorParsing:
    def test_parse_nvidia_output(self, collector):
        records = collector._parse_nvidia_output(NVIDIA_SMI_OUTPUT, hostname='testnode')
        assert len(records) == 3
        assert all(r['type'] == 'gpu' for r in records)
        assert all(r['node_name'] == 'testnode' for r in records)
        assert all(r['data_source'] == 'nvidia-smi' for r in records)
        assert records[0]['gpu_util_percent'] == 85.0
        assert records[1]['gpu_util_percent'] == 5.0
        assert records[2]['gpu_util_percent'] == 0.0

    def test_parse_handles_na(self, collector):
        line = "0, Test GPU, [N/A], [N/A], [N/A], 40000, [N/A], [N/A], [N/A], 400.0"
        records = collector._parse_nvidia_output(line)
        assert len(records) == 1
        assert records[0]['gpu_util_percent'] == 0.0

    def test_store_creates_schema(self, collector):
        records = collector._parse_nvidia_output(NVIDIA_SMI_OUTPUT, hostname='node01')
        records = collector._enrich_with_dcgm(records, {})
        collector.store([{'type': 'gpu', **r} for r in records])

        conn = sqlite3.connect(collector.db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(gpu_stats)")}
        conn.close()

        assert 'sm_active_pct' in cols
        assert 'real_util_pct' in cols
        assert 'workload_class' in cols
        assert 'data_source' in cols

    def test_store_migration_on_existing_db(self, tmp_db):
        """Collector tolerates an existing gpu_stats without the new DCGM columns."""
        conn = sqlite3.connect(tmp_db)
        conn.execute("""
            CREATE TABLE gpu_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME, node_name TEXT,
                gpu_index INTEGER, gpu_name TEXT,
                gpu_util_percent REAL,
                memory_util_percent REAL,
                memory_used_mb INTEGER, memory_total_mb INTEGER, memory_free_mb INTEGER,
                temperature_c INTEGER, power_draw_w REAL, power_limit_w REAL,
                compute_processes INTEGER
            )
        """)
        conn.commit()
        conn.close()

        collector = GPUCollector({'enabled': True}, tmp_db)
        # Should not raise
        records = [{'type': 'gpu', 'timestamp': '2026-01-01T00:00:00',
                    'node_name': 'n1', 'gpu_index': 0, 'gpu_name': 'A100',
                    'gpu_util_percent': 50.0, 'memory_util_percent': 30.0,
                    'memory_used_mb': 10000, 'memory_total_mb': 40000,
                    'memory_free_mb': 30000, 'temperature_c': 65,
                    'power_draw_w': 200.0, 'power_limit_w': 400.0,
                    'compute_processes': 1,
                    'sm_active_pct': None, 'tensor_active_pct': None,
                    'dram_active_pct': None, 'gr_engine_active_pct': None,
                    'fp64_active_pct': None, 'real_util_pct': None,
                    'workload_class': None, 'data_source': 'nvidia-smi'}]
        collector.store(records)

        conn = sqlite3.connect(tmp_db)
        count = conn.execute("SELECT COUNT(*) FROM gpu_stats").fetchone()[0]
        conn.close()
        assert count == 1

    def test_gpu_health_table_created(self, collector, tmp_db):
        conn = sqlite3.connect(tmp_db)
        collector._ensure_schema(conn)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert 'gpu_health' in tables


# ---------------------------------------------------------------------------
# Collector: DCGM detection (mocked)
# ---------------------------------------------------------------------------

class TestDCGMParsing:
    """Test _collect_dcgm parsing with real dcgmi dmon output format."""

    # Realistic dcgmi dmon output from RTX 6000 Ada (fields 1001,1002,1004,1005)
    # Values are fractions 0.0-1.0, must be multiplied by 100
    DCGM_OUTPUT_IDLE = """\
#Entity   GRACT        SMACT        TENSO        DRAMA
ID
GPU 7     0.000        0.000        0.000        0.000
GPU 6     0.000        0.000        0.000        0.000
GPU 0     0.000        0.000        0.000        0.000
"""

    DCGM_OUTPUT_ACTIVE = """\
#Entity   GRACT        SMACT        TENSO        DRAMA
ID
GPU 2     0.984        0.930        0.000        0.252
GPU 1     1.000        0.926        0.038        0.869
GPU 0     1.000        0.916        0.023        0.881
"""

    def test_parse_gpu_n_format(self, collector, tmp_path):
        """dcgmi dmon uses 'GPU N' not 'N' — both formats must parse."""
        with patch.object(collector, '_run', return_value=self.DCGM_OUTPUT_IDLE):
            result = collector._collect_dcgm('node51')
        assert len(result) == 3
        assert 0 in result and 6 in result and 7 in result

    def test_fraction_to_percentage_scaling(self, collector):
        """DCGM returns fractions (0.0-1.0); collector must scale to 0-100."""
        with patch.object(collector, '_run', return_value=self.DCGM_OUTPUT_ACTIVE):
            result = collector._collect_dcgm('node51')
        gpu0 = result[0]
        # 1.000 * 100 = 100.0
        assert abs(gpu0.gr_engine_active_pct - 100.0) < 0.1
        assert abs(gpu0.sm_active_pct - 91.6) < 0.2
        assert abs(gpu0.tensor_active_pct - 2.3) < 0.2
        assert abs(gpu0.dram_active_pct - 88.1) < 0.2

    def test_field_order_gr_sm_tensor_dram(self, collector):
        """Field order must match dcgmi dmon -e 1001,1002,1004,1005:
        GR_ENGINE(1001), SM_ACTIVE(1002), TENSOR(1004), DRAM(1005)."""
        with patch.object(collector, '_run', return_value=self.DCGM_OUTPUT_ACTIVE):
            result = collector._collect_dcgm('node51')
        gpu2 = result[2]
        # GPU 2: 0.984, 0.930, 0.000, 0.252
        assert abs(gpu2.gr_engine_active_pct - 98.4) < 0.2
        assert abs(gpu2.sm_active_pct - 93.0) < 0.2
        assert abs(gpu2.tensor_active_pct - 0.0) < 0.1
        assert abs(gpu2.dram_active_pct - 25.2) < 0.2

    def test_header_lines_skipped(self, collector):
        """#Entity header and ID separator lines must be skipped."""
        with patch.object(collector, '_run', return_value=self.DCGM_OUTPUT_IDLE):
            result = collector._collect_dcgm('node51')
        # Only GPU data lines, no header entries
        assert all(isinstance(k, int) for k in result.keys())

    def test_idle_gpus_classify_as_idle(self, collector):
        """Idle DCGM data should produce idle workload classification."""
        with patch.object(collector, '_run', return_value=self.DCGM_OUTPUT_IDLE):
            dcgm_data = collector._collect_dcgm('node51')
        records = [{'type': 'gpu', 'gpu_index': 0, 'gpu_util_percent': 0.0,
                    'data_source': 'nvidia-smi', 'node_name': 'node51'}]
        enriched = collector._enrich_with_dcgm(records, dcgm_data)
        assert enriched[0]['workload_class'] == 'idle'
        assert enriched[0]['real_util_pct'] == 0.0

    def test_active_gpus_classify_correctly(self, collector):
        """Active DCGM data (high SM, low tensor) should classify as compute."""
        with patch.object(collector, '_run', return_value=self.DCGM_OUTPUT_ACTIVE):
            dcgm_data = collector._collect_dcgm('node51')
        records = [{'type': 'gpu', 'gpu_index': 0, 'gpu_util_percent': 100.0,
                    'data_source': 'nvidia-smi', 'node_name': 'node51'}]
        enriched = collector._enrich_with_dcgm(records, dcgm_data)
        # SM ~91.6%, tensor ~2.3% → compute-heavy or compute-active
        assert enriched[0]['workload_class'] in ('compute-heavy', 'compute-active')
        assert enriched[0]['data_source'] == 'dcgm'
        assert enriched[0]['real_util_pct'] > 50  # should be high

    def test_dcgm_field_ids_in_command(self, collector):
        """Collector must use corrected field IDs: 1001,1002,1004,1005,1007."""
        calls = []
        def capture_run(cmd, host=None):
            calls.append(cmd)
            return self.DCGM_OUTPUT_IDLE
        with patch.object(collector, '_run', side_effect=capture_run):
            collector._collect_dcgm('node51')
        assert any('1001,1002,1004,1005' in str(c) for c in calls), \
            f"Expected field IDs 1001,1002,1004,1005 in commands: {calls}"
        # Must NOT use old wrong field 1000
        assert not any(',1000,' in str(c) for c in calls), \
            "Old field ID 1000 found — should be 1001 for gr_engine"


class TestDCGMDetection:
    def test_dcgm_detected_via_which(self, collector):
        with patch.object(collector, '_run', side_effect=lambda cmd, host=None: (
            '/usr/bin/dcgmi' if 'which' in cmd else None
        )):
            result = collector._detect_dcgm()
            assert result == 'dcgmi'

    def test_dcgm_not_present_returns_none(self, collector):
        with patch.object(collector, '_run', return_value=None):
            result = collector._detect_dcgm()
            assert result is None

    def test_dcgm_not_available_falls_back(self, collector):
        """collect() works without DCGM — DCGM fields set to None."""
        collector._gpu_available = True
        with patch.object(collector, '_run', side_effect=lambda cmd, host=None: (
            NVIDIA_SMI_OUTPUT if 'query-gpu' in (cmd if isinstance(cmd, str) else ' '.join(cmd)) else None
        )):
            collector._dcgm_checked = True
            collector._dcgm_mode = None
            records = [r for r in collector.collect() if r.get('type') == 'gpu']
        assert all(r.get('real_util_pct') is None for r in records)
        assert all(r.get('data_source') == 'nvidia-smi' for r in records)
