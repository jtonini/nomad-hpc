"""1.25.5: Slurm's estimate read as spydur's sbatch (Slurm 25.11) says it."""
import subprocess

from tests.test_release_1182 import scratch  # noqa: F401  (fixture)


def test_the_estimate_with_a_word_after_the_time(scratch, monkeypatch):  # noqa: F811
    # spydur, 3 Nov 2026: every row came back as an error, Slurm's whole line in its place
    from datetime import datetime
    import nomad.submit.cli as sc
    tmp, w, sent, slurm = scratch
    pl = sc.plan("job.sh")
    monkeypatch.setattr(sc, "_node_states", lambda names: {})
    said = {"yang2": "sbatch: Job 87237 to start at 2026-11-03T09:49:39 a using 1 processors on nodes spdr60 in "
                     "partition yang2",
            "basic": "sbatch: Job 87240 to start at 2026-11-03T11:00:00 a using 1 processors on nodes spdr01 in "
                     "partition basic"}

    def run(argv, cwd, data=None):
        part = next(a.split("=", 1)[1] for a in argv if a.startswith("--partition="))
        return subprocess.CompletedProcess(argv, 0, "", said[part] + "\n")
    doc = sc.start_times(pl, [], "yang2,basic", run=run, now=datetime(2026, 11, 3, 9, 49, 30))
    by = {s["partition"]: s for s in doc["starts"]}
    assert by["yang2"] == {"partition": "yang2", "start": "2026-11-03T09:49:39", "soon": True, "nodes": "spdr60",
                           "error": None}
    assert by["basic"]["start"] == "2026-11-03T11:00:00" and not by["basic"]["soon"] and by["basic"]["error"] is None
    # another partition than the one asked is still caught, the extra word or not
    said["basic"] = said["basic"].replace("partition basic", "partition medium")
    doc = sc.start_times(pl, [], "basic", run=run, now=datetime(2026, 11, 3, 9, 49, 30))
    assert doc["starts"][0]["error"] == "Slurm would run it in partition medium, not basic"
