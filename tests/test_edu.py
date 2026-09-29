# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Tests for NØMAÐ Edu module using MockCluster.

Run with: pytest tests/test_edu.py -v
"""

import pytest
from nomad.testing import MockCluster
from nomad.edu.scoring import score_job, JobFingerprint, proficiency_level
from nomad.edu.explain import explain_job, load_job, load_summary
from nomad.edu.progress import user_trajectory, group_summary


class TestProficiencyScoring:
    """Test the proficiency scoring engine."""

    def test_proficiency_levels(self):
        """Test score to level mapping."""
        assert proficiency_level(90) == "Excellent"
        assert proficiency_level(75) == "Good"
        assert proficiency_level(50) == "Developing"
        assert proficiency_level(20) == "Needs Work"

    def test_score_job_complete(self):
        """Test scoring a complete job with all metrics."""
        job = {
            "job_id": "12345",
            "user_name": "testuser",
            "state": "COMPLETED",
            "req_cpus": 8,
            "req_mem_mb": 16384,
            "req_gpus": 0,
            "req_time_seconds": 7200,
            "runtime_seconds": 3600,
        }
        summary = {
            "avg_cpu_percent": 75.0,
            "peak_cpu_percent": 90.0,
            "avg_memory_gb": 8.0,
            "peak_memory_gb": 12.0,
            "avg_io_wait_percent": 5.0,
            "total_nfs_write_gb": 0.5,
            "total_local_write_gb": 2.0,
            "nfs_ratio": 0.2,
        }

        fp = score_job(job, summary)

        assert isinstance(fp, JobFingerprint)
        assert fp.job_id == "12345"
        assert fp.user == "testuser"
        assert "cpu" in fp.dimensions
        assert "memory" in fp.dimensions
        assert "time" in fp.dimensions
        assert "io" in fp.dimensions
        assert 0 <= fp.overall <= 100

    def test_score_oom_job(self):
        """Test that OOM jobs get low memory scores."""
        job = {
            "job_id": "12346",
            "user_name": "testuser",
            "state": "OUT_OF_MEMORY",
            "req_cpus": 4,
            "req_mem_mb": 8192,
            "req_gpus": 0,
            "req_time_seconds": 3600,
            "runtime_seconds": 1800,
        }
        summary = {
            "peak_memory_gb": 8.0,  # Used all of it
        }

        fp = score_job(job, summary)

        # OOM should score poorly on memory
        assert fp.dimensions["memory"].score <= 20
        assert "OUT_OF_MEMORY" in fp.dimensions["memory"].detail

    def test_score_timeout_job(self):
        """Test that TIMEOUT jobs get low time scores."""
        job = {
            "job_id": "12347",
            "user_name": "testuser",
            "state": "TIMEOUT",
            "req_cpus": 4,
            "req_mem_mb": 8192,
            "req_gpus": 0,
            "req_time_seconds": 3600,
            "runtime_seconds": 3600,  # Hit the limit
        }
        summary = {}

        fp = score_job(job, summary)

        # TIMEOUT should score poorly on time
        assert fp.dimensions["time"].score <= 25
        assert "TIMEOUT" in fp.dimensions["time"].detail or "walltime" in fp.dimensions["time"].detail.lower()

    def test_score_gpu_unused(self):
        """Test that requesting but not using GPU scores poorly."""
        job = {
            "job_id": "12348",
            "user_name": "testuser",
            "state": "COMPLETED",
            "req_cpus": 4,
            "req_mem_mb": 8192,
            "req_gpus": 2,  # Requested GPUs
            "req_time_seconds": 3600,
            "runtime_seconds": 1800,
        }
        summary = {
            "used_gpu": 1,
            "avg_gpu_util": 2.0,  # measured: the GPUs sat idle
        }

        fp = score_job(job, summary)

        assert fp.dimensions["gpu"].applicable
        assert fp.dimensions["gpu"].score <= 20
        assert "very low gpu utilization" in fp.dimensions["gpu"].detail.lower()

    def test_score_gpu_unmeasured_is_not_graded(self):
        """used_gpu comes from the request, not a measurement: no grade from it."""
        job = {"job_id": "12349", "req_gpus": 2, "req_cpus": 4,
               "req_time_seconds": 3600, "runtime_seconds": 1800}
        for summary in ({"used_gpu": 0}, {"used_gpu": 1}, {}):
            gpu = score_job(job, summary).dimensions["gpu"]
            assert not gpu.applicable
            assert "not measured" in gpu.detail


class TestMockCluster:
    """Test the MockCluster itself."""

    def test_cluster_creates_database(self):
        """Test that MockCluster creates a valid database."""
        with MockCluster() as cluster:
            assert cluster.db_path is not None
            
            # Should be able to load jobs
            import sqlite3
            conn = sqlite3.connect(cluster.db_path)
            count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            conn.close()
            
            assert count > 0

    def test_cluster_has_groups(self):
        """Test that MockCluster creates group memberships."""
        with MockCluster() as cluster:
            import sqlite3
            conn = sqlite3.connect(cluster.db_path)
            groups = conn.execute("SELECT DISTINCT group_name FROM group_membership").fetchall()
            conn.close()
            
            group_names = [g[0] for g in groups]
            assert "cs101" in group_names
            assert "bio301" in group_names


class TestExplainWithMockCluster:
    """Test explain functionality with MockCluster."""

    def test_explain_job_from_mock(self):
        """Test explaining a job from mock database."""
        with MockCluster() as cluster:
            # Get a job ID from the mock
            import sqlite3
            conn = sqlite3.connect(cluster.db_path)
            job_id = conn.execute("SELECT job_id FROM jobs LIMIT 1").fetchone()[0]
            conn.close()

            result = explain_job(job_id, cluster.db_path, show_progress=False)

            assert result is not None
            assert "Proficiency Scores" in result
            assert "CPU Efficiency" in result

    def test_explain_nonexistent_job(self):
        """Test explaining a job that doesn't exist."""
        with MockCluster() as cluster:
            result = explain_job("99999999", cluster.db_path)
            assert result is None


