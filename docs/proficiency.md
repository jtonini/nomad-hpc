# Proficiency Scoring

NØMAÐ's Educational Analytics module tracks computational proficiency development through per-job behavioral fingerprints.

## Philosophy

Traditional HPC monitoring answers: *"Did the job run?"*

NØMAÐ Edu answers: *"Did the user learn to use HPC effectively?"*

This shift enables:

- Instructors to measure learning outcomes, not just resource consumption
- Mentors to identify specific skill gaps in research trainees
- Users to self-assess and improve their HPC practices
- Institutions to evaluate training program effectiveness

## The Five Dimensions

Every completed job is scored across five proficiency dimensions:
```
  NØMAÐ Group Report — cs101
  Last 90 days · 4 members · 4 ran jobs · 4 scored · 935 of 935 jobs measured · demo-cluster

  Median overall: 72/100 across 4 people (middle half 69–73)
  Over the period: 1 of 4 improved, 3 steady, 0 declined

  By dimension (median, people):
    CPU        51   (4)
    Memory     87   (4)
    Time       81   (4)
    I/O        69   (4)
    GPU        70   (4)

  Most common to work on:
    CPU: 4 of 4
    I/O: 2 of 4

  Member             Jobs  Measured  Overall  Change  Weakest
  diana               200       200       66      +1  I/O
  charlie             243       243       70      +9  CPU
  alice               260       260       73      +2  CPU
  bob                 232       232       73      -2  CPU
```

The Console's Group Reports page shows the same figures; see [edu](edu.md#nomad-edu-report).

### 1. CPU Efficiency

**What it measures**: How well did the user utilize requested CPU cores?

**Formula**:
$$\text{CPU Score} = \min\left(100, \frac{\text{CPU Time Used}}{\text{Cores} \times \text{Walltime}} \times 100\right)$$

**Scoring rubric**:

| Efficiency | Score | Level | Interpretation |
|------------|-------|-------|----------------|
| ≥ 80% | 85-100 | Excellent | Efficient parallelization |
| 50-79% | 65-84 | Good | Reasonable usage |
| 25-49% | 40-64 | Developing | Some waste, learning |
| < 25% | 0-39 | Needs Work | Significant over-allocation |

**Common issues**:

- Requesting 16 cores for single-threaded code
- I/O-bound jobs with idle CPUs
- Poor parallel scaling

**Recommendations generated**:
```
CPU Efficiency: Very low CPU utilization at 21% — requested 4 
cores but used ~1. This wastes resources and may delay other 
users' jobs.

Try: #SBATCH --ntasks=1
     If your code is single-threaded, request 1 core.
```

### 2. Memory Efficiency

**What it measures**: How well did the user size their memory request?

**Formula**:
$$\text{Memory Score} = \begin{cases}
100 - (100 - \text{Utilization}) \times 0.5 & \text{if utilization} \geq 50\% \\
\text{Utilization} \times 1.5 & \text{if utilization} < 50\%
\end{cases}$$

The asymmetric formula penalizes under-utilization more harshly than slight over-utilization (better to have headroom than OOM kills).

**Scoring rubric**:

| Utilization | Score | Level | Interpretation |
|-------------|-------|-------|----------------|
| 70-95% | 85-100 | Excellent | Well-sized |
| 50-69% | 65-84 | Good | Acceptable headroom |
| 30-49% | 40-64 | Developing | Over-allocated |
| < 30% | 0-39 | Needs Work | Significant waste |

**Common issues**:

- Requesting 64GB when job uses 2GB
- Copy-pasting scripts without adjusting memory
- Not profiling memory requirements

### 3. Time Estimation

**What it measures**: How accurately did the user estimate walltime?

**Formula**:
$$\text{Time Score} = \begin{cases}
95 + 5 \times (1 - \frac{\text{Runtime}}{\text{Requested}}) & \text{if ratio} \geq 0.7 \\
70 \times \frac{\text{Runtime}}{\text{Requested}} & \text{if ratio} < 0.7
\end{cases}$$

Using close to requested time (without exceeding) is optimal.

**Scoring rubric**:

