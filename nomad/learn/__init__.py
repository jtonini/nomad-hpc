# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Training the model from the Console (1.45.0): TESSERA, on a machine with a GPU, asked by the hub.

An administrator asks on Data readiness ("Train…"); the request waits on the
hub, in ml.db (beside lab_check.db, the nomad account's). A worker machine
(badenpowell: ``[ml.worker]``) asks the Console every few minutes whether
there is one (``nomad ml work``, from a timer), takes it, downloads the jobs'
request-time figures, trains, and sends back how well it did and its scores.
Nothing logs in to the worker, and the worker logs in to nothing: it speaks
to the Console over HTTPS with its own key (X-ML-Worker-Key), which the
Console knows from a file.

What leaves the hub: per job, only what Slurm is asked at submission (cores,
GPUs, memory, time asked, the hour and weekday it was submitted, its
partition and cluster as numbers), when it was submitted (as a month), and
what became of it (failed or not; most of its reserved time unused or not).
Never a person, a job's name, a command line, a job's ID, a partition's or
cluster's name: the hub keeps which row was which job, and only the hub.

How well it did is measured honestly: the newest jobs (``holdout_share``)
are trained on as unknown (TESSERA's label -1), and only then are their
scores compared with what became of them, and with a logistic regression's
scores trained on the same older jobs. A job running or waiting now is
scored the same way.

Modules: settings (nomad.toml), records (ml.db), dataset (what leaves the
hub), train (the worker's training), worker (its HTTPS client), cli
(``nomad ml``).
"""
