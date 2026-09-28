# Educational Analytics

NØMAÐ Edu bridges the gap between infrastructure monitoring and educational outcomes, helping instructors, mentors, and users track the development of computational proficiency.

## Overview

Traditional HPC metrics tell you *what* happened. NØMAÐ Edu tells you *how well* users are learning to use HPC effectively.

**Use cases**:

- **Instructors**: Track class-wide skill development, identify struggling students
- **Research mentors**: Monitor graduate student onboarding progress
- **HPC staff**: Evaluate workshop and training effectiveness
- **Users**: Self-assess and improve HPC practices

## Quick Start
```bash
# Explain a job with proficiency scores and recommendations
nomad edu explain 12345

# Track a user's improvement over time
nomad edu trajectory alice

# Generate a report for a course or research group
nomad edu report cs301
```

## Commands

### `nomad edu explain`

Analyze a single job with proficiency scores and actionable recommendations.
```bash
nomad edu explain <job_id> [options]
```

**Options**:

| Option | Description |
|--------|-------------|
| `--db PATH` | Database path (default: configured DB) |
| `--json` | Output as JSON |
| `--no-progress` | Skip historical comparison |

**Example output**:
```
  NØMAÐ Job Analysis — 1104
  ────────────────────────────────────────────────────────
  User: alice    Partition: compute    Node: node03
  State: COMPLETED    Runtime: 33h 38m / 48h 00m requested

  Proficiency Scores
  ────────────────────────────────────────────────────────
    CPU Efficiency       ██░░░░░░░░   22.6%   Needs Work
    Memory Efficiency    █████████░   90.4%   Excellent
    Time Estimation      ██████████   97.4%   Excellent
    I/O Awareness        ███████░░░   68.8%   Good
    ────────────────────────────────────────────────────
    Overall Score        ███████░░░   69.8%   Good

  Recommendations
  ────────────────────────────────────────────────────────
    CPU Efficiency:
      Very low CPU utilization at 21% — requested 4
      cores but used ~1. This wastes resources and
      may delay other users' jobs.
      Try: #SBATCH --ntasks=1
          If your code is single-threaded, request 1 core.

  Your Progress (last 30 jobs)
  ────────────────────────────────────────────────────────
    CPU Efficiency        53.7% →  22.6%  ↓ declining
    Memory Efficiency     90.0% →  90.4%  → stable
    Time Estimation       88.4% →  97.4%  ↑ improving
    I/O Awareness         91.8% →  68.8%  ↓ declining
```

### `nomad edu trajectory`

Track a user's proficiency development over time.
```bash
nomad edu trajectory <username> [options]
```

**Options**:

| Option | Description |
|--------|-------------|
| `--db PATH` | Database path |
| `--days N` | Lookback period (default: 90) |
| `--json` | Output as JSON |

**Example output**:
```
  NØMAÐ Proficiency Trajectory — alice
  ────────────────────────────────────────────────────────
  Jobs analyzed: 173    Period: 2026-02-04 → 2026-02-15
  Stable proficiency

  Score Progression
  ────────────────────────────────────────────────────────
    2026-01-29    ████████░░   78.6%  (21 jobs)
    2026-02-05    █████████░   78.9%  (144 jobs)

  Dimension Changes
  ────────────────────────────────────────────────────────
    I/O Awareness         90.6%  → +4.6%
    Memory Efficiency     85.9%  → +0.2%
    GPU Utilization       85.0%  → +0.0%
    CPU Efficiency        51.9%  → -1.2%
    Time Estimation       81.3%  → -2.1%
```

### `nomad edu report`

Generate aggregate reports for courses, research groups, or any Linux group.
```bash
nomad edu report <group_name> [options]
```

**Options**:

| Option | Description |
|--------|-------------|
| `--db PATH` | Database path |
| `--days N` | Lookback period (default: 90) |
| `--json` | Output as JSON |

**Example output**:
```
  NØMAÐ Group Report — cs101
  ────────────────────────────────────────────────────────
  Members: 4     Jobs: 602
  Period: 2026-02-04 → 2026-02-16

  Key Insight
  ────────────────────────────────────────────────────────
    0/4 students improved overall proficiency

  Group Proficiency
  ────────────────────────────────────────────────────────
    Memory Efficiency    █████████░   85.2%  → -0.8%
    GPU Utilization      █████████░   85.0%  → +0.0%
    I/O Awareness        ████████░░   80.2%  → -1.5%
    Time Estimation      ████████░░   78.9%  → -1.1%
    CPU Efficiency       █████░░░░░   51.9%  → -2.4%

    Weakest area:   cpu   |   Strongest: memory

  Student Breakdown
  ────────────────────────────────────────────────────────
    Improving:   0
    Stable:      4
    Declining:   0

  Per-Student Summary
  ────────────────────────────────────────────────────────
    User        Jobs    Overall   Change    Trend
    charlie      136     73.2%    +0.7%       →
    alice        173     78.9%    +0.3%       →
    diana        147     76.8%    +0.1%       →
    bob          146     76.1%    -3.0%       →
```

## Setting Up Groups

NØMAÐ uses Linux groups for course/lab membership. To track a class:

### Option 1: Use existing Linux groups

If your users are already in groups (e.g., `bio301`, `cs101`):
```bash
# Collect group membership
nomad collect -C groups --once

# Generate report
nomad edu report bio301
```

### Option 2: Create dedicated groups
```bash
# Create group for course
sudo groupadd cs301

# Add students
sudo usermod -aG cs301 student01
sudo usermod -aG cs301 student02
# ...

# Collect and report
nomad collect -C groups --once
nomad edu report cs301
```

### Option 3: Manual group file

Create a CSV file and import:
```csv
username,group_name,gid,cluster
alice,cs301,3001,spydur
bob,cs301,3001,spydur
```
```bash
nomad edu import-groups groups.csv
```

## Dashboard Integration

The dashboard includes an Education tab showing:

- Class-wide proficiency distributions
- Individual student progress
- Common problem areas
- Improvement trends over time

Access via: `nomad dashboard` → Education tab

## Best Practices

### For Instructors

1. **Baseline early**: Collect data from the first week to establish starting points
2. **Check weekly**: Review group reports to identify struggling students early
3. **Focus on trends**: Individual job scores vary; trajectories matter more
4. **Share reports**: Let students see class-wide (anonymized) progress

### For Mentors

1. **Onboarding checkpoint**: Review trajectory after first 10 jobs
2. **Specific feedback**: Use `explain` output to guide discussions
3. **Celebrate improvement**: Recognize when dimensions improve

### For Users

1. **Review failed jobs**: Use `explain` to understand what went wrong
2. **Track your trajectory**: Check weekly to see improvement
3. **Act on recommendations**: The suggestions are data-driven

## Technical Details

For detailed information on how proficiency is computed:

- [Proficiency Scoring](proficiency.md) — Formulas, dimensions, and scoring rubrics
- [Database Schema](proficiency.md#database-storage) — How scores are stored

## Troubleshooting

### "Job not found in database"
```
Job 12345 not found in database.

Hint: Specify a database with --db or run 'nomad init' to configure.
  Example: nomad edu explain 12345 --db ~/nomad_demo.db
```

**Solutions**:

1. Specify the database: `nomad edu explain 12345 --db /path/to/db`
2. Run `nomad init` to configure the default database
3. Ensure data collection is running: `nomad collect`

### "Not enough data for user"

The user needs at least 3 completed jobs for trajectory analysis.

### "No data found for group"

Ensure group membership data has been collected:
```bash
nomad collect -C groups --once
```
