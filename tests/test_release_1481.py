"""nomad 1.48.1: a GPU job's advice from the work its GPUs did, and what the average covers."""
import sqlite3

from nomad.db.jobkeys import gpus_during
from nomad.edu.scoring import score_gpu


def test_four_gpus_doing_less_than_one_gpus_work_are_told_one():
    d = score_gpu({"req_gpus": 4}, {"avg_gpu_util": 18.0, "gpus_measured": 4})
    assert d.level == "Developing" and d.suggestion.suggested_value == 1 and d.suggestion.current_value == 4
    assert "Together they did about 0.7 of one GPU's work, which 1 GPU kept busy would do." in d.detail
    d = score_gpu({"req_gpus": 8}, {"avg_gpu_util": 25.0, "gpus_measured": 8})
    assert d.suggestion.suggested_value == 3 and "about 2.0 GPUs' worth of work, which 3 GPUs" in d.detail
    # never more than one fewer than asked, never none
    assert score_gpu({"req_gpus": 2}, {"avg_gpu_util": 39.0, "gpus_measured": 2}).suggestion.suggested_value == 1
    assert score_gpu({"req_gpus": 8}, {"avg_gpu_util": 39.0, "gpus_measured": 8}).suggestion.suggested_value == 4


def test_an_average_over_more_gpus_than_the_job_had_says_so():
    d = score_gpu({"req_gpus": 4}, {"avg_gpu_util": 18.0, "gpus_measured": 8})
    assert "across the 8 GPUs on its node(s) while it ran (it asked for 4" in d.detail
    assert d.suggestion.suggested_value == 3 and "Together" not in d.detail
    low = score_gpu({"req_gpus": 2}, {"avg_gpu_util": 5.0, "gpus_measured": 4})
    assert "across the 4 GPUs on its node(s)" in low.detail and low.suggestion.suggested_value == 0


def test_without_a_count_as_before():
    d = score_gpu({"req_gpus": 4}, {"avg_gpu_util": 18.0})
    assert "across 4 GPU(s)." in d.detail and d.suggestion.suggested_value == 3


def test_the_gpus_an_average_covers(tmp_path):
    c = sqlite3.connect(tmp_path / "s.db")
    c.execute("CREATE TABLE gpu_stats (timestamp TEXT, node_name TEXT, gpu_index INTEGER, data_source TEXT, "
              "real_util_pct REAL, source_site TEXT)")
    for node in ("g01", "g02"):
        for i in range(4):
            for t in ("2026-11-10T10:00:00", "2026-11-10T11:00:00"):
                c.execute("INSERT INTO gpu_stats VALUES (?, ?, ?, 'dcgm', 20, 'hpc1')", (t, node, i))
    c.execute("INSERT INTO gpu_stats VALUES ('2026-11-10T10:30:00', 'g01', 7, 'nvidia-smi', NULL, 'hpc1')")
    assert gpus_during(c, "g01", "2026-11-10T09:00:00", "2026-11-10T12:00:00") == 4
    assert gpus_during(c, "g[01-02]", "2026-11-10T09:00:00", "2026-11-10T12:00:00", site="hpc1") == 8
    assert gpus_during(c, "g01", "2026-11-11T09:00:00", "2026-11-11T12:00:00") is None
    assert gpus_during(c, None, "2026-11-10T09:00:00", "2026-11-10T12:00:00") is None


def test_explain_counts_the_gpus_and_the_advice_follows(tmp_path):
    import json

    from nomad.edu import explain
    db = tmp_path / "nomad.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (job_id TEXT, user_name TEXT, state TEXT, partition TEXT, req_cpus INTEGER, "
              "req_mem_mb INTEGER, req_gpus INTEGER, req_time_seconds INTEGER, runtime_seconds INTEGER, "
              "start_time TEXT, end_time TEXT, node_list TEXT)")
    c.execute("INSERT INTO jobs VALUES ('9001', 'kim', 'COMPLETED', 'gpu', 8, 65536, 4, 7200, 3600, "
              "'2026-11-10T10:00:00', '2026-11-10T11:00:00', 'g01')")
    c.execute("CREATE TABLE job_summary (job_id TEXT, avg_cpu_percent REAL, peak_memory_gb REAL, avg_gpu_util REAL)")
    c.execute("INSERT INTO job_summary VALUES ('9001', 80.0, 30.0, 18.0)")
    c.execute("CREATE TABLE gpu_stats (timestamp TEXT, node_name TEXT, gpu_index INTEGER, data_source TEXT, "
              "real_util_pct REAL)")
    for i in range(4):
        c.execute("INSERT INTO gpu_stats VALUES ('2026-11-10T10:30:00', 'g01', ?, 'dcgm', 18)", (i,))
    c.commit()
    c.close()
    doc = json.loads(explain.explain_job("9001", str(db), output_format="json", show_progress=False, record=False))
    gpu = doc["dimensions"]["gpu"]
    assert "about 0.7 of one GPU's work" in gpu["detail"]
    assert gpu["suggestion"]["suggested_value"] == 1 and gpu["suggestion"]["current_value"] == 4
    assert gpu["suggestion"]["display"] == "#SBATCH --gres=gpu:1"