class TestProgressWithMockCluster:
    """Test progress tracking with MockCluster."""

    def test_user_trajectory(self):
        """Test user trajectory calculation."""
        with MockCluster() as cluster:
            # Alice should have jobs in the mock
            traj = user_trajectory(cluster.db_path, "alice", days=90)

            # May be None if not enough jobs, but shouldn't crash
            if traj is not None:
                assert traj.username == "alice"
                assert traj.total_jobs > 0

    def test_group_summary(self):
        """Test group summary calculation."""
        with MockCluster() as cluster:
            gs = group_summary(cluster.db_path, "cs101", days=90)

            if gs is not None:
                assert gs.group_name == "cs101"
                assert gs.member_count > 0


class TestPatcherFramework:
    """Test the patching framework."""

    def test_patch_applies(self):
        """Test that a simple patch applies correctly."""
        from nomad.patching import Patcher, Patch
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a test file
            test_file = os.path.join(tmpdir, "test.py")
            with open(test_file, "w") as f:
                f.write("def hello():\n    return 'hello'\n")

            patcher = Patcher(tmpdir)
            patcher.add(Patch(
                file="test.py",
                name="change_greeting",
                old="return 'hello'",
                new="return 'goodbye'",
            ))

            result = patcher.apply()

            assert result.success
            assert len(result.applied) == 1

            # Verify file was changed
            with open(test_file) as f:
                content = f.read()
            assert "return 'goodbye'" in content

    def test_patch_skips_if_present(self):
        """Test that patches skip if already applied."""
        from nomad.patching import Patcher, Patch
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.py")
            with open(test_file, "w") as f:
                f.write("def hello():\n    return 'goodbye'\n")

            patcher = Patcher(tmpdir)
            patcher.add(Patch(
                file="test.py",
                name="change_greeting",
                old="return 'hello'",
                new="return 'goodbye'",
                skip_if_present="return 'goodbye'",
            ))

            result = patcher.apply()

            assert result.success
            assert len(result.skipped) == 1
            assert len(result.applied) == 0

    def test_dry_run(self):
        """Test dry run doesn't modify files."""
        from nomad.patching import Patcher, Patch
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.py")
            original = "def hello():\n    return 'hello'\n"
            with open(test_file, "w") as f:
                f.write(original)

            patcher = Patcher(tmpdir)
            patcher.add(Patch(
                file="test.py",
                name="change_greeting",
                old="return 'hello'",
                new="return 'goodbye'",
            ))

            result = patcher.dry_run()

            assert result.success

            # File should be unchanged
            with open(test_file) as f:
                content = f.read()
            assert content == original
