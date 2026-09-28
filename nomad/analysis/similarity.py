# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ Similarity Analysis

Computes similarity matrices between jobs using cosine similarity
on enriched feature vectors combining sacct data and I/O patterns.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

logger = logging.getLogger(__name__)


@dataclass
class JobFeatures:
    """Enriched feature vector for a job."""
    job_id: str

    # From job_summary (sacct)
    health_score: float
    cpu_efficiency: float
    memory_efficiency: float
    used_gpu: bool
    had_swap: bool

    # From job_io_samples (monitor)
    total_write_gb: float
    write_rate_mbps: float  # Peak write rate
    nfs_ratio: float
    sample_count: int
    runtime_minutes: float

    # From iostat (system-level during job)
    avg_iowait_percent: float
    peak_iowait_percent: float
    avg_device_util: float

    # From mpstat (core-level during job)
    avg_core_busy: float
    core_imbalance_ratio: float  # std/avg - higher = more imbalance
    max_core_busy: float

    # From vmstat (memory pressure during job)
    avg_memory_pressure: float
    peak_swap_activity: float  # swap_in + swap_out
    avg_procs_blocked: float

    # Derived
    write_intensity: float  # GB per minute

    def to_vector(self) -> np.ndarray:
        """Convert to normalized feature vector for similarity."""
        return np.array([
            self.health_score,
            min(self.cpu_efficiency, 1.0),
            min(self.memory_efficiency, 1.0),
            1.0 if self.used_gpu else 0.0,
            1.0 if self.had_swap else 0.0,
            min(self.total_write_gb / 100, 1.0),  # Normalize to 100GB
            min(self.write_rate_mbps / 500, 1.0),  # Normalize to 500MB/s
            self.nfs_ratio,
            min(self.runtime_minutes / 60, 1.0),  # Normalize to 1 hour
            min(self.write_intensity / 2, 1.0),  # Normalize to 2GB/min
            min(self.avg_iowait_percent / 50, 1.0),  # Normalize to 50% iowait
            min(self.peak_iowait_percent / 80, 1.0),  # Normalize to 80% peak
            min(self.avg_device_util / 100, 1.0),  # Already 0-100
            min(self.avg_core_busy / 100, 1.0),  # Normalize to 100%
            min(self.core_imbalance_ratio / 1.0, 1.0),  # Normalize to 1.0 ratio
            min(self.max_core_busy / 100, 1.0),  # Normalize to 100%
            min(self.avg_memory_pressure, 1.0),  # Already 0-1
            min(self.peak_swap_activity / 1000, 1.0),  # Normalize to 1000 KB/s
            min(self.avg_procs_blocked / 10, 1.0),  # Normalize to 10 blocked procs
        ])

    @property
    def feature_names(self) -> list[str]:
        return [
            'health_score',
            'cpu_efficiency',
            'memory_efficiency',
            'used_gpu',
            'had_swap',
            'total_write_gb',
            'write_rate_mbps',
            'nfs_ratio',
            'runtime_minutes',
            'write_intensity',
            'avg_iowait_percent',
            'peak_iowait_percent',
            'avg_device_util',
            'avg_core_busy',
            'core_imbalance_ratio',
            'max_core_busy',
            'avg_memory_pressure',
            'peak_swap_activity',
            'avg_procs_blocked',
        ]


