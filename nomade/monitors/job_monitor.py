"""
NØMADE Job Monitor Daemon

Monitors running jobs to collect real-time I/O metrics.
Distinguishes NFS vs local storage writes by tracking file descriptors.
"""

import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ProcessIO:
    """I/O stats for a single process."""
    pid: int
    read_bytes: int = 0
    write_bytes: int = 0
    rchar: int = 0
    wchar: int = 0
    
    # Classified writes
    nfs_write_bytes: int = 0
    local_write_bytes: int = 0
    
    # Open files by filesystem type
    open_files: dict[str, list[str]] = field(default_factory=dict)


@dataclass 
class JobIOSnapshot:
    """I/O snapshot for a job at a point in time."""
    job_id: str
    timestamp: datetime
    pids: list[int]
    
    # Aggregated I/O
    total_read_bytes: int = 0
    total_write_bytes: int = 0
    nfs_write_bytes: int = 0
    local_write_bytes: int = 0
    
    # Process details
    processes: list[ProcessIO] = field(default_factory=list)
    
    @property
    def nfs_ratio(self) -> float:
        """Calculate NFS write ratio."""
        total = self.nfs_write_bytes + self.local_write_bytes
        if total == 0:
            return 0.0
        return self.nfs_write_bytes / total


class FilesystemClassifier:
    """Classifies paths as NFS or local storage."""
    
    def __init__(self, nfs_paths: list[str] = None, local_paths: list[str] = None):
        # Default classification (customize per cluster)
        self.nfs_prefixes = nfs_paths or [
            '/home',
            '/scratch', 
            '/nas',
            '/nfs',
            '/shared',
            '/project',
        ]
        self.local_prefixes = local_paths or [
            '/localscratch',
            '/local',
            '/tmp',
            '/dev/shm',
            '/run',
            '/var/tmp',
        ]
        
        # Cache resolved mount points
        self._mount_cache: dict[str, str] = {}
        self._refresh_mounts()
    
    def _refresh_mounts(self) -> None:
        """Parse /proc/mounts to identify NFS mounts."""
        try:
            with open('/proc/mounts', 'r') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3:
                        mount_point = parts[1]
                        fs_type = parts[2]
                        if fs_type in ('nfs', 'nfs4', 'cifs', 'lustre', 'gpfs', 'beegfs'):
                            self._mount_cache[mount_point] = 'nfs'
                        elif fs_type in ('ext4', 'xfs', 'btrfs', 'tmpfs', 'devtmpfs'):
                            self._mount_cache[mount_point] = 'local'
        except Exception as e:
            logger.debug(f"Could not read /proc/mounts: {e}")
    
    def classify(self, path: str) -> str:
        """
        Classify a path as 'nfs' or 'local'.
        
        Priority:
        1. Check actual mount points
        2. Fall back to path prefix heuristics
        """
        if not path:
            return 'unknown'
        
        # Resolve symlinks
        try:
            real_path = os.path.realpath(path)
        except Exception:
            real_path = path
        
        # Check against known mounts (longest match)
        best_match = ''
        best_type = None
        for mount_point, fs_type in self._mount_cache.items():
            if real_path.startswith(mount_point) and len(mount_point) > len(best_match):
                best_match = mount_point
                best_type = fs_type
        
        if best_type:
            return best_type
        
        # Fall back to prefix heuristics
        for prefix in self.nfs_prefixes:
            if real_path.startswith(prefix):
                return 'nfs'
        
        for prefix in self.local_prefixes:
            if real_path.startswith(prefix):
                return 'local'
        
        # Default to local for unknown paths
        return 'local'


