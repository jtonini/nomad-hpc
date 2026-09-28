#!/usr/bin/env python3
"""
NØMAÐ Metrics Simulator

Generates realistic disk and storage metrics for simulated clusters.
Allows testing NØMAÐ's analysis on large clusters without running 300 VMs.

Simulates:
- Per-node /localscratch usage (based on running jobs)
- Cluster-wide shared storage (/home, /scratch, /project)
- Realistic fill patterns and I/O behavior

Usage:
    python metrics-generator.py --config small --duration 3600
    python metrics-generator.py --config large --output metrics.json
    python metrics-generator.py --replay sample-data.json
"""

import argparse
import json
import logging
import random
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)


# ============================================
# CLUSTER CONFIGURATIONS
# ============================================

@dataclass
class NodeSpec:
    """Specification for a node type."""
    prefix: str
    count: int
    cores: int
    mem_gb: int
    local_storage_gb: int
    gpus: int = 0


@dataclass
class StorageSpec:
    """Specification for shared storage."""
    path: str
    total_tb: float
    baseline_used_pct: float  # Typical usage
    volatility: float  # How much usage varies (0-1)


CLUSTER_CONFIGS = {
    'minimal': {
        'nodes': [
            NodeSpec('nomad-test', 1, 4, 8, 100, 0),
        ],
        'storage': [
            StorageSpec('/', 100, 0.10, 0.05),
            StorageSpec('/home', 10, 0.50, 0.10),
            StorageSpec('/scratch', 50, 0.30, 0.40),
            StorageSpec('/localscratch', 2, 0.00, 0.90),
        ],
    },
    'small': {
        'nodes': [
            NodeSpec('cpu', 8, 64, 512, 1000, 0),
            NodeSpec('highmem', 2, 64, 1536, 1000, 0),
            NodeSpec('gpu', 8, 32, 256, 2000, 4),
        ],
        'storage': [
            StorageSpec('/home', 10, 0.60, 0.10),
            StorageSpec('/scratch', 50, 0.40, 0.50),
            StorageSpec('/project', 40, 0.75, 0.15),
        ],
    },
    'large': {
        'nodes': [
            NodeSpec('cpu', 150, 64, 512, 1000, 0),
            NodeSpec('highmem', 50, 64, 1536, 1000, 0),
            NodeSpec('gpu', 100, 32, 256, 2000, 4),
        ],
        'storage': [
            StorageSpec('/home', 100, 0.60, 0.08),
            StorageSpec('/scratch', 5000, 0.40, 0.50),
            StorageSpec('/project', 500, 0.75, 0.12),
            StorageSpec('/archive', 4400, 0.85, 0.05),
        ],
    },
}


# ============================================
# SIMULATED JOBS
# ============================================

@dataclass
class SimulatedJob:
    """A simulated job running on the cluster."""
    job_id: int
    node: str
    start_time: datetime
    duration_sec: int
    local_write_mb: float
    nfs_write_mb: float
    is_bad_io: bool
    
    @property
    def end_time(self) -> datetime:
        return self.start_time + timedelta(seconds=self.duration_sec)
    
    def local_usage_at(self, t: datetime) -> float:
        """Calculate local storage usage at time t (in MB)."""
        if t < self.start_time or t > self.end_time:
            return 0
        
        elapsed = (t - self.start_time).total_seconds()
        progress = elapsed / self.duration_sec
        
        # Jobs write data gradually, peak near middle
        if progress < 0.8:
            return self.local_write_mb * (progress / 0.8)
        else:
            # Cleanup phase - data decreases
            cleanup_progress = (progress - 0.8) / 0.2
            return self.local_write_mb * (1 - cleanup_progress * 0.5)


# ============================================
# METRICS GENERATOR
# ============================================

