"""
NØMADE Community Export Module

Provides anonymized data export for the NØMADE Community Dataset.
Ensures privacy through pseudonymization while preserving analytical value.
"""

import hashlib
import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict, List, Any
import sqlite3

try:
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq
    HAS_ARROW = True
except ImportError:
    HAS_ARROW = False


# Exit code to failure type mapping
EXIT_CODE_TO_FAILURE = {
    0: 'success',
    1: 'failed',
    2: 'timeout',
    3: 'oom',
    4: 'segfault',
    5: 'node_fail',
    6: 'cancelled',
}

# Partition name to category mapping (add your partitions here)
PARTITION_CATEGORIES = {
    'compute': 'cpu',
    'cpu': 'cpu',
    'highmem': 'highmem',
    'gpu': 'gpu',
    'debug': 'debug',
}


def generate_salt() -> str:
    """Generate a random salt for pseudonymization."""
    import secrets
    return secrets.token_hex(32)


def pseudonymize(value: str, salt: str, prefix: str = "id") -> str:
    """
    Convert a value to a consistent pseudonymized ID.
    
    Same value + same salt = same output (deterministic)
    Different salt = different output (can't correlate across institutions)
    """
    if not value:
        return f"{prefix}_unknown"
    combined = f"{salt}:{value}"
    hash_hex = hashlib.sha256(combined.encode()).hexdigest()[:8]
    return f"{prefix}_{hash_hex}"


def round_timestamp(ts: datetime) -> date:
    """Round timestamp to day precision for privacy."""
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except:
            return date.today()
    if isinstance(ts, datetime):
        return ts.date()
    return ts


def categorize_partition(partition_name: str) -> str:
    """Convert partition name to generic category."""
    if not partition_name:
        return 'other'
    partition_lower = partition_name.lower()
    for key, category in PARTITION_CATEGORIES.items():
        if key in partition_lower:
            return category
    return 'other'


def get_failure_type(exit_code: int, failure_reason: Any = None) -> str:
    """Convert exit code to failure type string."""
    if exit_code == 0:
        return 'success'
    if isinstance(failure_reason, str) and failure_reason:
        return failure_reason.lower()
    return EXIT_CODE_TO_FAILURE.get(exit_code, 'unknown')


def classify_resource_profile(job: dict) -> str:
    """Classify job's resource usage profile."""
    cpu_eff = job.get('cpu_efficiency', 0) or 0
    mem_eff = job.get('mem_efficiency', 0) or 0
    gpu_util = job.get('gpu_util', 0) or 0
    io_wait = job.get('io_wait_pct', 0) or 0
    
    if gpu_util > 0.5:
        return 'gpu_intensive'
    if io_wait > 30:
        return 'io_intensive'
    if mem_eff > 0.8 and cpu_eff < 0.5:
        return 'memory_intensive'
    if cpu_eff > 0.8:
        return 'cpu_intensive'
    if cpu_eff < 0.2 and mem_eff < 0.2:
        return 'minimal'
    return 'balanced'


