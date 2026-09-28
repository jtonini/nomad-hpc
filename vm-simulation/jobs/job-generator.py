#!/usr/bin/env python3
"""
NØMAÐ Synthetic Job Generator

Generates realistic HPC job submissions for testing NØMAÐ.
Jobs are created based on profiles that mimic real cluster workloads.

Supports multiple cluster configurations:
  - minimal: Single node for quick testing
  - small:   18 nodes (8 cpu + 2 highmem + 8 gpu)
  - large:   300 nodes (150 cpu + 50 highmem + 100 gpu)

Usage:
    python job-generator.py                       # Default: 10 jobs/hour
    python job-generator.py --config small        # Use small cluster profiles
    python job-generator.py --config large        # Use large cluster profiles
    python job-generator.py --rate 20             # 20 jobs/hour
    python job-generator.py --once 10             # Submit 10 jobs and exit
    python job-generator.py --dry-run             # Preview without submitting
    python job-generator.py --list-profiles       # Show all profiles
"""

import argparse
import logging
import os
import random
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)


# ============================================
# JOB PROFILES
# ============================================

@dataclass
class JobProfile:
    """Definition of a job type."""
    name: str
    weight: float  # Relative frequency (0-1)
    partition: str  # SLURM partition
    
    # Resource requirements
    cpus_min: int
    cpus_max: int
    mem_gb_min: float
    mem_gb_max: float
    
    # Time (minutes)
    time_min: int
    time_max: int
    
    # GPU
    gpus: int = 0
    gpu_type: str = "a100"
    
    # I/O behavior (MB)
    nfs_write_mb_min: float = 0
    nfs_write_mb_max: float = 100
    local_write_mb_min: float = 0
    local_write_mb_max: float = 500
    
    # Failure probability
    base_failure_rate: float = 0.05
    
    # Description for logging
    description: str = ""


# -----------------------------------------------------------------------------
# Minimal Cluster Profiles (single node, 4 cores for VM)
# -----------------------------------------------------------------------------
PROFILES_MINIMAL = [
    JobProfile(
        name="quick",
        weight=0.40,
        partition="standard",
        cpus_min=1, cpus_max=2,
        mem_gb_min=1, mem_gb_max=4,
        time_min=1, time_max=10,
        local_write_mb_min=0, local_write_mb_max=500,
        nfs_write_mb_min=0, nfs_write_mb_max=50,
        base_failure_rate=0.05,
        description="Quick test jobs",
    ),
    JobProfile(
        name="medium",
        weight=0.30,
        partition="standard",
        cpus_min=2, cpus_max=4,
        mem_gb_min=2, mem_gb_max=6,
        time_min=10, time_max=60,
        local_write_mb_min=100, local_write_mb_max=1000,
        nfs_write_mb_min=10, nfs_write_mb_max=100,
        base_failure_rate=0.08,
        description="Medium compute jobs",
    ),
    JobProfile(
        name="bad_io",
        weight=0.15,
        partition="standard",
        cpus_min=1, cpus_max=4,
        mem_gb_min=2, mem_gb_max=6,
        time_min=10, time_max=60,
        local_write_mb_min=0, local_write_mb_max=50,
        nfs_write_mb_min=500, nfs_write_mb_max=2000,
        base_failure_rate=0.25,
        description="BAD: Heavy NFS writes",
    ),
    JobProfile(
        name="debug",
        weight=0.15,
        partition="debug",
        cpus_min=1, cpus_max=2,
        mem_gb_min=1, mem_gb_max=2,
        time_min=1, time_max=5,
        local_write_mb_min=0, local_write_mb_max=100,
        base_failure_rate=0.02,
        description="Debug/test jobs",
    ),
]