class SimilarityAnalyzer:
    """
    Analyzes job similarity using enriched feature vectors.
    
    Combines data from:
    - job_summary: sacct metrics, health scores
    - job_io_samples: real-time I/O monitoring
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self._conn = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def get_enriched_features(self, min_samples: int = 3) -> list[JobFeatures]:
        """
        Get enriched feature vectors for jobs with I/O data.
        
        Args:
            min_samples: Minimum I/O samples required
            
        Returns:
            List of JobFeatures with combined metrics
        """
        conn = self._get_conn()

        query = """
        SELECT 
            js.job_id,
            js.health_score,
            COALESCE(js.peak_cpu_percent, 0) / 100.0 as cpu_efficiency,
            COALESCE(js.peak_memory_gb, 0) / NULLIF(j.req_mem_mb / 1024.0, 0) as memory_efficiency,
            COALESCE(js.used_gpu, 0) as used_gpu,
            COALESCE(js.had_swap, 0) as had_swap,
            COALESCE(js.nfs_ratio, 0) as nfs_ratio,
            -- I/O aggregates from job_io_samples
            COALESCE(SUM(io.total_write_bytes), 0) / (1024.0 * 1024 * 1024) as total_write_gb,
            COALESCE(MAX(io.total_write_bytes), 0) / (1024.0 * 1024) as peak_write_mb,
            COUNT(io.id) as sample_count,
            -- Runtime from samples
            (JULIANDAY(MAX(io.timestamp)) - JULIANDAY(MIN(io.timestamp))) * 24 * 60 as runtime_minutes,
            -- Job time range for iostat correlation
            j.start_time,
            j.end_time
        FROM job_summary js
        JOIN jobs j ON js.job_id = j.job_id
        LEFT JOIN job_io_samples io ON js.job_id = io.job_id
        GROUP BY js.job_id
        HAVING sample_count >= ?
        ORDER BY js.job_id
        """

        rows = conn.execute(query, (min_samples,)).fetchall()

        features = []
        for row in rows:
            runtime = max(row['runtime_minutes'] or 1, 1)  # Avoid division by zero
            total_write = row['total_write_gb'] or 0

            # Get iostat data during job runtime
            avg_iowait = 0.0
            peak_iowait = 0.0
            avg_device_util = 0.0

            if row['start_time'] and row['end_time']:
                iostat_query = """
                SELECT 
                    AVG(iowait_percent) as avg_iowait,
                    MAX(iowait_percent) as peak_iowait
                FROM iostat_cpu
                WHERE timestamp BETWEEN ? AND ?
                """
                iostat_row = conn.execute(iostat_query,
                    (row['start_time'], row['end_time'])).fetchone()
                if iostat_row:
                    avg_iowait = iostat_row['avg_iowait'] or 0.0
                    peak_iowait = iostat_row['peak_iowait'] or 0.0

                # Device utilization
                device_query = """
                SELECT AVG(util_percent) as avg_util
                FROM iostat_device
                WHERE timestamp BETWEEN ? AND ?
                  AND device NOT LIKE 'loop%'
                """
                device_row = conn.execute(device_query,
                    (row['start_time'], row['end_time'])).fetchone()
                if device_row:
                    avg_device_util = device_row['avg_util'] or 0.0

                # Core utilization from mpstat
                mpstat_query = """
                SELECT 
                    AVG(avg_busy_percent) as avg_core_busy,
                    AVG(imbalance_ratio) as avg_imbalance,
                    MAX(max_busy_percent) as max_core_busy
                FROM mpstat_summary
                WHERE timestamp BETWEEN ? AND ?
                """
                mpstat_row = conn.execute(mpstat_query,
                    (row['start_time'], row['end_time'])).fetchone()
                if mpstat_row:
                    avg_core_busy = mpstat_row['avg_core_busy'] or 0.0
                    core_imbalance = mpstat_row['avg_imbalance'] or 0.0
                    max_core_busy = mpstat_row['max_core_busy'] or 0.0
                else:
                    avg_core_busy = 0.0
                    core_imbalance = 0.0
                    max_core_busy = 0.0

                # Memory pressure from vmstat
                vmstat_query = """
                SELECT 
                    AVG(memory_pressure) as avg_pressure,
                    MAX(swap_in_kb + swap_out_kb) as peak_swap,
                    AVG(procs_blocked) as avg_blocked
                FROM vmstat
                WHERE timestamp BETWEEN ? AND ?
                """
                vmstat_row = conn.execute(vmstat_query,
                    (row['start_time'], row['end_time'])).fetchone()
                if vmstat_row:
                    avg_memory_pressure = vmstat_row['avg_pressure'] or 0.0
                    peak_swap_activity = vmstat_row['peak_swap'] or 0.0
                    avg_procs_blocked = vmstat_row['avg_blocked'] or 0.0
                else:
                    avg_memory_pressure = 0.0
                    peak_swap_activity = 0.0
                    avg_procs_blocked = 0.0
            else:
                avg_core_busy = 0.0
                core_imbalance = 0.0
                max_core_busy = 0.0
                avg_memory_pressure = 0.0
                peak_swap_activity = 0.0
                avg_procs_blocked = 0.0

            features.append(JobFeatures(
                job_id=row['job_id'],
                health_score=row['health_score'] or 0.5,
                cpu_efficiency=row['cpu_efficiency'] or 0,
                memory_efficiency=min(row['memory_efficiency'] or 0, 2.0),
                used_gpu=bool(row['used_gpu']),
                had_swap=bool(row['had_swap']),
                total_write_gb=total_write,
                write_rate_mbps=row['peak_write_mb'] or 0,
                nfs_ratio=row['nfs_ratio'] or 0,
                sample_count=row['sample_count'],
                runtime_minutes=runtime,
                avg_iowait_percent=avg_iowait,
                peak_iowait_percent=peak_iowait,
                avg_device_util=avg_device_util,
                avg_core_busy=avg_core_busy,
                core_imbalance_ratio=core_imbalance,
                max_core_busy=max_core_busy,
                avg_memory_pressure=avg_memory_pressure,
                peak_swap_activity=peak_swap_activity,
                avg_procs_blocked=avg_procs_blocked,
                write_intensity=total_write / runtime if runtime > 0 else 0,
            ))

        logger.info(f"Loaded {len(features)} jobs with enriched features")
        return features

    def compute_similarity_matrix(self, features: list[JobFeatures]) -> tuple[np.ndarray, list[str]]:
        """
        Compute pairwise cosine similarity matrix.
        
        Returns:
            (similarity_matrix, job_ids)
        """
        if not features:
            return np.array([]), []

        # Build feature matrix
        job_ids = [f.job_id for f in features]
        vectors = np.array([f.to_vector() for f in features])

        # Handle zero vectors
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1  # Avoid division by zero
        normalized = vectors / norms

        # Cosine similarity = 1 - cosine distance
        # For normalized vectors: similarity = dot product
        similarity = np.dot(normalized, normalized.T)

        # Ensure diagonal is 1.0
        np.fill_diagonal(similarity, 1.0)

        logger.info(f"Computed {len(job_ids)}x{len(job_ids)} similarity matrix")
        return similarity, job_ids

    def cluster_jobs(self, similarity: np.ndarray, job_ids: list[str],
                     n_clusters: int = None, threshold: float = 0.5) -> dict[str, int]:
        """
        Cluster jobs based on similarity.
        
        Args:
            similarity: Similarity matrix
            job_ids: Job IDs corresponding to rows/cols
            n_clusters: Fixed number of clusters (optional)
            threshold: Distance threshold for clustering (if n_clusters not set)
            
        Returns:
            Dict mapping job_id to cluster_id
        """
        if len(job_ids) < 2:
            return {job_ids[0]: 0} if job_ids else {}

        # Convert similarity to distance
        distance = 1 - similarity

        # Condensed distance matrix for scipy
        condensed = squareform(distance, checks=False)

        # Hierarchical clustering
        linkage_matrix = linkage(condensed, method='average')

        if n_clusters:
            clusters = fcluster(linkage_matrix, n_clusters, criterion='maxclust')
        else:
            clusters = fcluster(linkage_matrix, threshold, criterion='distance')

        result = {job_id: int(cluster) for job_id, cluster in zip(job_ids, clusters)}

        n_clusters_found = len(set(clusters))
        logger.info(f"Found {n_clusters_found} clusters")

        return result

    def find_anomalies(self, features: list[JobFeatures],
                       similarity: np.ndarray,
                       threshold: float = 0.3) -> list[tuple[str, float]]:
        """
        Find anomalous jobs (low average similarity to others).
        
        Returns:
            List of (job_id, anomaly_score) sorted by score descending
        """
        if len(features) < 2:
            return []

        job_ids = [f.job_id for f in features]

        # Average similarity to other jobs (excluding self)
        n = len(similarity)
        avg_similarity = (similarity.sum(axis=1) - 1) / (n - 1)

        # Anomaly score = 1 - avg_similarity
        anomaly_scores = 1 - avg_similarity

        # Filter and sort
        anomalies = [
            (job_ids[i], float(anomaly_scores[i]))
            for i in range(n)
            if anomaly_scores[i] > threshold
        ]

        anomalies.sort(key=lambda x: x[1], reverse=True)

        logger.info(f"Found {len(anomalies)} anomalies (threshold={threshold})")
        return anomalies

    def get_cluster_profiles(self, features: list[JobFeatures],
                            clusters: dict[str, int]) -> dict[int, dict]:
        """
        Compute average profile for each cluster.
        
        Returns:
            Dict mapping cluster_id to average feature values
        """
        # Group features by cluster
        cluster_features: dict[int, list[JobFeatures]] = {}
        for f in features:
            cluster_id = clusters.get(f.job_id, -1)
            if cluster_id not in cluster_features:
                cluster_features[cluster_id] = []
            cluster_features[cluster_id].append(f)

        profiles = {}
        for cluster_id, feats in cluster_features.items():
            vectors = np.array([f.to_vector() for f in feats])
            avg_vector = vectors.mean(axis=0)

            # Health distribution
            health_scores = [f.health_score for f in feats]
            failure_rate = sum(1 for h in health_scores if h < 0.5) / len(health_scores)

            profiles[cluster_id] = {
                'count': len(feats),
                'failure_rate': failure_rate,
                'avg_write_gb': np.mean([f.total_write_gb for f in feats]),
                'avg_runtime_min': np.mean([f.runtime_minutes for f in feats]),
                'avg_write_intensity': np.mean([f.write_intensity for f in feats]),
                'avg_nfs_ratio': np.mean([f.nfs_ratio for f in feats]),
                'feature_vector': avg_vector.tolist(),
            }

        return profiles

    def find_similar_jobs(self, job_id: str, features: list[JobFeatures],
                         similarity: np.ndarray, top_k: int = 5) -> list[tuple[str, float]]:
        """
        Find most similar jobs to a given job.
        
        Returns:
            List of (job_id, similarity_score) sorted by similarity descending
        """
        job_ids = [f.job_id for f in features]

        try:
            idx = job_ids.index(job_id)
        except ValueError:
            logger.warning(f"Job {job_id} not found in features")
            return []

        # Get similarities for this job
        similarities = similarity[idx]

        # Sort by similarity (excluding self)
        sorted_indices = np.argsort(similarities)[::-1]

        results = []
        for i in sorted_indices:
            if job_ids[i] != job_id:
                results.append((job_ids[i], float(similarities[i])))
                if len(results) >= top_k:
                    break

        return results

    def export_for_visualization(self, features: list[JobFeatures],
                                 similarity: np.ndarray,
                                 clusters: dict[str, int]) -> dict:
        """
        Export data in format suitable for 3D visualization.
        
        Returns:
            Dict with nodes and edges for network visualization
        """
        job_ids = [f.job_id for f in features]

        nodes = []
        for f in features:
            nodes.append({
                'id': f.job_id,
                'cluster': clusters.get(f.job_id, 0),
                'health_score': f.health_score,
                'total_write_gb': f.total_write_gb,
                'nfs_ratio': f.nfs_ratio,
                'runtime_minutes': f.runtime_minutes,
                'write_intensity': f.write_intensity,
                'features': f.to_vector().tolist(),
            })

        # Create edges for similar jobs (threshold > 0.7)
        edges = []
        n = len(similarity)
        for i in range(n):
            for j in range(i + 1, n):
                if similarity[i, j] > 0.7:
                    edges.append({
                        'source': job_ids[i],
                        'target': job_ids[j],
                        'weight': float(similarity[i, j]),
                    })

        return {
            'nodes': nodes,
            'edges': edges,
            'metadata': {
                'total_jobs': len(nodes),
                'total_edges': len(edges),
                'clusters': len(set(clusters.values())),
                'timestamp': datetime.now().isoformat(),
            }
        }

    def summary_report(self) -> str:
        """Generate a text summary of job patterns."""
        features = self.get_enriched_features(min_samples=3)

        if not features:
            return "No jobs with sufficient I/O data found."

        similarity, job_ids = self.compute_similarity_matrix(features)
        clusters = self.cluster_jobs(similarity, job_ids)
        anomalies = self.find_anomalies(features, similarity)
        profiles = self.get_cluster_profiles(features, clusters)

        lines = [
            "═══ NØMAÐ Similarity Analysis ═══",
            "",
            f"Jobs analyzed: {len(features)}",
            f"Clusters found: {len(profiles)}",
            f"Anomalies detected: {len(anomalies)}",
            "",
            "─── Cluster Profiles ───",
        ]

        for cluster_id, profile in sorted(profiles.items()):
            status = "⚠ HIGH RISK" if profile['failure_rate'] > 0.3 else "✓ Healthy"
            lines.append(f"\nCluster {cluster_id} ({profile['count']} jobs) {status}")
            lines.append(f"  Failure rate: {profile['failure_rate']:.1%}")
            lines.append(f"  Avg write: {profile['avg_write_gb']:.1f} GB")
            lines.append(f"  Avg runtime: {profile['avg_runtime_min']:.1f} min")
            lines.append(f"  Write intensity: {profile['avg_write_intensity']:.2f} GB/min")

        if anomalies:
            lines.append("\n─── Anomalous Jobs ───")
            for job_id, score in anomalies[:5]:
                lines.append(f"  {job_id}: anomaly_score={score:.2f}")

        return "\n".join(lines)


def main():
    """CLI for similarity analysis."""
    import argparse

    parser = argparse.ArgumentParser(description='NØMAÐ Similarity Analysis')
    parser.add_argument('--db', default='/var/lib/nomad/nomad.db', help='Database path')
    parser.add_argument('--min-samples', type=int, default=3, help='Min I/O samples per job')
    parser.add_argument('--export', type=str, help='Export JSON for visualization')
    parser.add_argument('--find-similar', type=str, help='Find jobs similar to this job ID')

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    analyzer = SimilarityAnalyzer(args.db)

    if args.find_similar:
        features = analyzer.get_enriched_features(args.min_samples)
        similarity, job_ids = analyzer.compute_similarity_matrix(features)
        similar = analyzer.find_similar_jobs(args.find_similar, features, similarity)
        print(f"\nJobs similar to {args.find_similar}:")
        for job_id, score in similar:
            print(f"  {job_id}: {score:.3f}")
    elif args.export:
        features = analyzer.get_enriched_features(args.min_samples)
        similarity, job_ids = analyzer.compute_similarity_matrix(features)
        clusters = analyzer.cluster_jobs(similarity, job_ids)
        data = analyzer.export_for_visualization(features, similarity, clusters)

        with open(args.export, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"Exported to {args.export}")
    else:
        print(analyzer.summary_report())


if __name__ == '__main__':
    main()