class JobMonitor:
    """
    Monitors running SLURM jobs and collects I/O metrics.
    
    Configuration:
        sample_interval: Seconds between samples (default: 30)
        nfs_paths: List of paths to classify as NFS
        local_paths: List of paths to classify as local
    """
    
    def __init__(self, config: dict[str, Any], db_path: str):
        self.config = config
        self.db_path = Path(db_path)
        
        self.sample_interval = config.get('sample_interval', 30)
        
        # Initialize filesystem classifier
        self.classifier = FilesystemClassifier(
            nfs_paths=config.get('nfs_paths'),
            local_paths=config.get('local_paths'),
        )
        
        # Track previous I/O values to compute deltas
        self._prev_io: dict[str, dict[int, ProcessIO]] = {}  # job_id -> {pid -> ProcessIO}
        
        # Track job summaries
        self._job_summaries: dict[str, dict] = {}
        
        logger.info(f"JobMonitor: sample_interval={self.sample_interval}s")
    
    def get_running_jobs(self) -> list[str]:
        """Get list of running job IDs."""
        try:
            result = subprocess.run(
                ['squeue', '-h', '-t', 'R', '-o', '%i'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return [j.strip() for j in result.stdout.strip().split('\n') if j.strip()]
        except Exception as e:
            logger.error(f"Failed to get running jobs: {e}")
        return []
    
    def get_job_pids(self, job_id: str) -> list[int]:
        """Get PIDs for a running job."""
        try:
            result = subprocess.run(
                ['scontrol', 'listpids', job_id],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                pids = []
                for line in result.stdout.strip().split('\n')[1:]:  # Skip header
                    parts = line.split()
                    if parts and parts[0].isdigit():
                        pids.append(int(parts[0]))
                return pids
        except Exception as e:
            logger.debug(f"Failed to get PIDs for job {job_id}: {e}")
        return []
    
    def read_proc_io(self, pid: int) -> ProcessIO | None:
        """Read I/O stats from /proc/[pid]/io."""
        try:
            io_path = f'/proc/{pid}/io'
            if not os.path.exists(io_path):
                return None
            
            proc_io = ProcessIO(pid=pid)
            
            with open(io_path, 'r') as f:
                for line in f:
                    if ':' in line:
                        key, value = line.split(':', 1)
                        key = key.strip()
                        value = int(value.strip())
                        
                        if key == 'read_bytes':
                            proc_io.read_bytes = value
                        elif key == 'write_bytes':
                            proc_io.write_bytes = value
                        elif key == 'rchar':
                            proc_io.rchar = value
                        elif key == 'wchar':
                            proc_io.wchar = value
            
            return proc_io
            
        except PermissionError:
            logger.debug(f"Permission denied reading /proc/{pid}/io")
        except Exception as e:
            logger.debug(f"Error reading /proc/{pid}/io: {e}")
        return None
    
    def get_open_files(self, pid: int) -> dict[str, list[str]]:
        """
        Get open files for a process, classified by filesystem type.
        
        Returns: {'nfs': [paths...], 'local': [paths...]}
        """
        result = {'nfs': [], 'local': []}
        
        try:
            fd_dir = f'/proc/{pid}/fd'
            if not os.path.exists(fd_dir):
                return result
            
            for fd in os.listdir(fd_dir):
                try:
                    link = os.readlink(os.path.join(fd_dir, fd))
                    
                    # Skip non-file descriptors
                    if link.startswith(('pipe:', 'socket:', 'anon_inode:', '/dev/')):
                        continue
                    
                    # Classify the path
                    fs_type = self.classifier.classify(link)
                    if fs_type in result:
                        result[fs_type].append(link)
                        
                except (OSError, PermissionError):
                    continue
                    
        except PermissionError:
            logger.debug(f"Permission denied reading /proc/{pid}/fd")
        except Exception as e:
            logger.debug(f"Error reading /proc/{pid}/fd: {e}")
        
        return result
    
    def sample_job(self, job_id: str) -> JobIOSnapshot | None:
        """Take an I/O snapshot for a job."""
        pids = self.get_job_pids(job_id)
        if not pids:
            return None
        
        snapshot = JobIOSnapshot(
            job_id=job_id,
            timestamp=datetime.now(),
            pids=pids,
        )
        
        for pid in pids:
            proc_io = self.read_proc_io(pid)
            if not proc_io:
                continue
            
            # Get open files to classify writes
            open_files = self.get_open_files(pid)
            proc_io.open_files = open_files
            
            # Estimate NFS vs local based on open files
            # This is a heuristic: if job has NFS files open, attribute writes proportionally
            nfs_files = len(open_files.get('nfs', []))
            local_files = len(open_files.get('local', []))
            total_files = nfs_files + local_files
            
            if total_files > 0:
                nfs_ratio = nfs_files / total_files
                proc_io.nfs_write_bytes = int(proc_io.write_bytes * nfs_ratio)
                proc_io.local_write_bytes = int(proc_io.write_bytes * (1 - nfs_ratio))
            else:
                # Default to local if no files detected
                proc_io.local_write_bytes = proc_io.write_bytes
            
            snapshot.processes.append(proc_io)
            snapshot.total_read_bytes += proc_io.read_bytes
            snapshot.total_write_bytes += proc_io.write_bytes
            snapshot.nfs_write_bytes += proc_io.nfs_write_bytes
            snapshot.local_write_bytes += proc_io.local_write_bytes
        
        return snapshot
    
    def compute_delta(self, job_id: str, current: JobIOSnapshot) -> dict[str, int]:
        """Compute I/O delta since last sample."""
        delta = {
            'read_bytes': 0,
            'write_bytes': 0,
            'nfs_write_bytes': 0,
            'local_write_bytes': 0,
        }
        
        prev = self._prev_io.get(job_id, {})
        
        for proc in current.processes:
            prev_proc = prev.get(proc.pid)
            if prev_proc:
                delta['read_bytes'] += max(0, proc.read_bytes - prev_proc.read_bytes)
                delta['write_bytes'] += max(0, proc.write_bytes - prev_proc.write_bytes)
                delta['nfs_write_bytes'] += max(0, proc.nfs_write_bytes - prev_proc.nfs_write_bytes)
                delta['local_write_bytes'] += max(0, proc.local_write_bytes - prev_proc.local_write_bytes)
            else:
                # New process, count all as delta
                delta['read_bytes'] += proc.read_bytes
                delta['write_bytes'] += proc.write_bytes
                delta['nfs_write_bytes'] += proc.nfs_write_bytes
                delta['local_write_bytes'] += proc.local_write_bytes
        
        # Update previous state
        self._prev_io[job_id] = {proc.pid: proc for proc in current.processes}
        
        return delta
    
    def update_job_summary(self, job_id: str, snapshot: JobIOSnapshot, delta: dict) -> None:
        """Update running summary for a job."""
        if job_id not in self._job_summaries:
            self._job_summaries[job_id] = {
                'job_id': job_id,
                'first_sample': snapshot.timestamp,
                'sample_count': 0,
                'total_read_bytes': 0,
                'total_write_bytes': 0,
                'total_nfs_write_bytes': 0,
                'total_local_write_bytes': 0,
                'peak_write_rate': 0,  # bytes/sec
            }
        
        summary = self._job_summaries[job_id]
        summary['last_sample'] = snapshot.timestamp
        summary['sample_count'] += 1
        summary['total_read_bytes'] += delta['read_bytes']
        summary['total_write_bytes'] += delta['write_bytes']
        summary['total_nfs_write_bytes'] += delta['nfs_write_bytes']
        summary['total_local_write_bytes'] += delta['local_write_bytes']
        
        # Calculate write rate
        write_rate = delta['write_bytes'] / self.sample_interval
        if write_rate > summary['peak_write_rate']:
            summary['peak_write_rate'] = write_rate
    
    def store_snapshot(self, snapshot: JobIOSnapshot) -> None:
        """Store I/O snapshot in database."""
        try:
            conn = sqlite3.connect(self.db_path)
            
            # Ensure table exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS job_io_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    timestamp DATETIME NOT NULL,
                    total_read_bytes INTEGER,
                    total_write_bytes INTEGER,
                    nfs_write_bytes INTEGER,
                    local_write_bytes INTEGER,
                    nfs_ratio REAL,
                    pid_count INTEGER
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_job_io_samples_job 
                ON job_io_samples(job_id, timestamp)
            """)
            
            conn.execute(
                """
                INSERT INTO job_io_samples 
                (job_id, timestamp, total_read_bytes, total_write_bytes, 
                 nfs_write_bytes, local_write_bytes, nfs_ratio, pid_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.job_id,
                    snapshot.timestamp.isoformat(),
                    snapshot.total_read_bytes,
                    snapshot.total_write_bytes,
                    snapshot.nfs_write_bytes,
                    snapshot.local_write_bytes,
                    snapshot.nfs_ratio,
                    len(snapshot.pids),
                )
            )
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"Failed to store snapshot: {e}")
    
    def finalize_job(self, job_id: str) -> None:
        """Finalize job summary when job completes."""
        if job_id not in self._job_summaries:
            return
        
        summary = self._job_summaries[job_id]
        
        try:
            conn = sqlite3.connect(self.db_path)
            
            # Calculate final NFS ratio
            total_writes = summary['total_nfs_write_bytes'] + summary['total_local_write_bytes']
            nfs_ratio = summary['total_nfs_write_bytes'] / total_writes if total_writes > 0 else 0.0
            
            # Update job_summary table
            conn.execute(
                """
                UPDATE job_summary SET
                    total_nfs_write_gb = ?,
                    total_local_write_gb = ?,
                    nfs_ratio = ?
                WHERE job_id = ?
                """,
                (
                    summary['total_nfs_write_bytes'] / (1024**3),
                    summary['total_local_write_bytes'] / (1024**3),
                    nfs_ratio,
                    job_id,
                )
            )
            
            conn.commit()
            conn.close()
            
            logger.info(f"Finalized job {job_id}: nfs_ratio={nfs_ratio:.2%}, "
                       f"total_write={summary['total_write_bytes']/(1024**2):.1f}MB")
            
            # Cleanup
            del self._job_summaries[job_id]
            if job_id in self._prev_io:
                del self._prev_io[job_id]
                
        except Exception as e:
            logger.error(f"Failed to finalize job {job_id}: {e}")
    
    def sample_all_jobs(self) -> int:
        """Sample all running jobs. Returns number of jobs sampled."""
        running_jobs = set(self.get_running_jobs())
        
        # Check for completed jobs
        tracked_jobs = set(self._job_summaries.keys())
        completed = tracked_jobs - running_jobs
        for job_id in completed:
            self.finalize_job(job_id)
        
        # Sample running jobs
        sampled = 0
        for job_id in running_jobs:
            snapshot = self.sample_job(job_id)
            if snapshot:
                delta = self.compute_delta(job_id, snapshot)
                self.update_job_summary(job_id, snapshot, delta)
                self.store_snapshot(snapshot)
                sampled += 1
                
                logger.debug(f"Job {job_id}: write_delta={delta['write_bytes']/(1024**2):.1f}MB, "
                           f"nfs_ratio={snapshot.nfs_ratio:.1%}")
        
        return sampled
    
    def run(self, once: bool = False) -> None:
        """Run the monitor loop."""
        logger.info(f"Starting job monitor (interval={self.sample_interval}s)")
        
        try:
            while True:
                sampled = self.sample_all_jobs()
                logger.info(f"Sampled {sampled} jobs")
                
                if once:
                    break
                
                time.sleep(self.sample_interval)
                
        except KeyboardInterrupt:
            logger.info("Stopping job monitor")
            
            # Finalize any remaining jobs
            for job_id in list(self._job_summaries.keys()):
                self.finalize_job(job_id)


def main():
    """CLI entry point for standalone execution."""
    import argparse
    
    parser = argparse.ArgumentParser(description='NØMADE Job Monitor Daemon')
    parser.add_argument('--db', default='/var/lib/nomade/nomade.db', help='Database path')
    parser.add_argument('--interval', type=int, default=30, help='Sample interval (seconds)')
    parser.add_argument('--once', action='store_true', help='Run once and exit')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    parser.add_argument('--nfs-paths', nargs='+', help='Paths to classify as NFS')
    parser.add_argument('--local-paths', nargs='+', help='Paths to classify as local')
    
    args = parser.parse_args()
    
    # Setup logging
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )
    
    # Build config
    config = {
        'sample_interval': args.interval,
    }
    if args.nfs_paths:
        config['nfs_paths'] = args.nfs_paths
    if args.local_paths:
        config['local_paths'] = args.local_paths
    
    # Run monitor
    monitor = JobMonitor(config, args.db)
    monitor.run(once=args.once)


if __name__ == '__main__':
    main()
