"""Tests for GPUCollector SSH mode."""
import os
import pytest
from unittest.mock import patch, MagicMock
from nomad.collectors.gpu import GPUCollector, GPU_TEMP_WARN, _get_temp_threshold


# Realistic nvidia-smi output from hpc2 node51 (8x RTX 6000 Ada)
NVIDIA_SSH_OUTPUT = """0, NVIDIA RTX 6000 Ada Generation, 45.2, 38, 12288, 49152, 36864, 62, 150.5, 300.0
1, NVIDIA RTX 6000 Ada Generation, 92.0, 85, 40960, 49152, 8192, 78, 275.3, 300.0
2, NVIDIA RTX 6000 Ada Generation, 0.0, 2, 512, 49152, 48640, 35, 25.0, 300.0
3, NVIDIA RTX 6000 Ada Generation, 0.0, 2, 512, 49152, 48640, 34, 24.8, 300.0
4, NVIDIA RTX 6000 Ada Generation, 67.5, 55, 28672, 49152, 20480, 71, 220.1, 300.0
5, NVIDIA RTX 6000 Ada Generation, 0.0, 2, 512, 49152, 48640, 33, 24.5, 300.0
6, NVIDIA RTX 6000 Ada Generation, 0.0, 2, 512, 49152, 48640, 34, 25.2, 300.0
7, NVIDIA RTX 6000 Ada Generation, 88.3, 72, 36864, 49152, 12288, 75, 265.0, 300.0"""

# hpc1 GPU output (A100 + A40)
NVIDIA_SSH_OUTPUT_HPC1_A100 = """0, NVIDIA A100-SXM4-80GB, 72.0, 65, 52000, 81920, 29920, 68, 380.0, 400.0
1, NVIDIA A100-SXM4-80GB, 0.0, 1, 256, 81920, 81664, 42, 55.0, 400.0"""

NVIDIA_SSH_OUTPUT_HPC1_A40 = """0, NVIDIA A40, 88.0, 70, 35000, 46080, 11080, 75, 290.0, 300.0
1, NVIDIA A40, 15.0, 10, 4096, 46080, 41984, 48, 80.0, 300.0"""


@pytest.fixture
def gpu_config_ssh():
    """Config with SSH GPU nodes."""
    return {
        'enabled': True,
        'gpu_nodes': ['node51', 'node52', 'node53'],
        'ssh_user': 'monitor',
    }


@pytest.fixture
def gpu_config_local():
    """Config without SSH (local mode)."""
    return {
        'enabled': True,
    }


def _no_dcgm(collector):
    """Mark DCGM as checked/unavailable on a collector instance."""
    collector._dcgm_checked = True
    collector._dcgm_mode = None


class TestGPUSSHMode:
    """Test GPU collector SSH mode."""

    def test_init_ssh_mode(self, gpu_config_ssh, tmp_path):
        """SSH mode should read gpu_nodes and ssh_user from config."""
        collector = GPUCollector(gpu_config_ssh, str(tmp_path / 'test.db'))
        assert collector._gpu_nodes == ['node51', 'node52', 'node53']
        assert collector._ssh_user == 'monitor'

    def test_init_local_mode(self, gpu_config_local, tmp_path):
        """Local mode should have empty gpu_nodes."""
        collector = GPUCollector(gpu_config_local, str(tmp_path / 'test.db'))
        assert collector._gpu_nodes == []

    @patch.object(GPUCollector, '_run_command_ssh')
    def test_collect_ssh_multi_node(self, mock_ssh, gpu_config_ssh, tmp_path):
        """SSH mode should collect from all configured nodes."""
        mock_ssh.return_value = NVIDIA_SSH_OUTPUT
        collector = GPUCollector(gpu_config_ssh, str(tmp_path / 'test.db'))
        collector._gpu_available = True
        _no_dcgm(collector)

        records = [r for r in collector.collect() if r.get('type') == 'gpu']

        # 3 nodes x 8 GPUs = 24 records
        assert len(records) == 24
        hostnames = {r.get('node_name') for r in records}
        assert hostnames == {'node51', 'node52', 'node53'}
        # One nvidia-smi call per node
        assert mock_ssh.call_count == 3

    @patch.object(GPUCollector, '_run_command_ssh')
    def test_collect_ssh_partial_failure(self, mock_ssh, gpu_config_ssh, tmp_path):
        """Should handle partial SSH failures gracefully."""
        # node51 responds, node52 fails, node53 responds
        mock_ssh.side_effect = [
            NVIDIA_SSH_OUTPUT,  # node51 OK
            None,               # node52 failed
            NVIDIA_SSH_OUTPUT,  # node53 OK
        ]
        collector = GPUCollector(gpu_config_ssh, str(tmp_path / 'test.db'))
        collector._gpu_available = True
        _no_dcgm(collector)

        records = [r for r in collector.collect() if r.get('type') == 'gpu']

        # 2 nodes x 8 GPUs = 16 records
        assert len(records) == 16
        hostnames = {r.get('node_name') for r in records}
        assert hostnames == {'node51', 'node53'}

    def test_parse_with_hostname(self, gpu_config_ssh, tmp_path):
        """Parsed records should include the hostname."""
        collector = GPUCollector(gpu_config_ssh, str(tmp_path / 'test.db'))
        records = collector._parse_nvidia_output(
            NVIDIA_SSH_OUTPUT, hostname='node51'
        )
        assert len(records) == 8
        assert all(r['node_name'] == 'node51' for r in records)

        gpu0 = records[0]
        assert gpu0['gpu_name'] == 'NVIDIA RTX 6000 Ada Generation'
        assert gpu0['gpu_util_percent'] == 45.2
        assert gpu0['memory_used_mb'] == 12288
        assert gpu0['temperature_c'] == 62

    @patch.object(GPUCollector, '_run_command_ssh')
    def test_check_available_ssh(self, mock_ssh, gpu_config_ssh, tmp_path):
        """_check_gpu_available should verify via SSH when nodes configured."""
        mock_ssh.return_value = NVIDIA_SSH_OUTPUT
        collector = GPUCollector(gpu_config_ssh, str(tmp_path / 'test.db'))

        assert collector._check_gpu_available() is True
        assert mock_ssh.call_count == 1

    @patch.object(GPUCollector, '_run_command_ssh')
    def test_check_available_ssh_all_fail(self, mock_ssh, gpu_config_ssh, tmp_path):
        """_check_gpu_available should return False if all SSH nodes fail."""
        mock_ssh.return_value = None
        collector = GPUCollector(gpu_config_ssh, str(tmp_path / 'test.db'))

        assert collector._check_gpu_available() is False
        assert mock_ssh.call_count == 3

    def test_idle_gpu_detection(self, gpu_config_ssh, tmp_path):
        """Should correctly identify idle GPUs (0% utilization)."""
        collector = GPUCollector(gpu_config_ssh, str(tmp_path / 'test.db'))
        records = collector._parse_nvidia_output(
            NVIDIA_SSH_OUTPUT, hostname='node51'
        )

        idle = [r for r in records if r.get('gpu_util_percent', 0) == 0.0]
        active = [r for r in records if r.get('gpu_util_percent', 0) > 0.0]

        assert len(idle) == 4   # GPUs 2, 3, 5, 6
        assert len(active) == 4  # GPUs 0, 1, 4, 7


