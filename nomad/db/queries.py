# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Database query utilities for NØMAÐ."""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TimeSeriesQuery:
    """Helper for time-series queries."""

    table: str
    metric_column: str
    timestamp_column: str = "timestamp"

    def build_query(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        group_by: str | None = None,
        additional_columns: list[str] | None = None,
        where_clause: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Build a time-series query with optional filters."""
        columns = [self.metric_column, self.timestamp_column]
        if additional_columns:
            columns.extend(additional_columns)
        if group_by and group_by not in columns:
            columns.append(group_by)

        query = f"SELECT {', '.join(columns)} FROM {self.table}"
        params = []

        conditions = []
        if start_time:
            conditions.append(f"{self.timestamp_column} >= ?")
            params.append(start_time.isoformat())
        if end_time:
            conditions.append(f"{self.timestamp_column} <= ?")
            params.append(end_time.isoformat())
        if where_clause:
            conditions.append(where_clause)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        if group_by:
            query += f" GROUP BY {group_by}"

        query += f" ORDER BY {self.timestamp_column}"

        return query, params


class QueryManager:
    """Centralized database query manager."""

    def __init__(self, db_path: Path):
        """Initialize query manager."""
        self.db_path = db_path

    def _execute(
        self,
        query: str,
        params: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a query and return results as list of dicts."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)

            return [dict(row) for row in cursor.fetchall()]

    def get_filesystem_usage(
        self,
        path: str | None = None,
        hours_back: int = 24,
    ) -> list[dict[str, Any]]:
        """Get filesystem usage history."""
        query = TimeSeriesQuery(
            table="filesystems",
            metric_column="used_percent",
            additional_columns=["path", "used_bytes", "available_bytes", "fill_rate_bytes_per_day"]
        )

        where_clause = f"path = '{path}'" if path else None
        start_time = datetime.now() - timedelta(hours=hours_back)

        sql, params = query.build_query(
            start_time=start_time,
            where_clause=where_clause
        )

        return self._execute(sql, params)

    def get_quota_usage(
        self,
        entity_name: str | None = None,
        entity_type: str = "user",
        hours_back: int = 24,
    ) -> list[dict[str, Any]]:
        """Get quota usage history."""
        query = TimeSeriesQuery(
            table="quotas",
            metric_column="used_percent",
            additional_columns=["entity_name", "used_bytes", "limit_bytes"]
        )

        conditions = [f"entity_type = '{entity_type}'"]
        if entity_name:
            conditions.append(f"entity_name = '{entity_name}'")
        where_clause = " AND ".join(conditions)

        start_time = datetime.now() - timedelta(hours=hours_back)

        sql, params = query.build_query(
            start_time=start_time,
            where_clause=where_clause
        )

        return self._execute(sql, params)

    def get_node_status(self, partition: str | None = None) -> list[dict[str, Any]]:
        """Get current node status."""
        query = "SELECT * FROM nodes"
        params = []

        if partition:
            query += " WHERE partition = ?"
            params.append(partition)

        query += " ORDER BY hostname"

        return self._execute(query, params)

    def get_failed_nodes(self) -> list[dict[str, Any]]:
        """Get nodes in failed state."""
        query = """
            SELECT hostname, partition, status, drain_reason, last_seen
            FROM nodes
            WHERE status IN ('DOWN', 'FAIL', 'DRAIN')
            ORDER BY last_seen DESC
        """
        return self._execute(query)

    def get_queue_stats(self, hours_back: int = 1) -> dict[str, Any]:
        """Get queue statistics."""
        start_time = datetime.now() - timedelta(hours=hours_back)

        query = """
            SELECT 
                COUNT(*) as total_jobs,
                SUM(CASE WHEN job_state = 'RUNNING' THEN 1 ELSE 0 END) as running,
                SUM(CASE WHEN job_state = 'PENDING' THEN 1 ELSE 0 END) as pending,
                AVG(CASE WHEN job_state = 'PENDING' THEN 
                    CAST((julianday('now') - julianday(submit_time)) * 24 AS REAL) 
                    ELSE NULL END) as avg_wait_hours,
                MAX(CASE WHEN job_state = 'PENDING' THEN 
                    CAST((julianday('now') - julianday(submit_time)) * 24 AS REAL) 
                    ELSE NULL END) as max_wait_hours
            FROM slurm_queue_snapshots
            WHERE timestamp >= ?
        """

        results = self._execute(query, [start_time.isoformat()])
        return results[0] if results else {}

    def get_recent_alerts(
        self,
        hours_back: int = 24,
        severity: str | None = None,
        unacknowledged_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Get recent alerts."""
        start_time = datetime.now() - timedelta(hours=hours_back)

        conditions = ["timestamp >= ?"]
        params = [start_time.isoformat()]

        if severity:
            conditions.append("severity = ?")
            params.append(severity)

        if unacknowledged_only:
            conditions.append("acknowledged = 0")

        query = f"""
            SELECT * FROM alert_history
            WHERE {' AND '.join(conditions)}
            ORDER BY timestamp DESC
        """

        return self._execute(query, params)

    def get_collector_status(self) -> list[dict[str, Any]]:
        """Get status of all collectors."""
        query = """
            WITH latest_runs AS (
                SELECT 
                    collector_name,
                    MAX(start_time) as last_run
                FROM collector_runs
                GROUP BY collector_name
            )
            SELECT 
                cr.collector_name,
                cr.start_time as last_run,
                cr.status,
                cr.records_collected,
                cr.error_message,
                CAST((julianday('now') - julianday(cr.start_time)) * 24 * 60 AS REAL) as minutes_since_run
            FROM collector_runs cr
            INNER JOIN latest_runs lr ON 
                cr.collector_name = lr.collector_name AND 
                cr.start_time = lr.last_run
            ORDER BY cr.collector_name
        """
        return self._execute(query)

    def get_disk_projections(self, days_ahead: int = 7) -> list[dict[str, Any]]:
        """Get disk fill projections."""
        query = """
            WITH latest AS (
                SELECT path, MAX(timestamp) as max_ts
                FROM filesystems
                GROUP BY path
            )
            SELECT 
                f.path,
                f.used_percent,
                f.fill_rate_bytes_per_day,
                f.days_until_full,
                f.total_bytes,
                f.used_bytes,
                CASE 
                    WHEN f.fill_rate_bytes_per_day > 0 THEN
                        f.used_bytes + (f.fill_rate_bytes_per_day * ?)
                    ELSE f.used_bytes
                END as projected_used_bytes,
                CASE 
                    WHEN f.fill_rate_bytes_per_day > 0 THEN
                        (f.used_bytes + (f.fill_rate_bytes_per_day * ?)) * 100.0 / f.total_bytes
                    ELSE f.used_percent
                END as projected_used_percent
            FROM filesystems f
            INNER JOIN latest l ON f.path = l.path AND f.timestamp = l.max_ts
            WHERE f.fill_rate_bytes_per_day IS NOT NULL
            ORDER BY f.days_until_full ASC NULLS LAST
        """
        return self._execute(query, [days_ahead, days_ahead])

    def get_job_health_distribution(self, hours_back: int = 24) -> dict[str, int]:
        """Get distribution of job health scores."""
        start_time = datetime.now() - timedelta(hours=hours_back)

        query = """
            SELECT 
                CASE 
                    WHEN health_score >= 0.9 THEN 'excellent'
                    WHEN health_score >= 0.7 THEN 'good'
                    WHEN health_score >= 0.5 THEN 'fair'
                    WHEN health_score >= 0.3 THEN 'poor'
                    ELSE 'critical'
                END as health_category,
                COUNT(*) as count
            FROM job_metrics_summary
            WHERE end_time >= ?
            GROUP BY health_category
            ORDER BY health_score DESC
        """

        results = self._execute(query, [start_time.isoformat()])
        return {row['health_category']: row['count'] for row in results}

    def cleanup_old_data(self, days_to_keep: int = 30) -> dict[str, int]:
        """Clean up old data from tables."""
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)
        deleted_counts = {}

        # Tables to clean with their timestamp columns
        tables_to_clean = [
            ('filesystems', 'timestamp'),
            ('quotas', 'timestamp'),
            ('slurm_queue_snapshots', 'timestamp'),
            ('alert_history', 'timestamp'),
            ('collector_runs', 'start_time'),
            ('job_metrics', 'timestamp'),
        ]

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            for table, timestamp_col in tables_to_clean:
                # Check if table exists
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)
                )
                if not cursor.fetchone():
                    continue

                # Delete old records
                cursor.execute(
                    f"DELETE FROM {table} WHERE {timestamp_col} < ?",
                    (cutoff_date.isoformat(),)
                )
                deleted_counts[table] = cursor.rowcount

            conn.commit()

        logger.info(f"Cleaned up old data: {deleted_counts}")
        return deleted_counts