| Runtime/Requested | Score | Level | Interpretation |
|-------------------|-------|-------|----------------|
| 70-100% | 85-100 | Excellent | Accurate estimation |
| 40-69% | 65-84 | Good | Conservative but reasonable |
| 20-39% | 40-64 | Developing | Significant overestimation |
| < 20% | 0-39 | Needs Work | Gross overestimation |

**Why it matters**:

- Backfill scheduling depends on accurate time estimates
- Over-requesting blocks resources from others
- Under-requesting causes job kills

### 4. I/O Awareness

**What it measures**: Did the user choose appropriate storage for their workload?

**Formula**:
$$\text{I/O Score} = 100 - (\text{NFS Ratio} \times 50) - (\text{IO Wait} \times 2)$$

Where:
- NFS Ratio = NFS writes / Total writes
- IO Wait = percentage of time waiting on I/O

**Scoring rubric**:

| NFS Ratio | IO Wait | Score | Level |
|-----------|---------|-------|-------|
| < 20% | < 5% | 85-100 | Excellent |
| 20-50% | 5-15% | 65-84 | Good |
| 50-80% | 15-30% | 40-64 | Developing |
| > 80% | > 30% | 0-39 | Needs Work |

**Applicability**: Scored only when NFS traffic was measured for the job.
Slurm accounting has no NFS/local breakdown, so jobs measured only by the job
collector show "not measured" rather than a score (before 1.7.5 the collector
wrote an NFS ratio of 0 as a placeholder, which scored every job Excellent).

**Common issues**:

- Writing temp files to NFS instead of local scratch
- Not using `$TMPDIR` or `/scratch`
- Reading input files repeatedly from network storage

**Recommendations generated**:
```
I/O Awareness: High NFS write ratio (78%) causing I/O wait. 
Jobs with this pattern have 3x higher failure rates.

Try: export TMPDIR=/scratch/$USER/$SLURM_JOB_ID
     Write temporary files to local scratch, copy results 
     back at job end.
```

### 5. GPU Utilization

**What it measures**: Did the user effectively utilize requested GPUs?

**Formula**:
$$\text{GPU Score} = \frac{\text{GPU Utilization} + \text{GPU Memory Utilization}}{2}$$

**Scoring rubric**:

| GPU Util | Score | Level | Interpretation |
|----------|-------|-------|----------------|
| ≥ 70% | 85-100 | Excellent | Efficient GPU usage |
| 40-69% | 65-84 | Good | Acceptable |
| 20-39% | 40-64 | Developing | Under-utilizing expensive resource |
| < 20% | 0-39 | Needs Work | GPU mostly idle |

**Applicability**: Only scored if the job requested GPUs *and* its GPU
utilization was measured (DCGM data for its nodes and time window). A GPU job
without that data shows "not measured": the request alone says nothing about
how the GPUs were used.

**Common issues**:

- CPU preprocessing starving GPU
- Small batch sizes
- Requesting GPU for CPU-only code

## Proficiency Levels

Scores map to four proficiency levels:

| Score Range | Level | Description |
|-------------|-------|-------------|
| 85-100 | **Excellent** | Demonstrates strong HPC understanding |
| 65-84 | **Good** | Reasonable usage with minor inefficiencies |
| 40-64 | **Developing** | Learning, with clear room for improvement |
| 0-39 | **Needs Work** | Significant resource waste or misconfiguration |

## Overall Score

The overall score is the plain mean of the dimensions that apply to the job
and were measured:

$$\text{Overall} = \frac{1}{|A|}\sum_{d \in A} s_d \qquad A = \text{applicable, measured dimensions}$$

A dimension that was not measured is left out, never counted as zero or as a
default. For a person or a period, dimensions are averaged over their measured
jobs first, and the overall is the mean of those averages. Only jobs NØMAÐ
measured (with a `job_summary` row) are scored; the rest are counted, not scored.

## Trajectory Tracking