# -----------------------------------------------------------------------------
# Small Cluster Profiles (18 nodes)
# 8 cpu-standard (64c, 512GB), 2 cpu-highmem (64c, 1.5TB), 8 gpu (32c, 256GB, 4xA100)
# -----------------------------------------------------------------------------
PROFILES_SMALL = [
    # Standard CPU jobs
    JobProfile(
        name="compute_small",
        weight=0.25,
        partition="standard",
        cpus_min=4, cpus_max=16,
        mem_gb_min=8, mem_gb_max=64,
        time_min=30, time_max=240,
        local_write_mb_min=500, local_write_mb_max=5000,
        nfs_write_mb_min=50, nfs_write_mb_max=500,
        base_failure_rate=0.05,
        description="Small compute jobs",
    ),
    JobProfile(
        name="compute_medium",
        weight=0.15,
        partition="standard",
        cpus_min=16, cpus_max=32,
        mem_gb_min=64, mem_gb_max=256,
        time_min=120, time_max=720,
        local_write_mb_min=2000, local_write_mb_max=20000,
        nfs_write_mb_min=100, nfs_write_mb_max=1000,
        base_failure_rate=0.08,
        description="Medium compute jobs",
    ),
    JobProfile(
        name="compute_large",
        weight=0.08,
        partition="standard",
        cpus_min=32, cpus_max=64,
        mem_gb_min=128, mem_gb_max=400,
        time_min=240, time_max=1440,
        local_write_mb_min=5000, local_write_mb_max=50000,
        nfs_write_mb_min=500, nfs_write_mb_max=2000,
        base_failure_rate=0.10,
        description="Large compute jobs",
    ),
    
    # High-memory jobs
    JobProfile(
        name="highmem_genomics",
        weight=0.06,
        partition="highmem",
        cpus_min=16, cpus_max=32,
        mem_gb_min=512, mem_gb_max=1000,
        time_min=120, time_max=720,
        local_write_mb_min=10000, local_write_mb_max=100000,
        nfs_write_mb_min=1000, nfs_write_mb_max=5000,
        base_failure_rate=0.12,
        description="Genomics/assembly with high memory",
    ),
    JobProfile(
        name="highmem_chemistry",
        weight=0.04,
        partition="highmem",
        cpus_min=32, cpus_max=64,
        mem_gb_min=800, mem_gb_max=1400,
        time_min=240, time_max=1440,
        local_write_mb_min=5000, local_write_mb_max=50000,
        nfs_write_mb_min=500, nfs_write_mb_max=2000,
        base_failure_rate=0.15,
        description="Quantum chemistry with high memory",
    ),
    
    # GPU jobs
    JobProfile(
        name="gpu_training",
        weight=0.12,
        partition="gpu",
        cpus_min=8, cpus_max=16,
        mem_gb_min=32, mem_gb_max=128,
        time_min=120, time_max=1440,
        gpus=1,
        local_write_mb_min=5000, local_write_mb_max=50000,
        nfs_write_mb_min=500, nfs_write_mb_max=2000,
        base_failure_rate=0.10,
        description="ML training (single GPU)",
    ),
    JobProfile(
        name="gpu_multi",
        weight=0.06,
        partition="gpu",
        cpus_min=16, cpus_max=32,
        mem_gb_min=64, mem_gb_max=200,
        time_min=240, time_max=2880,
        gpus=4,
        local_write_mb_min=20000, local_write_mb_max=200000,
        nfs_write_mb_min=1000, nfs_write_mb_max=5000,
        base_failure_rate=0.12,
        description="ML training (multi-GPU)",
    ),
    JobProfile(
        name="gpu_inference",
        weight=0.08,
        partition="gpu",
        cpus_min=4, cpus_max=8,
        mem_gb_min=16, mem_gb_max=64,
        time_min=30, time_max=240,
        gpus=1,
        local_write_mb_min=1000, local_write_mb_max=10000,
        nfs_write_mb_min=100, nfs_write_mb_max=500,
        base_failure_rate=0.05,
        description="ML inference",
    ),
    
    # Bad I/O patterns (what NØMAÐ should catch!)
    JobProfile(
        name="bad_io_nfs_heavy",
        weight=0.08,
        partition="standard",
        cpus_min=8, cpus_max=32,
        mem_gb_min=32, mem_gb_max=128,
        time_min=60, time_max=480,
        local_write_mb_min=0, local_write_mb_max=100,
        nfs_write_mb_min=5000, nfs_write_mb_max=50000,
        base_failure_rate=0.25,
        description="BAD: Heavy NFS writes instead of localscratch",
    ),
    JobProfile(
        name="bad_io_small_files",
        weight=0.05,
        partition="standard",
        cpus_min=4, cpus_max=16,
        mem_gb_min=16, mem_gb_max=64,
        time_min=30, time_max=240,
        local_write_mb_min=0, local_write_mb_max=50,
        nfs_write_mb_min=100, nfs_write_mb_max=500,
        base_failure_rate=0.20,
        description="BAD: Many small files to NFS",
    ),
    
    # Debug partition
    JobProfile(
        name="debug",
        weight=0.03,
        partition="debug",
        cpus_min=1, cpus_max=4,
        mem_gb_min=4, mem_gb_max=16,
        time_min=5, time_max=30,
        local_write_mb_min=0, local_write_mb_max=500,
        base_failure_rate=0.02,
        description="Quick debug jobs",
    ),
]