class MetricsGenerator:
    """Generates simulated metrics for a cluster."""
    
    def __init__(self, config: str = 'minimal'):
        self.config_name = config
        self.config = CLUSTER_CONFIGS.get(config, CLUSTER_CONFIGS['minimal'])
        
        # Build node list
        self.nodes = []
        for spec in self.config['nodes']:
            if spec.count == 1 and spec.prefix == 'nomad-test':
                self.nodes.append(spec.prefix)
            else:
                for i in range(1, spec.count + 1):
                    if spec.count >= 100:
                        self.nodes.append(f"{spec.prefix}{i:03d}")
                    else:
                        self.nodes.append(f"{spec.prefix}{i:02d}")
        
        self.storage = {s.path: s for s in self.config['storage']}
        
        # Simulation state
        self.current_time = datetime.now()
        self.jobs: list[SimulatedJob] = []
        self.job_counter = 0
        self.node_local_usage: dict[str, float] = dict.fromkeys(self.nodes, 0)
        self.storage_usage: dict[str, float] = {}
        
        # Initialize storage with baseline
        for path, spec in self.storage.items():
            self.storage_usage[path] = spec.baseline_used_pct
        
        logger.info(f"Initialized '{config}' cluster: {len(self.nodes)} nodes, "
                   f"{len(self.storage)} filesystems")
    
    def _get_node_spec(self, node: str) -> NodeSpec | None:
        """Get the spec for a node."""
        for spec in self.config['nodes']:
            if node.startswith(spec.prefix):
                return spec
        return None
    
    def spawn_job(self) -> SimulatedJob:
        """Create a new simulated job."""
        self.job_counter += 1
        
        # Random node
        node = random.choice(self.nodes)
        spec = self._get_node_spec(node)
        
        # Job characteristics
        is_bad_io = random.random() < 0.15  # 15% bad I/O jobs
        
        if is_bad_io:
            local_write_mb = random.uniform(0, 100)
            nfs_write_mb = random.uniform(1000, 10000)
            duration_sec = random.randint(600, 3600)
        else:
            local_write_mb = random.uniform(100, spec.local_storage_gb * 0.3 * 1024 if spec else 500)
            nfs_write_mb = random.uniform(10, 500)
            duration_sec = random.randint(60, 1800)
        
        job = SimulatedJob(
            job_id=self.job_counter,
            node=node,
            start_time=self.current_time,
            duration_sec=duration_sec,
            local_write_mb=local_write_mb,
            nfs_write_mb=nfs_write_mb,
            is_bad_io=is_bad_io,
        )
        
        self.jobs.append(job)
        return job
    
    def cleanup_finished_jobs(self):
        """Remove finished jobs."""
        active = []
        for job in self.jobs:
            if self.current_time < job.end_time:
                active.append(job)
        self.jobs = active
    
    def calculate_metrics(self) -> dict:
        """Calculate current metrics for all nodes and storage."""
        self.cleanup_finished_jobs()
        
        metrics = {
            'timestamp': self.current_time.isoformat(),
            'cluster': self.config_name,
            'nodes': {},
            'storage': {},
            'jobs': {
                'running': len(self.jobs),
                'bad_io': sum(1 for j in self.jobs if j.is_bad_io),
            },
        }
        
        # Per-node local storage
        for node in self.nodes:
            spec = self._get_node_spec(node)
            local_capacity_mb = spec.local_storage_gb * 1024 if spec else 100 * 1024
            
            # Sum local usage from all jobs on this node
            usage_mb = sum(
                j.local_usage_at(self.current_time)
                for j in self.jobs if j.node == node
            )
            
            usage_pct = min(100, (usage_mb / local_capacity_mb) * 100)
            
            metrics['nodes'][node] = {
                'localscratch_used_mb': usage_mb,
                'localscratch_used_pct': usage_pct,
                'localscratch_total_gb': spec.local_storage_gb if spec else 100,
                'running_jobs': sum(1 for j in self.jobs if j.node == node),
            }
        
        # Shared storage
        for path, spec in self.storage.items():
            # Add some random variation
            variation = random.gauss(0, spec.volatility * 5)
            usage_pct = max(0, min(100, spec.baseline_used_pct * 100 + variation))
            
            # Jobs contribute to /scratch
            if path == '/scratch':
                nfs_contribution = sum(j.nfs_write_mb for j in self.jobs) / (spec.total_tb * 1024 * 1024) * 100
                usage_pct = min(100, usage_pct + nfs_contribution)
            
            total_bytes = spec.total_tb * 1024 ** 4
            used_bytes = total_bytes * (usage_pct / 100)
            
            metrics['storage'][path] = {
                'total_tb': spec.total_tb,
                'used_pct': usage_pct,
                'used_tb': spec.total_tb * usage_pct / 100,
                'available_tb': spec.total_tb * (1 - usage_pct / 100),
            }
        
        return metrics
    
    def step(self, seconds: int = 5):
        """Advance simulation by given seconds."""
        self.current_time += timedelta(seconds=seconds)
        
        # Randomly spawn new jobs
        # Rate depends on cluster size
        jobs_per_hour = len(self.nodes) * 0.5  # ~0.5 jobs/node/hour
        spawn_prob = (jobs_per_hour / 3600) * seconds
        
        while random.random() < spawn_prob:
            self.spawn_job()
            spawn_prob -= 1
    
    def run(self, duration_sec: int = 3600, interval_sec: int = 5,
            output_file: Path | None = None, realtime: bool = False):
        """Run simulation for specified duration."""
        
        logger.info(f"Running simulation for {duration_sec}s ({duration_sec/3600:.1f}h)")
        
        results = []
        steps = duration_sec // interval_sec
        
        for i in range(steps):
            self.step(interval_sec)
            metrics = self.calculate_metrics()
            results.append(metrics)
            
            if realtime:
                # Print summary
                if i % 12 == 0:  # Every minute at 5s intervals
                    running = metrics['jobs']['running']
                    bad_io = metrics['jobs']['bad_io']
                    
                    # Find busiest nodes
                    busiest = sorted(
                        metrics['nodes'].items(),
                        key=lambda x: x[1]['localscratch_used_pct'],
                        reverse=True
                    )[:3]
                    
                    logger.info(
                        f"t={i*interval_sec:5}s | jobs: {running:3} ({bad_io} bad_io) | "
                        f"busiest: {busiest[0][0]}={busiest[0][1]['localscratch_used_pct']:.1f}%"
                        if busiest else ""
                    )
                
                time.sleep(0.01)  # Small delay for readability
        
        if output_file:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, 'w') as f:
                json.dump(results, f, indent=2)
            logger.info(f"Saved {len(results)} metrics to {output_file}")
        
        return results
    
    def generate_sample_data(self, output_dir: Path):
        """Generate sample datasets for testing."""
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # 1-hour sample with 5-second intervals
        logger.info("Generating 1-hour sample (720 points)...")
        results_1h = self.run(3600, interval_sec=5)
        with open(output_dir / f'{self.config_name}-1h.json', 'w') as f:
            json.dump(results_1h, f)
        
        # Reset and generate 24-hour sample with 1-minute intervals
        self.__init__(self.config_name)
        logger.info("Generating 24-hour sample (1440 points)...")
        results_24h = self.run(86400, interval_sec=60)
        with open(output_dir / f'{self.config_name}-24h.json', 'w') as f:
            json.dump(results_24h, f)
        
        logger.info(f"Sample data saved to {output_dir}")


