"""A practice run (nomad demos): pi, guessed by throwing random points at a
square and counting those inside the circle. Its result means nothing for
anyone's research: it shows how a Python job runs and what it writes."""
import os
import time

import numpy as np

throws = 2_000_000_000
chunk = 20_000_000
rng = np.random.default_rng(42)
inside = 0
started = time.time()
for done in range(0, throws, chunk):
    xy = rng.random((chunk, 2))
    inside += int(np.count_nonzero((xy * xy).sum(axis=1) <= 1.0))
    if (done + chunk) % 200_000_000 == 0:
        print(f"{done + chunk:>14,} points: pi is about {4 * inside / (done + chunk):.7f}", flush=True)
took = time.time() - started
print(f"{throws:,} points in {took:.1f} s on a job of {os.environ.get('SLURM_CPUS_PER_TASK', '?')} CPU(s)")
with open("pi.txt", "w") as f:
    f.write(f"{4 * inside / throws:.8f}\n")
print("Wrote pi.txt")