# -----------------------------------------------------------------------------
# Large Cluster Profiles (300 nodes)
# 150 cpu-standard, 50 cpu-highmem, 100 gpu
# -----------------------------------------------------------------------------
PROFILES_LARGE = [
    # Standard CPU jobs
    JobProfile(
        name="compute_small",
        weight=0.20,
        partition="standard",
        cpus_min=4, cpus_max=16,
        mem_gb_min=8, mem_gb_max=64,
        time_min=30, time_max=240,
        local_write_mb_min=500, local_write_mb_max=5000,
        nfs_write_mb_min=50, nfs_write_mb_max=500,
        base_failure_rate=0.05,
        description="Small compute jobs",
    ),
    JobProfile(
        name="compute_medium",
        weight=0.18,
        partition="standard",
        cpus_min=16, cpus_max=32,
        mem_gb_min=64, mem_gb_max=256,
        time_min=120, time_max=720,
        local_write_mb_min=2000, local_write_mb_max=20000,
        nfs_write_mb_min=100, nfs_write_mb_max=1000,
        base_failure_rate=0.08,
        description="Medium compute jobs",
    ),
    JobProfile(
        name="compute_large",
        weight=0.10,
        partition="standard",
        cpus_min=32, cpus_max=64,
        mem_gb_min=128, mem_gb_max=400,
        time_min=240, time_max=1440,
        local_write_mb_min=5000, local_write_mb_max=50000,
        nfs_write_mb_min=500, nfs_write_mb_max=2000,
        base_failure_rate=0.10,
        description="Large compute jobs",
    ),
    JobProfile(
        name="compute_multinode",
        weight=0.05,
        partition="standard",
        cpus_min=64, cpus_max=64,
        mem_gb_min=256, mem_gb_max=450,
        time_min=480, time_max=2880,
        local_write_mb_min=10000, local_write_mb_max=100000,
        nfs_write_mb_min=2000, nfs_write_mb_max=10000,
        base_failure_rate=0.15,
        description="Full-node compute jobs",
    ),
    
    # High-memory jobs
    JobProfile(
        name="highmem_genomics",
        weight=0.06,
        partition="highmem",
        cpus_min=16, cpus_max=32,
        mem_gb_min=512, mem_gb_max=1000,
        time_min=120, time_max=720,
        local_write_mb_min=10000, local_write_mb_max=100000,
        nfs_write_mb_min=1000, nfs_write_mb_max=5000,
        base_failure_rate=0.12,
        description="Genomics/assembly",
    ),
    JobProfile(
        name="highmem_assembly",
        weight=0.04,
        partition="highmem",
        cpus_min=32, cpus_max=64,
        mem_gb_min=1000, mem_gb_max=1400,
        time_min=480, time_max=2880,
        local_write_mb_min=50000, local_write_mb_max=500000,
        nfs_write_mb_min=5000, nfs_write_mb_max=20000,
        base_failure_rate=0.18,
        description="Large genome assembly",
    ),
    JobProfile(
        name="highmem_chemistry",
        weight=0.03,
        partition="highmem",
        cpus_min=32, cpus_max=64,
        mem_gb_min=800, mem_gb_max=1400,
        time_min=240, time_max=1440,
        local_write_mb_min=5000, local_write_mb_max=50000,
        nfs_write_mb_min=500, nfs_write_mb_max=2000,
        base_failure_rate=0.15,
        description="Quantum chemistry",
    ),
    
    # GPU jobs
    JobProfile(
        name="gpu_training_single",
        weight=0.08,
        partition="gpu",
        cpus_min=8, cpus_max=16,
        mem_gb_min=32, mem_gb_max=128,
        time_min=120, time_max=1440,
        gpus=1,
        local_write_mb_min=5000, local_write_mb_max=50000,
        nfs_write_mb_min=500, nfs_write_mb_max=2000,
        base_failure_rate=0.10,
        description="ML training (1 GPU)",
    ),
    JobProfile(
        name="gpu_training_multi",
        weight=0.06,
        partition="gpu",
        cpus_min=16, cpus_max=32,
        mem_gb_min=64, mem_gb_max=200,
        time_min=240, time_max=2880,
        gpus=4,
        local_write_mb_min=20000, local_write_mb_max=200000,
        nfs_write_mb_min=1000, nfs_write_mb_max=5000,
        base_failure_rate=0.12,
        description="ML training (4 GPU)",
    ),
    JobProfile(
        name="gpu_inference",
        weight=0.05,
        partition="gpu",
        cpus_min=4, cpus_max=8,
        mem_gb_min=16, mem_gb_max=64,
        time_min=30, time_max=240,
        gpus=1,
        local_write_mb_min=1000, local_write_mb_max=10000,
        nfs_write_mb_min=100, nfs_write_mb_max=500,
        base_failure_rate=0.05,
        description="ML inference",
    ),
    JobProfile(
        name="gpu_molecular_dynamics",
        weight=0.04,
        partition="gpu",
        cpus_min=8, cpus_max=16,
        mem_gb_min=32, mem_gb_max=128,
        time_min=480, time_max=2880,
        gpus=2,
        local_write_mb_min=10000, local_write_mb_max=100000,
        nfs_write_mb_min=2000, nfs_write_mb_max=10000,
        base_failure_rate=0.08,
        description="GROMACS/AMBER MD simulations",
    ),
    
    # Bad I/O patterns
    JobProfile(
        name="bad_io_nfs_heavy",
        weight=0.05,
        partition="standard",
        cpus_min=8, cpus_max=32,
        mem_gb_min=32, mem_gb_max=128,
        time_min=60, time_max=480,
        local_write_mb_min=0, local_write_mb_max=100,
        nfs_write_mb_min=5000, nfs_write_mb_max=50000,
        base_failure_rate=0.25,
        description="BAD: Heavy NFS writes",
    ),
    JobProfile(
        name="bad_io_small_files",
        weight=0.03,
        partition="standard",
        cpus_min=4, cpus_max=16,
        mem_gb_min=16, mem_gb_max=64,
        time_min=30, time_max=240,
        local_write_mb_min=0, local_write_mb_max=50,
        nfs_write_mb_min=100, nfs_write_mb_max=500,
        base_failure_rate=0.20,
        description="BAD: Many small files",
    ),
    
    # Debug
    JobProfile(
        name="debug",
        weight=0.03,
        partition="debug",
        cpus_min=1, cpus_max=4,
        mem_gb_min=4, mem_gb_max=16,
        time_min=5, time_max=30,
        local_write_mb_min=0, local_write_mb_max=500,
        base_failure_rate=0.02,
        description="Debug jobs",
    ),
]