def anonymize_job(job: dict, salt: str) -> dict:
    """
    Anonymize a single job record.
    
    Removes: real user/job IDs, paths, node names
    Keeps: metrics, efficiency ratios, failure info
    Pseudonymizes: user_id, job_id (consistent within institution)
    """
    exit_code = job.get('exit_code', 0) or job.get('failure_reason', 0) or 0
    if isinstance(exit_code, str):
        exit_code = 1  # Non-zero for string failure reasons
        
    return {
        'job_id': pseudonymize(str(job.get('job_id', '')), salt, 'job'),
        'user_id': pseudonymize(str(job.get('user_name', job.get('user', job.get('user_id', '')))), salt, 'user'),
        'timestamp': round_timestamp(job.get('submit_time', job.get('timestamp', datetime.now()))),
        'partition_type': categorize_partition(job.get('partition', '')),
        'runtime_sec': float(job.get('runtime_sec', 0) or 0),
        'walltime_sec': float(job.get('walltime_sec', job.get('timelimit_sec', 0)) or 0),
        'cpu_count': int(job.get('cpu_count', job.get('ncpus', 1)) or 1),
        'mem_mb': float(job.get('mem_mb', job.get('req_mem_mb', 0)) or 0),
        'gpu_count': int(job.get('gpu_count', job.get('ngpus', 0)) or 0),
        'cpu_efficiency': float(job.get('cpu_efficiency', 0) or 0),
        'mem_efficiency': float(job.get('mem_efficiency', 0) or 0),
        'gpu_efficiency': float(job.get('gpu_util', job.get('gpu_efficiency', 0)) or 0),
        'nfs_write_gb': float(job.get('nfs_write_gb', 0) or 0),
        'local_write_gb': float(job.get('local_write_gb', 0) or 0),
        'nfs_ratio': float(job.get('nfs_ratio', 0) or 0),
        'io_wait_pct': float(job.get('io_wait_pct', job.get('avg_iowait', 0)) or 0),
        'exit_code': int(exit_code) if isinstance(exit_code, (int, float)) else 1,
        'success': bool(job.get('success', exit_code == 0)),
        'failure_type': get_failure_type(
            exit_code if isinstance(exit_code, int) else 1,
            job.get('failure_reason')
        ),
        'health_score': float(job.get('health_score', 0) or 0),
    }


def compute_user_stats(jobs: List[dict]) -> Dict[str, dict]:
    """Compute per-user aggregate statistics."""
    user_jobs = {}
    for job in jobs:
        uid = job['user_id']
        if uid not in user_jobs:
            user_jobs[uid] = []
        user_jobs[uid].append(job)
    
    user_stats = {}
    for uid, ujobs in user_jobs.items():
        successes = sum(1 for j in ujobs if j['success'])
        failures = [j['failure_type'] for j in ujobs if not j['success']]
        failure_counts = {}
        for f in failures:
            failure_counts[f] = failure_counts.get(f, 0) + 1
        
        # Get most common failure types
        common_failures = sorted(failure_counts.items(), key=lambda x: -x[1])[:3]
        
        # Determine resource profile
        profiles = [classify_resource_profile(j) for j in ujobs]
        profile_counts = {}
        for p in profiles:
            profile_counts[p] = profile_counts.get(p, 0) + 1
        dominant_profile = max(profile_counts.items(), key=lambda x: x[1])[0]
        
        user_stats[uid] = {
            'job_count': len(ujobs),
            'success_rate': round(successes / len(ujobs), 3) if ujobs else 0,
            'avg_runtime_sec': round(sum(j['runtime_sec'] for j in ujobs) / len(ujobs), 1) if ujobs else 0,
            'common_failure_types': [f[0] for f in common_failures],
            'resource_profile': dominant_profile,
        }
    
    return user_stats


