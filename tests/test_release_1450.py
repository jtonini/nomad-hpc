"""1.45.0: training the model from the Console, on a worker with a GPU (#117).

The hub keeps requests in ml.db; a worker (badenpowell) asks the Console for
one, downloads the jobs' request-time figures (no person, job name, job ID,
partition or cluster name), trains TESSERA and a logistic regression on the
older jobs, measures both on the newest ones neither learned from, and sends
back the summary and the scores. Here: the settings, what leaves the hub,
the requests' life, the training (logistic always; TESSERA where PyTorch is),
and the worker's pass against a stand-in Console.
"""
import json
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pytest
from click.testing import CliRunner

from nomad import lab_check as L
from nomad.learn import dataset as D
from nomad.learn import records as R
from nomad.learn import train as T
from nomad.learn import worker as W
from nomad.learn.settings import Settings, WorkerSettings, read_key, settings, worker_settings

NOW = datetime(2026, 11, 12, 12, 0)


# --- settings -------------------------------------------------------------------------------

def test_settings_and_their_problems():
    s = settings({})
    assert (s.enabled, s.engine, s.predicts, s.holdout_share, s.keep_runs, s.problems) == (
        False, "tessera", ("failure", "waste"), 0.2, 20, ())
    assert s.epochs == {"gnn": 100, "lstm": 60, "ae": 60}
    on = settings({"ml": {"enabled": False, "epochs": 100, "train": {
        "enabled": True, "engine": "baseline", "predicts": "waste", "trainer": "badenpowell",
        "jobs_days": 90, "max_jobs": 50000, "epochs": {"gnn": 20}}}})
    assert (on.enabled, on.engine, on.predicts, on.trainer, on.jobs_days, on.max_jobs, on.epochs["gnn"]) == (
        True, "baseline", ("waste",), "badenpowell", 90, 50000, 20)
    assert on.plan() == {"engine": "baseline", "predicts": ["waste"], "jobs_days": 90, "max_jobs": 50000,
                         "waste_threshold": 0.6, "holdout_share": 0.2, "epochs": {"gnn": 20, "lstm": 60, "ae": 60},
                         "compare": True, "tessera": {"similarity": "euclidean", "n_bins": 3, "k_neighbors": 10}}
    bad = settings({"ml": {"train": {"enabled": "yes", "engine": "magic", "predicts": ["luck"], "holdout_share": 0.9,
                                     "jobs_days": 1, "epochs": {"ae": 0}}}})
    assert (bad.enabled, bad.engine, bad.predicts, bad.holdout_share, bad.jobs_days, bad.epochs["ae"]) == (
        False, "tessera", ("failure", "waste"), 0.2, 365, 60)
    assert len(bad.problems) == 6
    w = worker_settings({"ml": {"worker": {"console": "https://nomad.example.edu/", "device": "cpu",
                                           "name": "badenpowell"}}})
    assert (w.console, w.device, w.name, w.problems) == ("https://nomad.example.edu", "cpu", "badenpowell", ())
    assert worker_settings({}).problems                                   # no console said


def test_read_key(tmp_path):
    p = tmp_path / "k"
    assert read_key(str(p)) is None
    p.write_text("short\n")
    assert read_key(str(p)) is None
    p.write_text("x" * 40 + "\n")
    assert read_key(str(p)) == "x" * 40


# --- what leaves the hub --------------------------------------------------------------------

def jobs_db(path, n=400, seed=0):
    rng = np.random.default_rng(seed)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE jobs (job_id TEXT, user_name TEXT, job_name TEXT, partition TEXT, req_cpus INTEGER, "
              "req_gpus INTEGER, req_mem_mb INTEGER, req_time_seconds INTEGER, runtime_seconds INTEGER, "
              "submit_time TEXT, end_time TEXT, state TEXT, source_site TEXT)")
    for i in range(n):
        sub = NOW - timedelta(days=300) + timedelta(hours=i * 17)
        cpus = int(rng.choice([1, 4, 16, 64]))
        asked = int(rng.choice([1, 4, 24, 72])) * 3600
        bad = cpus >= 16 and rng.random() < 0.7 or rng.random() < 0.05     # big jobs fail more: a signal
        state = "FAILED" if bad else ("CANCELLED by 1234" if rng.random() < 0.05 else "COMPLETED")
        ran = int(asked * (0.1 if cpus == 1 else 0.8))                     # small jobs ask for far too long
        c.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (f"{1000 + i}", f"user{i % 7}", f"secret_job_{i}", ["basicpart", "gpupart"][i % 2], cpus, i % 2, 4096,
                   asked, ran, sub.isoformat(), (sub + timedelta(seconds=ran)).isoformat(), state,
                   ["spydur", "arachne"][i % 3 == 0]))
    for i in range(5):
        c.execute("INSERT INTO jobs VALUES (?, 'user1', 'waiting_job', 'basicpart', 8, 0, 2048, 7200, NULL, ?, NULL, "
                  "?, 'spydur')", (f"9{i}", (NOW - timedelta(hours=i)).isoformat(), ["PENDING", "RUNNING"][i % 2]))
    c.commit()
    return c