class TestA100A40Support:
    """Verify A100 and A40 GPU names parse and classify correctly."""

    def test_parse_a100_output(self, tmp_path):
        collector = GPUCollector({'enabled': True}, str(tmp_path / 'test.db'))
        records = collector._parse_nvidia_output(NVIDIA_SSH_OUTPUT_HPC1_A100, hostname='nd16')
        assert len(records) == 2
        assert all('A100' in r['gpu_name'] for r in records)
        assert records[0]['gpu_util_percent'] == 72.0
        assert records[0]['memory_total_mb'] == 81920

    def test_parse_a40_output(self, tmp_path):
        collector = GPUCollector({'enabled': True}, str(tmp_path / 'test.db'))
        records = collector._parse_nvidia_output(NVIDIA_SSH_OUTPUT_HPC1_A40, hostname='nd17')
        assert len(records) == 2
        assert all('A40' in r['gpu_name'] for r in records)
        assert records[0]['memory_total_mb'] == 46080

    def test_a100_temp_threshold(self):
        assert _get_temp_threshold('NVIDIA A100-SXM4-80GB') == 93

    def test_a40_temp_threshold(self):
        assert _get_temp_threshold('NVIDIA A40') == 93

    def test_rtx6000_ada_temp_threshold(self):
        assert _get_temp_threshold('NVIDIA RTX 6000 Ada Generation') == 92

    def test_a100_in_gpu_temp_warn(self):
        assert 'A100' in GPU_TEMP_WARN
        assert 'A40' in GPU_TEMP_WARN

    def test_a100_dcgm_enrichment(self, tmp_path):
        """A100 records enrich correctly with DCGM data."""
        from nomad.collectors.gpu import DCGMStats
        collector = GPUCollector({'enabled': True}, str(tmp_path / 'test.db'))
        records = collector._parse_nvidia_output(NVIDIA_SSH_OUTPUT_HPC1_A100, hostname='nd16')
        from nomad.collectors.gpu import DCGMStats
        dcgm_data = {
            0: DCGMStats(gpu_index=0, sm_active_pct=65.0, tensor_active_pct=50.0,
                         dram_active_pct=40.0, gr_engine_active_pct=60.0, fp64_active_pct=25.0),
        }
        enriched = collector._enrich_with_dcgm(records, dcgm_data)
        assert enriched[0]['data_source'] == 'dcgm'
        assert enriched[0]['workload_class'] == 'tensor-heavy compute'
        # GPU 1 has no DCGM data — should fall back cleanly
        assert enriched[1]['data_source'] == 'nvidia-smi'
        assert enriched[1]['real_util_pct'] is None

    def test_a40_fp64_hpc_classification(self, tmp_path):
        """A40 running FP64 HPC workload classifies correctly."""
        from nomad.collectors.gpu import DCGMStats
        collector = GPUCollector({'real_util_weights': 'hpc'}, str(tmp_path / 'test.db'))
        records = collector._parse_nvidia_output(NVIDIA_SSH_OUTPUT_HPC1_A40, hostname='nd17')
        dcgm_data = {
            0: DCGMStats(gpu_index=0, sm_active_pct=75.0, tensor_active_pct=5.0,
                         dram_active_pct=30.0, gr_engine_active_pct=65.0, fp64_active_pct=30.0),
        }
        enriched = collector._enrich_with_dcgm(records, dcgm_data)
        assert enriched[0]['workload_class'] == 'FP64 / HPC compute'