# Profile configurations
CLUSTER_CONFIGS = {
    'minimal': PROFILES_MINIMAL,
    'small': PROFILES_SMALL,
    'large': PROFILES_LARGE,
}


# ============================================
# JOB GENERATOR
# ============================================

class JobGenerator:
    """Generates and submits synthetic SLURM jobs."""
    
    def __init__(
        self,
        config: str = 'minimal',
        failure_rate_multiplier: float = 1.0,
        dry_run: bool = False,
    ):
        self.config = config
        self.profiles = CLUSTER_CONFIGS.get(config, PROFILES_MINIMAL)
        self.failure_rate_multiplier = failure_rate_multiplier
        self.dry_run = dry_run
        self.job_counter = 0
        
        # Weighted profile selection
        self.profile_weights = [p.weight for p in self.profiles]
        
        logger.info(f"Loaded {len(self.profiles)} profiles for '{config}' cluster")
        
    def select_profile(self) -> JobProfile:
        """Select a job profile based on weights."""
        return random.choices(self.profiles, weights=self.profile_weights, k=1)[0]
    
    def generate_job(self) -> dict:
        """Generate a single job configuration."""
        profile = self.select_profile()
        self.job_counter += 1
        
        # Random values within profile ranges
        cpus = random.randint(profile.cpus_min, profile.cpus_max)
        mem_gb = random.uniform(profile.mem_gb_min, profile.mem_gb_max)
        runtime_min = random.randint(profile.time_min, profile.time_max)
        
        nfs_write_mb = random.uniform(profile.nfs_write_mb_min, profile.nfs_write_mb_max)
        local_write_mb = random.uniform(profile.local_write_mb_min, profile.local_write_mb_max)
        
        # Determine if job should fail
        failure_rate = profile.base_failure_rate * self.failure_rate_multiplier
        should_fail = random.random() < failure_rate
        failure_type = None
        if should_fail:
            failure_type = random.choice(['timeout', 'oom', 'error', 'nfs_slow'])
        
        return {
            'name': f"job_{profile.name}_{self.job_counter:05d}",
            'profile': profile.name,
            'partition': profile.partition,
            'cpus': cpus,
            'mem_gb': mem_gb,
            'runtime_min': runtime_min,
            'gpus': profile.gpus,
            'gpu_type': profile.gpu_type,
            'nfs_write_mb': nfs_write_mb,
            'local_write_mb': local_write_mb,
            'should_fail': should_fail,
            'failure_type': failure_type,
            'description': profile.description,
        }
    
    def create_job_script(self, job: dict) -> str:
        """Create a SLURM job script."""
        
        # Actual runtime (shorter than requested for simulation)
        actual_runtime_sec = min(job['runtime_min'] * 6, 300)  # Max 5 min for testing
        
        # Build script
        script_lines = [
            "#!/bin/bash",
            f"#SBATCH --job-name={job['name']}",
            f"#SBATCH --partition={job['partition']}",
            f"#SBATCH --cpus-per-task={job['cpus']}",
            f"#SBATCH --mem={int(job['mem_gb'] * 1024)}M",
            f"#SBATCH --time={job['runtime_min']}",
            "#SBATCH --output=/tmp/slurm-%j.out",
            "#SBATCH --error=/tmp/slurm-%j.err",
        ]
        
        if job['gpus'] > 0:
            script_lines.append(f"#SBATCH --gres=gpu:{job['gpu_type']}:{job['gpus']}")
        
        script_lines.extend([
            "",
            "# Job metadata (for NØMAÐ tracking)",
            f"echo \"NOMADE_PROFILE={job['profile']}\"",
            f"echo \"NOMADE_PARTITION={job['partition']}\"",
            f"echo \"NOMADE_CPUS={job['cpus']}\"",
            f"echo \"NOMADE_MEM_GB={job['mem_gb']:.1f}\"",
            f"echo \"NOMADE_GPUS={job['gpus']}\"",
            f"echo \"NOMADE_EXPECTED_NFS_MB={job['nfs_write_mb']:.0f}\"",
            f"echo \"NOMADE_EXPECTED_LOCAL_MB={job['local_write_mb']:.0f}\"",
            "",
            f"echo \"Starting job {job['name']} at $(date)\"",
            f"echo \"Description: {job['description']}\"",
            "",
        ])
        
        # Simulate I/O
        if job['local_write_mb'] > 0:
            local_mb = int(min(job['local_write_mb'], 1000))  # Cap for testing
            script_lines.extend([
                "# LOCAL I/O - Good practice: use /localscratch",
                "LOCAL_SCRATCH=/localscratch/$SLURM_JOB_ID",
                "mkdir -p $LOCAL_SCRATCH",
                f"dd if=/dev/zero of=$LOCAL_SCRATCH/data.dat bs=1M count={local_mb} 2>/dev/null || true",
            ])
        
        if job['nfs_write_mb'] > 0:
            nfs_mb = int(min(job['nfs_write_mb'], 100))  # Cap for testing
            script_lines.extend([
                "# NFS I/O - Writing to shared storage",
                "mkdir -p /scratch/$USER 2>/dev/null || true",
                f"dd if=/dev/zero of=/scratch/$USER/nfs_$SLURM_JOB_ID.dat bs=1M count={nfs_mb} 2>/dev/null || true",
            ])
        
        # Simulate compute work
        script_lines.extend([
            "",
            "# Simulate compute",
            f"sleep {actual_runtime_sec}",
        ])
        
        # Handle failures
        if job['should_fail']:
            if job['failure_type'] == 'timeout':
                script_lines.append(f"sleep {job['runtime_min'] * 60 + 60}")
            elif job['failure_type'] == 'oom':
                script_lines.append("python3 -c \"x = ' ' * (1024**3 * 100)\" 2>/dev/null || true")
                script_lines.append("exit 137")
            elif job['failure_type'] == 'error':
                script_lines.append("exit 1")
            elif job['failure_type'] == 'nfs_slow':
                script_lines.extend([
                    "for i in $(seq 1 50); do",
                    "    dd if=/dev/zero of=/scratch/$USER/nfs_slow_$i.dat bs=1M count=20 2>/dev/null",
                    "    sync",
                    "done",
                ])
        
        # Cleanup
        script_lines.extend([
            "",
            "# Cleanup",
            "rm -rf /localscratch/$SLURM_JOB_ID 2>/dev/null || true",
            "rm -f /scratch/$USER/nfs_$SLURM_JOB_ID.dat 2>/dev/null || true",
            "rm -f /scratch/$USER/nfs_slow_*.dat 2>/dev/null || true",
            "",
            f"echo \"Job {job['name']} completed at $(date)\"",
        ])
        
        if not job['should_fail']:
            script_lines.append("exit 0")
        
        return "\n".join(script_lines)
    
    def submit_job(self, job: dict) -> int | None:
        """Submit a job to SLURM."""
        script = self.create_job_script(job)
        
        if self.dry_run:
            gpu_str = f" {job['gpus']}gpu" if job['gpus'] > 0 else ""
            logger.info(f"[DRY RUN] {job['name']} | {job['partition']} | "
                       f"{job['cpus']}c {job['mem_gb']:.0f}GB{gpu_str} | {job['description']}")
            return None
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
            f.write(script)
            script_path = f.name
        
        try:
            result = subprocess.run(
                ['sbatch', script_path],
                capture_output=True,
                text=True,
            )
            
            if result.returncode == 0:
                job_id = int(result.stdout.strip().split()[-1])
                logger.info(f"Submitted: {job['name']} (ID: {job_id}, "
                           f"partition: {job['partition']}, profile: {job['profile']})")
                return job_id
            else:
                logger.error(f"Failed to submit {job['name']}: {result.stderr}")
                return None
                
        finally:
            os.unlink(script_path)
    
    def run_continuous(self, rate: float = 10.0):
        """Continuously generate jobs at specified rate."""
        interval = 3600 / rate
        
        logger.info(f"Starting continuous generation: {rate} jobs/hour ({interval:.1f}s interval)")
        
        try:
            while True:
                job = self.generate_job()
                self.submit_job(job)
                sleep_time = interval * random.uniform(0.5, 1.5)
                time.sleep(sleep_time)
                
        except KeyboardInterrupt:
            logger.info("Stopping job generator")
    
    def run_burst(self, count: int = 20):
        """Submit a burst of jobs quickly."""
        logger.info(f"Submitting burst of {count} jobs")
        
        for _ in range(count):
            job = self.generate_job()
            self.submit_job(job)
            time.sleep(0.5)
        
        logger.info(f"Burst complete: {count} jobs submitted")
    
    def run_once(self, count: int = 10):
        """Submit a fixed number of jobs and exit."""
        logger.info(f"Submitting {count} jobs")
        
        for _ in range(count):
            job = self.generate_job()
            self.submit_job(job)
            time.sleep(1)
        
        logger.info(f"Complete: {count} jobs submitted")
    
    def show_profiles(self):
        """Display available profiles."""
        print(f"\n{'='*80}")
        print(f"Profiles for '{self.config}' cluster ({len(self.profiles)} total)")
        print(f"{'='*80}\n")
        
        for p in sorted(self.profiles, key=lambda x: -x.weight):
            gpu_str = f"{p.gpus}x {p.gpu_type}" if p.gpus > 0 else "none"
            print(f"  {p.name:25} | weight: {p.weight*100:>4.0f}% | "
                  f"partition: {p.partition:10} | "
                  f"cpus: {p.cpus_min}-{p.cpus_max:>3} | "
                  f"gpu: {gpu_str}")
            print(f"  {'':<25} | {p.description}")
            print()