PLAN = Settings(enabled=True).plan()


def test_the_dataset_has_numbers_only(tmp_path):
    conn = jobs_db(tmp_path / "combined.db")
    data, mapping = D.build(conn, PLAN, NOW)
    text = json.dumps(data)
    for word in ("user1", "secret_job", "basicpart", "gpupart", "spydur", "arachne", "1001", "waiting_job"):
        assert word not in text, word
    n_ended = sum(1 for p in data["part"] if p != "now")
    assert data["counts"]["now"] == 5 and data["counts"]["holdout"] == round(n_ended * 0.2)
    assert len(data["rows"]) == len(mapping) == len(data["part"]) == len(data["window"])
    assert data["features"] == list(D.FEATURES) and data["partitions"] == 2 and data["clusters"] == 2
    # the holdout is the newest ended jobs
    ends = {j: r["end_time"] for j, r in ((r["job_id"], r) for r in conn.execute("SELECT * FROM jobs"))}
    train_end = max(ends[mapping[i][1]] for i, p in enumerate(data["part"]) if p == "train")
    hold_end = min(ends[mapping[i][1]] for i, p in enumerate(data["part"]) if p == "holdout")
    assert train_end <= hold_end
    # cancelled jobs aren't outcomes; failed ones have no waste label; waiting ones none at all
    states = {r["job_id"]: r["state"] for r in conn.execute("SELECT job_id, state FROM jobs")}
    assert not any(states[j].startswith("CANCELLED") for _, j, _ in mapping)
    for i, (_, j, _) in enumerate(mapping):
        if states[j] == "FAILED":
            assert data["labels"]["failure"][i] == 1 and data["labels"]["waste"][i] is None
        if states[j] in ("PENDING", "RUNNING"):
            assert data["part"][i] == "now" and data["labels"]["failure"][i] is None
    capped, _ = D.build(conn, {**PLAN, "max_jobs": 100}, NOW)
    assert sum(1 for p in capped["part"] if p != "now") == 100
    with pytest.raises(ValueError):
        D.build(sqlite3.connect(":memory:"), PLAN, NOW)


# --- the requests ---------------------------------------------------------------------------

@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(R.os, "geteuid", lambda: 1000)
    conn = jobs_db(tmp_path / "combined.db")
    rec = R.open_records(tmp_path / "ml.db", write=True)
    return conn, rec, Settings(enabled=True, keep_runs=2)


def test_a_request_from_asked_to_done(hub):
    conn, rec, s = hub
    with pytest.raises(R.RequestError, match="off"):
        R.ask(rec, Settings(), "adam")
    r = R.ask(rec, s, "Adam", note="  first   one ")
    assert (r["state"], r["asked_by"], r["note"], r["plan"]["engine"]) == ("queued", "adam", "first one", "tessera")
    with pytest.raises(R.RequestError, match="queued already"):
        R.ask(rec, s, "adam")
    got = R.claim(rec, s, "badenpowell")
    assert (got["id"], got["state"], got["worker"]) == (r["id"], "running", "badenpowell")
    assert R.claim(rec, s, "badenpowell") is None
    with pytest.raises(R.RequestError, match="isn't running on other"):
        R.dataset(rec, conn, r["id"], "other")
    data = R.dataset(rec, conn, r["id"], "badenpowell", NOW)
    assert data["request"] == r["id"] and rec.execute("SELECT COUNT(*) FROM ml_jobs").fetchone()[0] == len(data["rows"])
    R.progress(rec, r["id"], "badenpowell", "training\n on   400 jobs")
    assert R.request_of(rec, r["id"])["progress"] == "training on 400 jobs"
    result = T.run({**data, "plan": {**data["plan"], "engine": "baseline"}}, "cpu")
    done = R.finish(rec, s, r["id"], "badenpowell", result)
    assert done["state"] == "done" and done["summary"]["targets"]["failure"]["auc"]["baseline"] > 0.6
    st = R.status(rec, s)
    assert st["current"] is None and st["last"]["id"] == r["id"] and st["enabled"] is True
    top = R.scores(rec, r["id"], "failure", "now", 3)
    assert len(top) == 3 and top[0]["job_id"].startswith("9") and top[0]["risk"] >= top[1]["risk"]
    hold = R.scores(rec, r["id"], "failure", "holdout", 1000)
    assert hold and all(x["label"] in (0, 1) for x in hold)