Beyond single jobs, NØMAÐ tracks proficiency development over time:
```
┌────────────────────────────────────────────────────────────┐
│           Proficiency Trajectory — alice                   │
├────────────────────────────────────────────────────────────┤
│ Jobs analyzed: 173    Period: 2026-01-15 → 2026-02-15     │
│ Trend: Improving                                           │
├────────────────────────────────────────────────────────────┤
│ Score Progression                                          │
│                                                            │
│   2026-01-15    ████████░░   78.6%  (21 jobs)             │
│   2026-02-01    █████████░   82.3%  (52 jobs)             │
│   2026-02-15    █████████░   86.1%  (100 jobs)            │
│                                                            │
│ Dimension Changes                                          │
│                                                            │
│   CPU Efficiency      48.3% → 71.2%   ↑ +22.9%            │
│   Memory Efficiency   84.0% → 89.9%   ↑ +5.9%             │
│   Time Estimation     72.1% → 85.4%   ↑ +13.3%            │
│   I/O Awareness       81.5% → 88.2%   ↑ +6.7%             │
└────────────────────────────────────────────────────────────┘
```

**Trend classification**:

| Trend | Criteria |
|-------|----------|
| Improving | Recent average > Historical average + 5% |
| Stable | Within ±5% |
| Declining | Recent average < Historical average - 5% |

## Group Reports

Aggregate proficiency across course sections or research groups:
```bash
nomad edu report cs301
```
```
┌────────────────────────────────────────────────────────────┐
│           NØMAÐ Group Report — cs301                      │
├────────────────────────────────────────────────────────────┤
│ Members: 24    Jobs: 1,847    Period: 2026-01-15 → 02-15  │
├────────────────────────────────────────────────────────────┤
│ Key Insight                                                │
│   18/24 students improved overall proficiency              │
│                                                            │
│ Group Proficiency                                          │
│   Memory Efficiency    ███████████░   92.1%  → +3.2%      │
│   Time Estimation      █████████░░░   84.7%  → +8.1%      │
│   I/O Awareness        ████████░░░░   79.3%  → +5.4%      │
│   CPU Efficiency       ██████░░░░░░   58.2%  → +12.1%     │
│                                                            │
│ Weakest area: CPU    |    Strongest: Memory               │
├────────────────────────────────────────────────────────────┤
│ Student Breakdown                                          │
│   Improving:  18                                           │
│   Stable:      4                                           │
│   Declining:   2                                           │
└────────────────────────────────────────────────────────────┘
```

**Use cases**:

- **Instructors**: Identify which concepts need more coverage
- **TA/Mentors**: Find students needing individual help
- **Administrators**: Evaluate workshop effectiveness
- **Researchers**: Track new lab member onboarding

## Database Storage

`nomad edu explain` records each job it explains in `proficiency_scores`.
Trajectories, group reports and the Console's lists do not read this table:
they score every measured job directly, so they cover everyone, not only the
jobs someone happened to explain.

```sql
CREATE TABLE proficiency_scores (
    id INTEGER PRIMARY KEY,
    timestamp DATETIME,
    job_id TEXT NOT NULL,
    user_name TEXT NOT NULL,
    cluster TEXT,
    
    -- Dimension scores
    cpu_score REAL,
    cpu_level TEXT,
    memory_score REAL,
    memory_level TEXT,
    time_score REAL,
    time_level TEXT,
    io_score REAL,
    io_level TEXT,
    gpu_score REAL,
    gpu_level TEXT,
    gpu_applicable INTEGER,
    
    -- Overall
    overall_score REAL,
    overall_level TEXT,
    
    -- Recommendations
    needs_work TEXT,  -- JSON array of dimension names
    strengths TEXT,   -- JSON array of dimension names
    
    UNIQUE(job_id)
);
```

## CLI Commands
```bash
# Explain a single job
nomad edu explain <job_id>
nomad edu explain <job_id> --json
nomad edu explain <job_id> --no-progress

# User trajectory
nomad edu trajectory <username>
nomad edu trajectory <username> --days 30
nomad edu trajectory <username> --json

# Group report
nomad edu report <group_name>
nomad edu report <group_name> --days 90
nomad edu report <group_name> --json
```

## Integration with SLURM

For automatic scoring, add to SLURM epilog:
```bash
#!/bin/bash
# /etc/slurm/epilog.d/nomad-edu.sh

nomad edu explain $SLURM_JOB_ID --json >> /var/log/nomad/edu.log 2>&1
```

Users can then view their proficiency in the dashboard or via CLI.
