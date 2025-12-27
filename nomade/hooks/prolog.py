#!/usr/bin/env python3
"""
SLURM Prolog Script for Real-Time Job Scoring

This script runs before each job starts and scores it against
the trained ML model to identify high-risk submissions.

Installation:
    1. Copy to /etc/slurm/prolog.d/nomade_score.py
    2. Make executable: chmod +x nomade_score.py
    3. Add to slurm.conf: Prolog=/etc/slurm/prolog.d/nomade_score.py

Environment variables set by SLURM:
    SLURM_JOB_ID, SLURM_JOB_USER, SLURM_JOB_PARTITION,
    SLURM_JOB_NUM_NODES, SLURM_CPUS_PER_TASK, SLURM_MEM_PER_NODE,
    SLURM_TIMELIMIT
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Configuration
DB_PATH = Path("/var/lib/nomade/nomade.db")
LOG_FILE = Path("/var/log/nomade/prolog.log")
ALERT_FILE = Path("/var/log/nomade/high_risk_jobs.log")
RISK_THRESHOLD = 0.7  # Alert if risk score > this


def setup_logging():
    """Setup logging to file."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )
    return logging.getLogger('nomade.prolog')


def get_job_features() -> dict:
    """Extract job features from SLURM environment variables."""
    return {
        'job_id': os.environ.get('SLURM_JOB_ID', '0'),
        'user': os.environ.get('SLURM_JOB_USER', 'unknown'),
        'partition': os.environ.get('SLURM_JOB_PARTITION', 'default'),
        'num_nodes': int(os.environ.get('SLURM_JOB_NUM_NODES', 1)),
        'cpus_per_task': int(os.environ.get('SLURM_CPUS_PER_TASK', 1)),
        'mem_per_node': parse_memory(os.environ.get('SLURM_MEM_PER_NODE', '0')),
        'time_limit': parse_time(os.environ.get('SLURM_TIMELIMIT', '01:00:00')),
        'gpus': int(os.environ.get('SLURM_GPUS', 0)),
    }


def parse_memory(mem_str: str) -> int:
    """Parse SLURM memory string to MB."""
    if not mem_str or mem_str == '0':
        return 0
    mem_str = mem_str.upper()
    if mem_str.endswith('G'):
        return int(float(mem_str[:-1]) * 1024)
    elif mem_str.endswith('M'):
        return int(float(mem_str[:-1]))
    elif mem_str.endswith('K'):
        return int(float(mem_str[:-1]) / 1024)
    return int(mem_str)


def parse_time(time_str: str) -> int:
    """Parse SLURM time string to seconds."""
    if not time_str:
        return 3600
    
    # Handle days-hours:minutes:seconds format
    if '-' in time_str:
        days, rest = time_str.split('-')
        days = int(days)
    else:
        days = 0
        rest = time_str
    
    parts = rest.split(':')
    if len(parts) == 3:
        hours, minutes, seconds = map(int, parts)
    elif len(parts) == 2:
        hours, minutes = map(int, parts)
        seconds = 0
    else:
        hours = int(parts[0])
        minutes = seconds = 0
    
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def score_job(features: dict, db_path: Path, logger) -> dict:
    """
    Score a job against historical patterns.
    
    Returns dict with:
        - risk_score: 0.0 (safe) to 1.0 (high risk)
        - similar_failures: count of similar past failures
        - recommendation: actionable advice
    """
    result = {
        'risk_score': 0.0,
        'similar_failures': 0,
        'similar_total': 0,
        'recommendation': None
    }
    
    if not db_path.exists():
        logger.warning(f"Database not found: {db_path}")
        return result
    
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        # Get historical failure rate for similar jobs
        cpus = features['cpus_per_task'] * features['num_nodes']
        mem = features['mem_per_node']
        time_sec = features['time_limit']
        
        # Find similar jobs (±20% tolerance)
        similar_query = """
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN failure_reason > 0 THEN 1 ELSE 0 END) as failures,
                SUM(CASE WHEN failure_reason = 1 THEN 1 ELSE 0 END) as timeouts,
                SUM(CASE WHEN failure_reason = 4 THEN 1 ELSE 0 END) as ooms
            FROM jobs
            WHERE req_cpus BETWEEN ? AND ?
              AND req_mem_mb BETWEEN ? AND ?
              AND req_time_seconds BETWEEN ? AND ?
        """
        
        cursor = conn.execute(similar_query, (
            max(1, cpus * 0.8), cpus * 1.2,
            max(1, mem * 0.8), mem * 1.2,
            max(1, time_sec * 0.8), time_sec * 1.2
        ))
        row = cursor.fetchone()
        
        if row and row['total'] > 0:
            result['similar_total'] = row['total']
            result['similar_failures'] = row['failures']
            result['risk_score'] = row['failures'] / row['total']
            
            # Generate specific recommendations
            if row['timeouts'] > row['failures'] * 0.5:
                result['recommendation'] = "Similar jobs often timeout - consider increasing time limit"
            elif row['ooms'] > row['failures'] * 0.3:
                result['recommendation'] = "Similar jobs often run out of memory - consider increasing memory"
        
        # Check user-specific patterns
        user_query = """
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN failure_reason > 0 THEN 1 ELSE 0 END) as failures
            FROM jobs
            WHERE user = ?
            ORDER BY submit_time DESC
            LIMIT 100
        """
        cursor = conn.execute(user_query, (features['user'],))
        user_row = cursor.fetchone()
        
        if user_row and user_row['total'] > 10:
            user_failure_rate = user_row['failures'] / user_row['total']
            # Blend: 70% similar jobs, 30% user history
            result['risk_score'] = 0.7 * result['risk_score'] + 0.3 * user_failure_rate
        
        conn.close()
        
    except Exception as e:
        logger.error(f"Scoring error: {e}")
    
    return result


def log_high_risk(features: dict, score: dict):
    """Log high-risk job for admin review."""
    ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    with open(ALERT_FILE, 'a') as f:
        entry = {
            'timestamp': datetime.now().isoformat(),
            'job_id': features['job_id'],
            'user': features['user'],
            'partition': features['partition'],
            'risk_score': round(score['risk_score'], 3),
            'similar_failures': score['similar_failures'],
            'similar_total': score['similar_total'],
            'recommendation': score['recommendation']
        }
        f.write(json.dumps(entry) + '\n')


def main():
    """Main entry point for prolog script."""
    logger = setup_logging()
    
    try:
        features = get_job_features()
        logger.info(f"Scoring job {features['job_id']} for user {features['user']}")
        
        score = score_job(features, DB_PATH, logger)
        
        logger.info(
            f"Job {features['job_id']}: risk={score['risk_score']:.2f}, "
            f"similar={score['similar_failures']}/{score['similar_total']}"
        )
        
        if score['risk_score'] > RISK_THRESHOLD:
            logger.warning(
                f"HIGH RISK: Job {features['job_id']} (user={features['user']}, "
                f"score={score['risk_score']:.2f})"
            )
            log_high_risk(features, score)
        
        # Always exit 0 - don't block jobs, just log
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"Prolog error: {e}")
        sys.exit(0)  # Don't block jobs on error


if __name__ == '__main__':
    main()