def test_refusals_stale_cancel_and_pruning(hub):
    conn, rec, s = hub
    r = R.ask(rec, s, "adam")
    assert R.cancel(rec, r["id"], "adam")["state"] == "cancelled"
    with pytest.raises(R.RequestError, match="only a queued one"):
        R.cancel(rec, r["id"], "adam")
    r = R.ask(rec, s, "adam")
    R.claim(rec, s, "w")
    rec.execute("UPDATE ml_requests SET progress_at = '2020-01-01T00:00:00' WHERE id = ?", (r["id"],))
    rec.commit()
    assert R.give_up_stale(rec, s) == 1 and R.request_of(rec, r["id"])["state"] == "failed"
    with pytest.raises(R.RequestError):
        R.progress(rec, r["id"], "w", "late")
    ids = []
    for _ in range(3):
        q = R.ask(rec, s, "adam")
        R.claim(rec, s, "w")
        data = R.dataset(rec, conn, q["id"], "w", NOW)
        res = T.run({**data, "plan": {**data["plan"], "engine": "baseline"}}, "cpu")
        with pytest.raises(R.RequestError, match="not a row sent"):
            R.finish(rec, s, q["id"], "w", {**res, "scores": {"failure": [{"idx": 10 ** 6, "part": "now"}]}})
        with pytest.raises(R.RequestError, match="not asked"):
            R.finish(rec, s, q["id"], "w", {**res, "scores": {"luck": []}})
        R.finish(rec, s, q["id"], "w", res)
        ids.append(q["id"])
    kept = {r[0] for r in rec.execute("SELECT DISTINCT request FROM ml_scores")}
    assert kept == set(ids[-2:])                                   # keep_runs 2
    assert {r[0] for r in rec.execute("SELECT DISTINCT request FROM ml_jobs")} == set(ids[-2:])
    q = R.ask(rec, s, "adam")
    R.claim(rec, s, "w")
    assert R.fail(rec, q["id"], "w", "no GPU\nhere")["error"] == "no GPU here"


def test_ml_db_is_never_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(R.os, "geteuid", lambda: 0)
    with pytest.raises(R.RequestError, match="root's"):
        R.open_records(tmp_path / "ml.db", write=True)
    assert R.open_records(tmp_path / "none.db") is None


def test_where_ml_db_is(tmp_path, monkeypatch):
    from nomad.actions import remote
    monkeypatch.setattr(remote, "hub_settings", lambda target=None: {"combined_db": tmp_path / "combined.db"})
    assert R.db_path({}) == tmp_path / "ml.db"
    there = tmp_path / "home" / "lab_check.db"
    assert R.db_path({"console": {"labs": {"check": {"db": str(there)}}}}) == there.parent / "ml.db"
    assert R.db_path({"ml": {"train": {"db": "x/ml.db"}}}) == tmp_path / "x" / "ml.db"


# --- the training ---------------------------------------------------------------------------

def test_baseline_training_is_measured_on_the_holdout(tmp_path):
    conn = jobs_db(tmp_path / "combined.db")
    data, _ = D.build(conn, {**PLAN, "engine": "baseline"}, NOW)
    out = T.run(data, "cpu")
    sm = out["summary"]
    f = sm["targets"]["failure"]
    assert f["holdout"] == data["counts"]["holdout"] and f["now"] == 5 and f["auc"]["tessera"] is None
    assert f["auc"]["baseline"] > 0.6 and f["top_tenth"]["baseline"] >= f["rate_holdout"]
    w = sm["targets"]["waste"]
    assert w["jobs"] < f["jobs"]                                     # completed ones (and those now)
    assert {x["part"] for x in out["scores"]["failure"]} == {"holdout", "now"}
    assert all(x["label"] is None for x in out["scores"]["failure"] if x["part"] == "now")
    assert sm["engine"] == "baseline" and sm["device"] is None


