#!/usr/bin/env python3
"""
Patch 1: Add OOM/TIMEOUT state detection to scoring.py
Patch 2: Add group_membership table to demo.py
"""
import sys
from pathlib import Path


def patch_scoring_oom(nomade_dir):
    """Add job state awareness to memory scoring."""
    path = nomade_dir / 'edu' / 'scoring.py'
    content = path.read_text()
    
    if "OUT_OF_MEMORY" in content:
        print("  = scoring.py already has OOM detection")
        return True
    
    # Find the memory scoring function and add state check
    old = '''def score_memory(job: dict, summary: dict) -> DimensionScore:
    """
    Score memory efficiency.

    Compares peak memory usage against requested memory.
    Over-requesting memory is the most common resource waste on teaching
    clusters — students copy example scripts with --mem=64G for jobs
    that use 2GB.

    Scoring:
        Utilization 60-95%  → Excellent (good sizing with safety margin)
        Utilization 30-60%  → Good
        Utilization 10-30%  → Developing
        Utilization < 10%   → Needs Work (massive over-request)
        Utilization > 95%   → slight penalty (risk of OOM)
    """
    peak_mem_gb = summary.get("peak_memory_gb") or summary.get("peak_mem_gb") or 0
    req_mem_mb = job.get("req_mem_mb", 0)
    req_mem_gb = req_mem_mb / 1024 if req_mem_mb else 0'''
    
    new = '''def score_memory(job: dict, summary: dict) -> DimensionScore:
    """
    Score memory efficiency.

    Compares peak memory usage against requested memory.
    Over-requesting memory is the most common resource waste on teaching
    clusters — students copy example scripts with --mem=64G for jobs
    that use 2GB.

    Scoring:
        Utilization 60-95%  → Excellent (good sizing with safety margin)
        Utilization 30-60%  → Good
        Utilization 10-30%  → Developing
        Utilization < 10%   → Needs Work (massive over-request)
        Utilization > 95%   → slight penalty (risk of OOM)
        OUT_OF_MEMORY state → Needs Work (under-requested)
    """
    # Check for OOM failure first
    job_state = job.get("state", "").upper()
    if job_state in ("OUT_OF_MEMORY", "OOM"):
        req_mem_mb = job.get("req_mem_mb", 0)
        req_mem_gb = req_mem_mb / 1024 if req_mem_mb else 0
        peak_mem_gb = summary.get("peak_memory_gb") or summary.get("peak_mem_gb") or req_mem_gb
        # Suggest 50% more memory
        suggested_gb = max(1, round(peak_mem_gb * 1.5))
        return DimensionScore(
            name="Memory Efficiency",
            score=15,
            level="Needs Work",
            detail=(f"Job failed with OUT_OF_MEMORY. Requested {req_mem_gb:.0f}GB "
                    f"was insufficient. Your job needed more memory than allocated."),
            suggestion=f"Try: #SBATCH --mem={suggested_gb}G  (increase memory request)",
        )
    
    peak_mem_gb = summary.get("peak_memory_gb") or summary.get("peak_mem_gb") or 0
    req_mem_mb = job.get("req_mem_mb", 0)
    req_mem_gb = req_mem_mb / 1024 if req_mem_mb else 0'''
    
    if old in content:
        content = content.replace(old, new, 1)
        path.write_text(content)
        print("  + scoring.py: added OOM detection")
        return True
    else:
        print("  ! scoring.py: could not find memory function")
        return False