def load_jobs_from_db(db_path: Path, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[dict]:
    """Load jobs from NØMADE database."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    query = "SELECT * FROM jobs WHERE 1=1"
    params = []
    
    if start_date:
        query += " AND submit_time >= ?"
        params.append(start_date)
    if end_date:
        query += " AND submit_time <= ?"
        params.append(end_date)
    
    cursor = conn.execute(query, params)
    jobs = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return jobs


def export_community_data(
    db_path: Path,
    output_path: Path,
    salt: str,
    institution_type: str = "academic",
    cluster_type: str = "mixed_small",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    min_jobs: int = 100,
) -> dict:
    """
    Export anonymized community dataset.
    
    Args:
        db_path: Path to NØMADE database
        output_path: Output file path (.parquet or .json)
        salt: Institution-specific salt for pseudonymization
        institution_type: academic, government, industry, nonprofit
        cluster_type: e.g., gpu_small, mixed_medium
        start_date: Optional start date filter (YYYY-MM-DD)
        end_date: Optional end date filter (YYYY-MM-DD)
        min_jobs: Minimum jobs required (default 100)
    
    Returns:
        Summary statistics dict
    """
    # Load jobs
    print(f"Loading jobs from {db_path}...")
    jobs = load_jobs_from_db(db_path, start_date, end_date)
    
    if len(jobs) < min_jobs:
        raise ValueError(f"Insufficient data: {len(jobs)} jobs (minimum {min_jobs} required)")
    
    # Anonymize
    print(f"Anonymizing {len(jobs)} jobs...")
    anon_jobs = [anonymize_job(job, salt) for job in jobs]
    
    # Compute statistics
    user_stats = compute_user_stats(anon_jobs)
    
    successes = sum(1 for j in anon_jobs if j['success'])
    failure_types = {}
    for j in anon_jobs:
        if not j['success']:
            ft = j['failure_type']
            failure_types[ft] = failure_types.get(ft, 0) + 1
    
    # Get date range
    dates = [j['timestamp'] for j in anon_jobs]
    date_min = min(dates) if dates else date.today()
    date_max = max(dates) if dates else date.today()
    
    # Build export structure
    institution_id = pseudonymize(salt[:16], "nomade_community_2025", "inst")
    
    export_data = {
        'version': '1.0',
        'institution': {
            'id': institution_id,
            'type': institution_type,
            'cluster_type': cluster_type,
        },
        'export_date': date.today().isoformat(),
        'date_range': {
            'start': date_min.isoformat() if isinstance(date_min, date) else str(date_min),
            'end': date_max.isoformat() if isinstance(date_max, date) else str(date_max),
        },
        'summary': {
            'total_jobs': len(anon_jobs),
            'total_users': len(user_stats),
            'success_rate': round(successes / len(anon_jobs), 3),
            'failure_types': failure_types,
        },
        'users': user_stats,
        'jobs': anon_jobs,
    }
    
    # Save output
    output_path = Path(output_path)
    
    if output_path.suffix == '.parquet' and HAS_ARROW:
        print(f"Saving to {output_path} (Parquet)...")
        df = pd.DataFrame(anon_jobs)
        # Convert date objects to strings for parquet
        df['timestamp'] = df['timestamp'].astype(str)
        df.to_parquet(output_path, index=False)
        
        # Also save metadata
        meta_path = output_path.with_suffix('.meta.json')
        meta = {k: v for k, v in export_data.items() if k != 'jobs'}
        with open(meta_path, 'w') as f:
            json.dump(meta, f, indent=2, default=str)
        print(f"Metadata saved to {meta_path}")
    else:
        # JSON fallback
        if output_path.suffix != '.json':
            output_path = output_path.with_suffix('.json')
        print(f"Saving to {output_path} (JSON)...")
        with open(output_path, 'w') as f:
            json.dump(export_data, f, indent=2, default=str)
    
    print(f"\n✓ Export complete!")
    print(f"  Institution ID: {institution_id}")
    print(f"  Jobs: {len(anon_jobs)}")
    print(f"  Users: {len(user_stats)} (pseudonymized)")
    print(f"  Success rate: {export_data['summary']['success_rate']*100:.1f}%")
    print(f"  Date range: {export_data['date_range']['start']} to {export_data['date_range']['end']}")
    
    return export_data['summary']


def verify_export(file_path: Path) -> dict:
    """Verify an export file meets community standards."""
    file_path = Path(file_path)
    issues = []
    warnings = []
    
    # Load data
    if file_path.suffix == '.parquet':
        if not HAS_ARROW:
            raise ImportError("pyarrow required for parquet files")
        df = pd.read_parquet(file_path)
        data = df.to_dict('records')
    else:
        with open(file_path) as f:
            export = json.load(f)
        data = export.get('jobs', [])
    
    # Check minimum jobs
    if len(data) < 100:
        issues.append(f"Insufficient jobs: {len(data)} (minimum 100)")
    
    # Check for real usernames (common patterns)
    suspicious_users = []
    for job in data[:100]:  # Sample check
        uid = job.get('user_id', '')
        if not uid.startswith('user_') or len(uid) != 13:
            suspicious_users.append(uid)
    if suspicious_users:
        issues.append(f"Non-pseudonymized user IDs found: {suspicious_users[:3]}")
    
    # Check for paths
    for job in data[:100]:
        for key, val in job.items():
            if isinstance(val, str) and ('/' in val or '\\' in val):
                if key not in ['timestamp']:
                    warnings.append(f"Possible path in field '{key}': {val[:50]}")
    
    # Check timestamp precision
    timestamps = [job.get('timestamp', '') for job in data[:100]]
    for ts in timestamps:
        if isinstance(ts, str) and 'T' in ts:
            warnings.append("Timestamps have time precision (should be date only)")
            break
    
    result = {
        'valid': len(issues) == 0,
        'issues': issues,
        'warnings': warnings,
        'stats': {
            'jobs': len(data),
            'users': len(set(j.get('user_id', '') for j in data)),
        }
    }
    
    print("\n=== Export Verification ===")
    print(f"File: {file_path}")
    print(f"Jobs: {result['stats']['jobs']}")
    print(f"Users: {result['stats']['users']}")
    
    if issues:
        print("\n❌ ISSUES (must fix):")
        for issue in issues:
            print(f"  - {issue}")
    
    if warnings:
        print("\n⚠️  WARNINGS (review):")
        for warn in warnings[:5]:
            print(f"  - {warn}")
    
    if result['valid']:
        print("\n✓ Export is valid for community submission")
    else:
        print("\n✗ Export has issues that must be resolved")
    
    return result


def preview_export(file_path: Path, n_samples: int = 5):
    """Preview an export file."""
    file_path = Path(file_path)
    
    if file_path.suffix == '.parquet':
        if not HAS_ARROW:
            raise ImportError("pyarrow required for parquet files")
        df = pd.read_parquet(file_path)
        
        # Load metadata if exists
        meta_path = file_path.with_suffix('.meta.json')
        if meta_path.exists():
            with open(meta_path) as f:
                meta = json.load(f)
        else:
            meta = {}
    else:
        with open(file_path) as f:
            export = json.load(f)
        df = pd.DataFrame(export.get('jobs', []))
        meta = {k: v for k, v in export.items() if k != 'jobs'}
    
    print("\n=== Export Preview ===")
    print(f"File: {file_path}")
    
    if meta:
        print(f"\nInstitution ID: {meta.get('institution', {}).get('id', 'N/A')}")
        print(f"Cluster Type: {meta.get('institution', {}).get('cluster_type', 'N/A')}")
        print(f"Date Range: {meta.get('date_range', {}).get('start', 'N/A')} to {meta.get('date_range', {}).get('end', 'N/A')}")
        
        summary = meta.get('summary', {})
        print(f"\nTotal Jobs: {summary.get('total_jobs', len(df))}")
        print(f"Total Users: {summary.get('total_users', 'N/A')} (pseudonymized)")
        print(f"Success Rate: {summary.get('success_rate', 0)*100:.1f}%")
        
        if summary.get('failure_types'):
            print("\nFailure Types:")
            for ft, count in sorted(summary['failure_types'].items(), key=lambda x: -x[1])[:5]:
                print(f"  {ft}: {count}")
    
    print(f"\nSample Records ({n_samples}):")
    print(df[['job_id', 'user_id', 'timestamp', 'success', 'failure_type']].head(n_samples).to_string(index=False))


# CLI integration
if __name__ == '__main__':
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python community.py <command> [args]")
        print("Commands: export, verify, preview")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == 'export':
        # Example usage
        export_community_data(
            db_path=Path("nomade.db"),
            output_path=Path("community_export.parquet"),
            salt="test-salt-change-this",
            institution_type="academic",
            cluster_type="mixed_small",
        )
    elif cmd == 'verify':
        verify_export(Path(sys.argv[2]))
    elif cmd == 'preview':
        preview_export(Path(sys.argv[2]))