def test_too_few_to_learn_from_is_said(tmp_path):
    conn = jobs_db(tmp_path / "combined.db", n=40)
    data, _ = D.build(conn, {**PLAN, "engine": "baseline"}, NOW)
    out = T.run(data, "cpu")
    assert "too few" in out["summary"]["targets"]["failure"]["skipped"] and "failure" not in out["scores"]


def test_tessera_needs_its_packages_and_a_gpu_when_asked(tmp_path, monkeypatch):
    conn = jobs_db(tmp_path / "combined.db")
    data, _ = D.build(conn, PLAN, NOW)
    try:
        import torch
    except ImportError:
        with pytest.raises(T.TrainError, match="PyTorch"):
            T.run(data, "cuda")
        return
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(T.TrainError, match="no CUDA GPU"):
        T.run(data, "cuda")


def test_tessera_training_where_pytorch_is(tmp_path):
    pytest.importorskip("torch")
    pytest.importorskip("torch_geometric")
    pytest.importorskip("tessera.models.classifier")
    conn = jobs_db(tmp_path / "combined.db")
    data, _ = D.build(conn, {**PLAN, "epochs": {"gnn": 20, "lstm": 5, "ae": 5}}, NOW)
    out = T.run(data, "cpu")
    f = out["summary"]["targets"]["failure"]
    assert f["auc"]["tessera"] is not None and f["auc"]["baseline"] is not None
    assert set(f["weights"]) == {"gnn", "lstm", "ae"} and f["similarity"] == "dense"


# --- the worker's pass, against a stand-in Console --------------------------------------------

class Console:
    """The Console's worker routes, over the records directly."""
    def __init__(self, conn, rec, s):
        self.conn, self.rec, self.s, self.calls = conn, rec, s, []

    def post(self, path, body=None, timeout=None):
        self.calls.append(path)
        parts = path.strip("/").split("/")
        w = (body or {}).get("worker")
        if parts[-1] == "claim":
            got = R.claim(self.rec, self.s, w)
            return {"request": got and {"id": got["id"], "plan": got["plan"]}}
        rid, what = int(parts[3]), parts[4]
        if what == "data":
            return R.dataset(self.rec, self.conn, rid, w, NOW)
        if what == "progress":
            R.progress(self.rec, rid, w, body["text"])
            return {}
        if what == "result":
            return R.finish(self.rec, self.s, rid, w, body)
        if what == "fail":
            return R.fail(self.rec, rid, w, body["error"])
        raise AssertionError(path)


def test_a_worker_pass(hub):
    conn, rec, s = hub
    ws = WorkerSettings(console="https://x", name="badenpowell", device="cpu")
    console = Console(conn, rec, Settings(enabled=True, engine="baseline"))
    assert W.work_once(ws, say=lambda t: None, client=console) == "no training request"
    r = R.ask(rec, Settings(enabled=True, engine="baseline"), "adam")
    said = []
    assert W.work_once(ws, say=said.append, client=console) == f"request {r['id']}: done"
    assert R.request_of(rec, r["id"])["state"] == "done" and any("training on" in x for x in said)
    # a failure is told to the Console, then raised
    r = R.ask(rec, Settings(enabled=True), "adam")                  # tessera, on a GPU this test hasn't
    with pytest.raises(T.TrainError):
        W.work_once(WorkerSettings(console="https://x", name="badenpowell", device="cuda"), say=lambda t: None,
                    client=console)
    assert R.request_of(rec, r["id"])["state"] == "failed"


def test_the_client_needs_its_settings_and_key(tmp_path):
    with pytest.raises(W.WorkerError, match="console isn't set"):
        W.Client(WorkerSettings())
    with pytest.raises(W.WorkerError, match="no worker key"):
        W.Client(WorkerSettings(console="https://x", key_file=str(tmp_path / "none")))