# ============================================
# MAIN
# ============================================

def main():
    parser = argparse.ArgumentParser(
        description="Generate simulated metrics for NØMAÐ testing"
    )
    
    parser.add_argument(
        '--config', '-c',
        choices=['minimal', 'small', 'large'],
        default='minimal',
        help='Cluster configuration (default: minimal)'
    )
    parser.add_argument(
        '--duration', '-d',
        type=int, default=3600,
        help='Simulation duration in seconds (default: 3600)'
    )
    parser.add_argument(
        '--interval', '-i',
        type=int, default=5,
        help='Metrics collection interval in seconds (default: 5)'
    )
    parser.add_argument(
        '--output', '-o',
        type=Path,
        help='Output file for metrics JSON'
    )
    parser.add_argument(
        '--generate-samples',
        action='store_true',
        help='Generate sample datasets'
    )
    parser.add_argument(
        '--realtime',
        action='store_true',
        help='Show progress in real-time'
    )
    parser.add_argument(
        '--summary',
        action='store_true',
        help='Print cluster summary and exit'
    )
    
    args = parser.parse_args()
    
    generator = MetricsGenerator(args.config)
    
    if args.summary:
        print(f"\n{'='*60}")
        print(f"Cluster: {args.config}")
        print(f"{'='*60}")
        print(f"\nNodes ({len(generator.nodes)} total):")
        for spec in generator.config['nodes']:
            gpu_str = f", {spec.gpus} GPUs" if spec.gpus > 0 else ""
            print(f"  {spec.prefix}[01-{spec.count:02d}]: {spec.cores}c, "
                  f"{spec.mem_gb}GB RAM, {spec.local_storage_gb}GB local{gpu_str}")
        
        print("\nShared Storage:")
        for spec in generator.config['storage']:
            print(f"  {spec.path:15} {spec.total_tb:>8.0f} TB  "
                  f"(~{spec.baseline_used_pct*100:.0f}% typical)")
        print()
        return
    
    if args.generate_samples:
        sample_dir = Path('sample-data')
        generator.generate_sample_data(sample_dir)
        return
    
    output_file = args.output or Path(f'{args.config}-metrics.json')
    generator.run(
        duration_sec=args.duration,
        interval_sec=args.interval,
        output_file=output_file,
        realtime=args.realtime,
    )


if __name__ == '__main__':
    main()
