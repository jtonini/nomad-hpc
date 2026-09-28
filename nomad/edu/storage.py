# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Edu — Proficiency Score Storage

Stores proficiency scores in the database for historical tracking.
This enables:
    - Tracking user improvement over time
    - Generating course/group reports
    - Dashboard visualizations
    - Research on HPC training effectiveness
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nomad.edu.scoring import JobFingerprint

logger = logging.getLogger(__name__)

# ── Schema ───────────────────────────────────────────────────────────

PROFICIENCY_SCHEMA = """
CREATE TABLE IF NOT EXISTS proficiency_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    job_id TEXT NOT NULL,
    user_name TEXT NOT NULL,
    cluster TEXT DEFAULT 'default',
    
    -- Dimension scores (0-100)
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
    
    -- Recommendations (JSON array)
    needs_work TEXT,
    strengths TEXT,
    
    UNIQUE(job_id)
);

CREATE INDEX IF NOT EXISTS idx_proficiency_user 
    ON proficiency_scores(user_name);
CREATE INDEX IF NOT EXISTS idx_proficiency_timestamp 
    ON proficiency_scores(timestamp);
CREATE INDEX IF NOT EXISTS idx_proficiency_cluster_user 
    ON proficiency_scores(cluster, user_name);
"""


def init_proficiency_table(db_path: str | Path) -> None:
    """Create the proficiency_scores table if it doesn't exist."""
    conn = sqlite3.connect(db_path)
    conn.executescript(PROFICIENCY_SCHEMA)
    conn.commit()
    conn.close()
    logger.debug(f"Initialized proficiency_scores table in {db_path}")


def save_proficiency_score(
    db_path: str | Path,
    fingerprint: 'JobFingerprint',
    cluster: str = 'default',
) -> bool:
    """
    Save a job's proficiency fingerprint to the database.
    
    Args:
        db_path: Path to the SQLite database
        fingerprint: JobFingerprint from score_job()
        cluster: Cluster name for multi-cluster setups
        
    Returns:
        True if saved successfully, False otherwise
    """
    import json

    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        # Ensure table exists
        c.executescript(PROFICIENCY_SCHEMA)

        # Extract dimension scores
        cpu = fingerprint.dimensions.get("cpu")
        memory = fingerprint.dimensions.get("memory")
        time = fingerprint.dimensions.get("time")
        io = fingerprint.dimensions.get("io")
        gpu = fingerprint.dimensions.get("gpu")

        c.execute("""
            INSERT OR REPLACE INTO proficiency_scores (
                timestamp, job_id, user_name, cluster,
                cpu_score, cpu_level,
                memory_score, memory_level,
                time_score, time_level,
                io_score, io_level,
                gpu_score, gpu_level, gpu_applicable,
                overall_score, overall_level,
                needs_work, strengths
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            fingerprint.job_id,
            fingerprint.user,
            cluster,
            cpu.score if cpu else None,
            cpu.level if cpu else None,
            memory.score if memory else None,
            memory.level if memory else None,
            time.score if time else None,
            time.level if time else None,
            io.score if io else None,
            io.level if io else None,
            gpu.score if gpu else None,
            gpu.level if gpu else None,
            1 if (gpu and gpu.applicable) else 0,
            fingerprint.overall,
            fingerprint.overall_level,
            json.dumps([d.name for d in fingerprint.needs_work]),
            json.dumps([d.name for d in fingerprint.strengths]),
        ))

        conn.commit()
        conn.close()
        logger.debug(f"Saved proficiency score for job {fingerprint.job_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to save proficiency score: {e}")
        return False


def get_user_proficiency_history(
    db_path: str | Path,
    username: str,
    cluster: str = None,
    days: int = 90,
    limit: int = 100,
) -> list[dict]:
    """
    Retrieve a user's proficiency history.
    
    Args:
        db_path: Path to the SQLite database
        username: Username to query
        cluster: Optional cluster filter
        days: Lookback period in days
        limit: Maximum number of records
        
    Returns:
        List of proficiency records as dicts
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    query = """
        SELECT * FROM proficiency_scores
        WHERE user_name = ?
        AND timestamp >= datetime('now', ?)
    """
    params = [username, f'-{days} days']

    if cluster:
        query += " AND cluster = ?"
        params.append(cluster)

    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    c.execute(query, params)
    rows = [dict(row) for row in c.fetchall()]
    conn.close()

    return rows


def get_group_proficiency_stats(
    db_path: str | Path,
    group_name: str,
    days: int = 90,
) -> dict:
    """
    Get aggregate proficiency statistics for a group.
    
    Args:
        db_path: Path to the SQLite database
        group_name: Group name to query
        days: Lookback period in days
        
    Returns:
        Dict with group statistics
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get group members
    c.execute("""
        SELECT DISTINCT username FROM group_membership
        WHERE group_name = ?
    """, (group_name,))
    members = [row['username'] for row in c.fetchall()]

    if not members:
        conn.close()
        return None

    # Get proficiency stats for members
    placeholders = ','.join('?' * len(members))
    c.execute(f"""
        SELECT 
            user_name,
            COUNT(*) as job_count,
            AVG(overall_score) as avg_overall,
            AVG(cpu_score) as avg_cpu,
            AVG(memory_score) as avg_memory,
            AVG(time_score) as avg_time,
            AVG(io_score) as avg_io,
            AVG(gpu_score) as avg_gpu
        FROM proficiency_scores
        WHERE user_name IN ({placeholders})
        AND timestamp >= datetime('now', ?)
        GROUP BY user_name
    """, members + [f'-{days} days'])

    user_stats = [dict(row) for row in c.fetchall()]
    conn.close()

    return {
        'group_name': group_name,
        'member_count': len(members),
        'members_with_data': len(user_stats),
        'user_stats': user_stats,
    }