def patch_scoring_timeout(nomade_dir):
    """Add TIMEOUT state detection to time scoring."""
    path = nomade_dir / 'edu' / 'scoring.py'
    content = path.read_text()
    
    if "TIMEOUT" in content and "job_state" in content:
        print("  = scoring.py already has TIMEOUT detection")
        return True
    
    old = '''def score_time(job: dict, summary: dict) -> DimensionScore:
    """
    Score walltime estimation accuracy.

    Students often copy --time=48:00:00 from examples regardless of
    actual runtime. This hurts scheduler efficiency (backfill can't
    use the slot) and increases wait times for everyone.

    Scoring:
        Ratio 0.50-0.85  → Excellent (good estimate with buffer)
        Ratio 0.25-0.50  → Good
        Ratio 0.05-0.25  → Developing
        Ratio < 0.05     → Needs Work (massive over-estimate)
    """
    runtime = job.get("runtime_seconds", 0)
    req_time = job.get("req_time_seconds", 0)'''
    
    new = '''def score_time(job: dict, summary: dict) -> DimensionScore:
    """
    Score walltime estimation accuracy.

    Students often copy --time=48:00:00 from examples regardless of
    actual runtime. This hurts scheduler efficiency (backfill can't
    use the slot) and increases wait times for everyone.

    Scoring:
        Ratio 0.50-0.85  → Excellent (good estimate with buffer)
        Ratio 0.25-0.50  → Good
        Ratio 0.05-0.25  → Developing
        Ratio < 0.05     → Needs Work (massive over-estimate)
        TIMEOUT state    → Needs Work (under-estimated)
    """
    # Check for TIMEOUT failure first
    job_state = job.get("state", "").upper()
    if job_state == "TIMEOUT":
        runtime = job.get("runtime_seconds", 0)
        req_time = job.get("req_time_seconds", 0)
        # Suggest 50% more time
        suggested_sec = int(req_time * 1.5) if req_time else 7200
        suggested_h = suggested_sec // 3600
        suggested_m = (suggested_sec % 3600) // 60
        suggested_str = f"{suggested_h}:{suggested_m:02d}:00"
        return DimensionScore(
            name="Time Estimation",
            score=20,
            level="Needs Work",
            detail=(f"Job was killed for exceeding walltime. "
                    f"Your job needed more time than the requested limit."),
            suggestion=f"Try: #SBATCH --time={suggested_str}  (increase time request)",
        )
    
    runtime = job.get("runtime_seconds", 0)
    req_time = job.get("req_time_seconds", 0)'''
    
    if old in content:
        content = content.replace(old, new, 1)
        path.write_text(content)
        print("  + scoring.py: added TIMEOUT detection")
        return True
    else:
        print("  ! scoring.py: could not find time function")
        return False


def patch_demo_groups(nomade_dir):
    """Add group_membership table to demo.py."""
    path = nomade_dir / 'demo.py'
    content = path.read_text()
    
    if "group_membership" in content:
        print("  = demo.py already has group_membership")
        return True
    
    # Add table creation after node_state index
    old = '''        c.execute("CREATE INDEX IF NOT EXISTS idx_node_state_ts ON node_state(timestamp)")

        conn.commit()
        conn.close()'''
    
    new = '''        c.execute("CREATE INDEX IF NOT EXISTS idx_node_state_ts ON node_state(timestamp)")

        # Group membership for edu module
        c.execute("""CREATE TABLE IF NOT EXISTS group_membership (
            username TEXT, group_name TEXT, gid INTEGER, cluster TEXT,
            PRIMARY KEY (username, group_name, cluster))""")
        
        # Populate with demo users in demo groups
        demo_groups = [
            ("alice", "cs101", 2001, "demo"),
            ("alice", "research", 3001, "demo"),
            ("bob", "cs101", 2001, "demo"),
            ("charlie", "cs101", 2001, "demo"),
            ("charlie", "physics-lab", 3002, "demo"),
            ("diana", "cs101", 2001, "demo"),
            ("diana", "bio301", 2002, "demo"),
            ("eve", "bio301", 2002, "demo"),
            ("eve", "research", 3001, "demo"),
        ]
        for username, group_name, gid, cluster in demo_groups:
            c.execute("""INSERT OR REPLACE INTO group_membership 
                (username, group_name, gid, cluster) VALUES (?, ?, ?, ?)""",
                (username, group_name, gid, cluster))

        conn.commit()
        conn.close()'''
    
    if old in content:
        content = content.replace(old, new, 1)
        path.write_text(content)
        print("  + demo.py: added group_membership table")
        return True
    else:
        print("  ! demo.py: could not find insertion point")
        return False


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 patch_edu_state.py /path/to/nomade/nomade/")
        sys.exit(1)
    
    nomade_dir = Path(sys.argv[1])
    if not (nomade_dir / 'edu').exists():
        print(f"ERROR: {nomade_dir}/edu not found")
        sys.exit(1)
    
    print("\nPatching Edu Module")
    print("=" * 30)
    
    patch_scoring_oom(nomade_dir)
    patch_scoring_timeout(nomade_dir)
    patch_demo_groups(nomade_dir)
    
    print("\nDone! Regenerate demo data to test:")
    print("  rm ~/nomade_demo.db")
    print("  nomade demo")
    print("  nomade edu report cs101 --db ~/nomade_demo.db")


if __name__ == '__main__':
    main()