def test_the_client_never_follows_a_redirect_with_its_key(tmp_path):
    """A sign-in page in front of the Console: the worker says so, and its key goes nowhere else."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append((self.path, self.headers.get("X-ML-Worker-Key")))
            if self.path.startswith("/api/ml/worker/claim"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/signin?next=x")
                self.end_headers()
            else:
                body = b"<html>sign in</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        do_GET = do_POST
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        key = tmp_path / "key"
        key.write_text("k" * 43)
        c = W.Client(WorkerSettings(console=f"http://127.0.0.1:{srv.server_port}", key_file=str(key)))
        with pytest.raises(W.WorkerError, match=r"redirect \(302, to http://127.0.0.1:\d+/signin\), not followed"):
            c.post("/api/ml/worker/claim", {"worker": "w"})
        assert [p for p, _ in seen] == ["/api/ml/worker/claim"]          # never to /signin
        with pytest.raises(W.WorkerError, match="not JSON"):
            c.post("/api/ml/worker/hello", {"worker": "w"})
    finally:
        srv.shutdown()


# --- `nomad ml` -----------------------------------------------------------------------------

def test_cli_on_the_hub(tmp_path, monkeypatch):
    from nomad.actions import remote
    from nomad.cli import cli as main
    monkeypatch.setattr(remote, "hub_settings", lambda target=None: {"combined_db": tmp_path / "combined.db"})
    monkeypatch.setattr(R.os, "geteuid", lambda: 1000)
    monkeypatch.setattr("nomad.learn.cli.os.geteuid", lambda: 1000)
    key = tmp_path / "key"
    toml = tmp_path / "nomad.toml"
    toml.write_text(f'[ml]\nenabled = false\nepochs = 100\n[ml.train]\nenabled = true\nengine = "baseline"\n'
                    f'trainer = "badenpowell"\nkey_file = "{key}"\n')
    run = lambda *a: CliRunner().invoke(main, ["-c", str(toml), "ml", *a])  # noqa: E731
    r = run("status")
    assert r.exit_code == 0, r.output
    assert "Training from the Console: on; engine baseline" in r.output and "Now: none" in r.output
    r = run("ask", "--by", "adam", "--note", "first")
    assert r.exit_code == 0 and "request 1 queued" in r.output
    assert run("ask").exit_code != 0
    assert "request 1 cancelled" in run("cancel", "1").output
    r = run("key")
    assert r.exit_code == 0 and "made" in r.output and "fingerprint" in r.output
    assert oct(key.stat().st_mode & 0o777) == "0o600" and read_key(str(key))
    again = run("key")
    assert "made" not in again.output and again.output.split("fingerprint ")[1] == r.output.split("fingerprint ")[1]
    assert json.loads(run("status", "--json").output)["history"][0]["state"] == "cancelled"
    assert "No scores" in run("scores", "1", "failure").output
    t = run("timer", "--user", "root")
    assert t.exit_code != 0


def test_cli_key_on_a_worker_isnt_made_there(tmp_path):
    from nomad.cli import cli as main
    toml = tmp_path / "nomad.toml"
    toml.write_text(f'[ml.worker]\nconsole = "https://x"\nkey_file = "{tmp_path / "k"}"\n')
    r = CliRunner().invoke(main, ["-c", str(toml), "ml", "key"])
    assert r.exit_code != 0 and "copy the hub's there" in r.output
    (tmp_path / "k").write_text("y" * 43 + "\n")
    (tmp_path / "k").chmod(0o644)
    r = CliRunner().invoke(main, ["-c", str(toml), "ml", "key"])
    assert r.exit_code == 0 and "chmod 600" in r.output


def test_check_says_whether_training_can_run_here(monkeypatch):
    from nomad.learn import cli as C
    ws = WorkerSettings(console="https://x", device="cuda")
    monkeypatch.setattr(T, "_versions", lambda: {"tessera-ml": None})
    assert "pip install 'nomad-hpc[predict]'" in C._can_train(ws)
    monkeypatch.setattr(T, "_versions", lambda: {"tessera-ml": "0.3.0", "torch": "2.5.1"})

    def no_gpu(device):
        raise T.TrainError("no CUDA GPU PyTorch can use here")
    monkeypatch.setattr(T, "_device", no_gpu)
    assert C._can_train(ws) == "TESSERA 0.3.0 is here, but: no CUDA GPU PyTorch can use here"
    monkeypatch.setattr(T, "_device", lambda device: "cpu")
    assert C._can_train(WorkerSettings(device="cpu")) == "training here: TESSERA 0.3.0, PyTorch 2.5.1, on cpu"


def test_account_requests_table_is_a_known_setting():
    """[console.labs.accounts] (1.42.0) is read; the check had called it unknown and ignored."""
    from nomad.config.access import access_from
    got = access_from({"console": {"labs": {"accounts": {"notify": "hpc@example.edu"}, "nonsense": 1}}}, log=False)
    said = " ".join(got.problems)
    assert "accounts: unknown" not in said and "[console.labs] nonsense: unknown setting; ignored" in said


def test_two_passes_never_take_the_same_request(tmp_path, monkeypatch):
    """The second of two passes asking at once finds the request taken (1.45.1)."""
    monkeypatch.setattr(R.os, "geteuid", lambda: 1000)
    s = settings({"ml": {"train": {"enabled": True, "db": str(tmp_path / "ml.db")}}})
    a = R.open_records(tmp_path / "ml.db", write=True)
    b = R.open_records(tmp_path / "ml.db", write=True)
    R.ask(a, s, "adam", None)
    real = b.execute

    class Racing:                          # b reads "queued", then a takes it before b's update
        def __getattr__(self, name):
            return getattr(b, name)

        def __enter__(self):
            return b.__enter__()

        def __exit__(self, *e):
            return b.__exit__(*e)

        def execute(self, sql, *args):
            got = real(sql, *args)
            if sql.startswith("SELECT id FROM ml_requests WHERE state = 'queued'"):
                row = got.fetchone()
                assert R.claim(a, s, "badenpowell")["state"] == "running"
                return type("C", (), {"fetchone": lambda self: row})()
            return got
    assert R.claim(Racing(), s, "badenpowell") is None


def test_lab_check_untouched():
    assert L.SCHEMA                                                  # ml.db is its own file


# ── #121: size advice only from jobs that completed ─────────────────────────────────────────

SIZING_TIERS = [{"cluster": "spydur", "memory_mb": 384_000, "memory_gb": 375.0, "node_count": 16,
                 "partitions": "basic"}]


def _sized_db(path, jobs):
    """jobs: (state, cpu_percent, runtime_s, mem_gb) for one person on one partition: 52 cores,
    350 GB and 12 days asked each time (as on spdr01)."""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, cluster TEXT, user_name TEXT, partition TEXT, "
              "node_list TEXT, job_name TEXT, state TEXT, exit_code INTEGER, exit_signal INTEGER, "
              "failure_reason TEXT, submit_time TEXT, start_time TEXT, end_time TEXT, req_cpus INTEGER, "
              "req_mem_mb INTEGER, req_gpus INTEGER, req_time_seconds INTEGER, runtime_seconds INTEGER, "
              "wait_time_seconds INTEGER)")
    c.execute("CREATE TABLE job_summary (job_id TEXT PRIMARY KEY, cluster TEXT, peak_cpu_percent REAL, "
              "peak_memory_gb REAL, avg_cpu_percent REAL, avg_memory_gb REAL, avg_io_wait_percent REAL, "
              "total_nfs_read_gb REAL, total_nfs_write_gb REAL, total_local_read_gb REAL, "
              "total_local_write_gb REAL, nfs_ratio REAL, used_gpu INTEGER, avg_gpu_util REAL, health_score REAL)")
    for i, (state, cpu, run, mem) in enumerate(jobs):
        jid = str(9000 + i)
        c.execute("INSERT INTO jobs (job_id, cluster, user_name, partition, state, end_time, req_cpus, req_mem_mb, "
                  "req_gpus, req_time_seconds, runtime_seconds, wait_time_seconds) VALUES "
                  "(?, 'spydur', 'pat', 'basic', ?, datetime('now', ?), 52, 358400, 0, 1036800, ?, 60)",
                  (jid, state, f"-{i + 1} hours", run))
        c.execute("INSERT INTO job_summary (job_id, cluster, peak_cpu_percent, avg_cpu_percent, peak_memory_gb, "
                  "avg_memory_gb, avg_io_wait_percent, nfs_ratio, used_gpu, health_score) VALUES "
                  "(?, 'spydur', ?, ?, ?, ?, 1.0, 0.1, 0, 50.0)", (jid, cpu, cpu, mem, mem))
    c.commit()
    c.close()


def _insights(tmp_path, jobs, name="sized"):
    from nomad.edu.insights import user_insights
    db = str(tmp_path / f"{name}.db")
    _sized_db(db, jobs)
    ui = user_insights(db, "pat", cluster_capacities=SIZING_TIERS)
    return {i.dimension_key: i for i in ui.issues if i.kind == "dimension"}


def test_failed_jobs_give_no_advice_to_ask_for_less(tmp_path):
    """Three jobs failed in their first minutes (as on spdr01): no "52 → 2 cores", "12 days → 2:30"
    or "350G → 64G"; a card says they failed, and how many."""
    got = _insights(tmp_path, [("FAILED", 2.0, 300, 3.0)] * 3)
    assert not {"cpu", "memory", "time"} & set(got), sorted(got)
    f = got["failed"]
    assert f.dimension == "Jobs that failed" and f.failed_jobs == 3 and f.completed_jobs == 0
    assert f.affected_jobs == 3 and f.total_applicable == 3 and f.severity == "high"
    assert "failed" in f.rationale and not f.directive_label


def test_completed_jobs_still_get_their_advice_with_how_many(tmp_path):
    got = _insights(tmp_path, [("COMPLETED", 2.0, 3600, 3.0)] * 3)
    assert "cpu" in got and "failed" not in got
    assert got["cpu"].completed_jobs == 3 and got["cpu"].failed_jobs == 0
    assert got["cpu"].affected_jobs == 3


def test_advice_from_the_completed_ones_and_the_failed_ones_said(tmp_path):
    got = _insights(tmp_path, [("COMPLETED", 2.0, 3600, 3.0)] * 3 + [("FAILED", 1.0, 120, 1.0)] * 3)
    cpu = got["cpu"]
    assert cpu.completed_jobs == 3 and cpu.failed_jobs == 3
    assert cpu.affected_jobs == 3 and cpu.total_applicable == 3            # judged on the completed three
    assert got["failed"].failed_jobs == 3 and got["failed"].severity == "medium"


def test_one_failure_among_many_is_no_card(tmp_path):
    got = _insights(tmp_path, [("COMPLETED", 2.0, 3600, 3.0)] * 4 + [("FAILED", 1.0, 120, 1.0)])
    assert "failed" not in got and got["cpu"].failed_jobs == 1


def test_a_job_that_used_all_it_asked_for_is_no_cpu_advice(tmp_path):
    """The running job on spdr01 used all 52 cores: when such jobs complete, no advice to ask for less."""
    got = _insights(tmp_path, [("COMPLETED", 95.0, 900_000, 330.0)] * 3 + [("FAILED", 2.0, 300, 3.0)] * 3)
    assert "cpu" not in got and "memory" not in got
    assert got["failed"].completed_jobs == 3


def test_timeouts_count_for_time_and_out_of_memory_for_memory_only(tmp_path):
    got = _insights(tmp_path, [("TIMEOUT", 2.0, 1036800, 3.0)] * 3)
    assert "cpu" not in got and "memory" not in got                       # cut off: their use says little
    assert "failed" not in got                                            # a time limit, not a failure
    got = _insights(tmp_path, [("OUT_OF_MEMORY", 2.0, 300, 350.0)] * 3, name="oom")
    assert "cpu" not in got and "time" not in got


def test_person_facts_carry_the_counts(tmp_path):
    from nomad.facts.person import _issue
    got = _insights(tmp_path, [("COMPLETED", 2.0, 3600, 3.0)] * 3 + [("FAILED", 1.0, 120, 1.0)] * 3)
    d = _issue(got["cpu"])
    assert d["completed_jobs"] == 3 and d["failed_jobs"] == 3


def test_the_failed_card_leads_its_place_and_says_so(tmp_path):
    from nomad.edu.insights import format_user_insights, user_insights
    db = str(tmp_path / "lead.db")
    _sized_db(db, [("COMPLETED", 2.0, 3600, 3.0)] * 2 + [("FAILED", 2.0, 300, 3.0)] * 3)
    ui = user_insights(db, "pat", cluster_capacities=SIZING_TIERS)
    assert ui.issues[0].dimension_key == "failed"
    text = format_user_insights(ui)
    assert "3 of 5 measured jobs failed (2 completed)" in text
    assert "from the jobs that completed; 3 that didn't aren't counted" in text
    assert "scored below threshold" not in text.split("CPU Efficiency")[0]