# ============================================
# MAIN
# ============================================

def main():
    parser = argparse.ArgumentParser(
        description="Generate synthetic HPC jobs for NØMAÐ testing"
    )
    
    parser.add_argument(
        '--config', '-c',
        choices=['minimal', 'small', 'large'],
        default='minimal',
        help='Cluster configuration (default: minimal)'
    )
    parser.add_argument(
        '--rate', type=float, default=10.0,
        help='Jobs per hour for continuous mode (default: 10)'
    )
    parser.add_argument(
        '--failure-rate', type=float, default=1.0,
        help='Failure rate multiplier (default: 1.0)'
    )
    parser.add_argument(
        '--burst', action='store_true',
        help='Submit a burst of 20 jobs'
    )
    parser.add_argument(
        '--once', type=int, metavar='N',
        help='Submit N jobs and exit'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Print jobs without submitting'
    )
    parser.add_argument(
        '--list-profiles', action='store_true',
        help='Show available job profiles and exit'
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='Verbose output'
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    generator = JobGenerator(
        config=args.config,
        failure_rate_multiplier=args.failure_rate,
        dry_run=args.dry_run,
    )
    
    if args.list_profiles:
        generator.show_profiles()
    elif args.burst:
        generator.run_burst()
    elif args.once:
        generator.run_once(args.once)
    else:
        generator.run_continuous(rate=args.rate)


if __name__ == '__main__':
    main()
