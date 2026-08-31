# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Database migration system for NOMAD."""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Migration scripts: (version, description, SQL)
MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "Initial schema", Path(__file__).parent.joinpath("schema.sql").read_text()),
    (2, "Add alert_history table", """
        CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
            message TEXT NOT NULL,
            metric_name TEXT,
            metric_value REAL,
            threshold_value REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            acknowledged BOOLEAN DEFAULT FALSE,
            acknowledged_by TEXT,
            acknowledged_at DATETIME
        );
        CREATE INDEX idx_alert_history_timestamp ON alert_history(timestamp);
        CREATE INDEX idx_alert_history_severity ON alert_history(severity);
    """),
    (3, "Add collector_runs table", """
        CREATE TABLE IF NOT EXISTS collector_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collector_name TEXT NOT NULL,
            start_time DATETIME NOT NULL,
            end_time DATETIME,
            status TEXT CHECK (status IN ('running', 'success', 'failed')),
            records_collected INTEGER DEFAULT 0,
            error_message TEXT,
            UNIQUE(collector_name, start_time)
        );
        CREATE INDEX idx_collector_runs_name ON collector_runs(collector_name);
    """),

    (4, "Add cluster column to node_state", """
        ALTER TABLE node_state ADD COLUMN cluster TEXT DEFAULT 'default';
        CREATE INDEX IF NOT EXISTS idx_node_state_cluster
            ON node_state(cluster);
    """),
    (5, "Add node_name to gpu_stats for SSH mode", """
        ALTER TABLE gpu_stats ADD COLUMN node_name TEXT DEFAULT '';
    """),
    (6, "Add workstation per-user tracking tables", """
        -- Per-user cgroup snapshots. One row per (hostname, username, timestamp).
        -- Stores cumulative counters; deltas are computed at query time, which is
        -- corruption-resistant across dropped collection cycles.
        CREATE TABLE IF NOT EXISTS workstation_user_snapshot (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp               DATETIME NOT NULL,
            hostname                TEXT    NOT NULL,
            username                TEXT    NOT NULL,
            uid                     INTEGER NOT NULL,
            -- cgroup slice ctime. Changes when user logs out and back in
            -- (slice destroyed and recreated). Delta queries MUST group by
            -- session_epoch to avoid computing nonsense across resets.
            session_epoch           INTEGER,
            cpu_usage_usec          INTEGER,
            cpu_user_usec           INTEGER,
            cpu_system_usec         INTEGER,
            memory_current_bytes    INTEGER,
            memory_peak_bytes       INTEGER,
            io_read_bytes           INTEGER,
            io_write_bytes          INTEGER,
            pids_current            INTEGER,
            collector_version       TEXT,
            source                  TEXT DEFAULT 'cgroup_v2'
        );
        CREATE INDEX IF NOT EXISTS idx_wus_host_user_ts
            ON workstation_user_snapshot(hostname, username, timestamp);
        CREATE INDEX IF NOT EXISTS idx_wus_timestamp
            ON workstation_user_snapshot(timestamp);
        CREATE INDEX IF NOT EXISTS idx_wus_session
            ON workstation_user_snapshot(hostname, username, session_epoch);

        -- Per-process historical record from pacct. Idempotent ingestion via
        -- UNIQUE(hostname, pid, start_time): re-running the collector never
        -- double-counts.
        CREATE TABLE IF NOT EXISTS workstation_process_record (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            hostname                TEXT    NOT NULL,
            username                TEXT    NOT NULL,
            uid                     INTEGER NOT NULL,
            pid                     INTEGER NOT NULL,
            ppid                    INTEGER,
            command                 TEXT,
            start_time              INTEGER NOT NULL,
            exit_time               INTEGER NOT NULL,
            elapsed_seconds         REAL,
            cpu_user_seconds        REAL,
            cpu_system_seconds      REAL,
            memory_avg_kb           INTEGER,
            io_chars                INTEGER,
            io_read_blocks          INTEGER,
            io_write_blocks         INTEGER,
            exit_code               INTEGER,
            flags                   INTEGER,
            ingested_at             DATETIME DEFAULT CURRENT_TIMESTAMP,
            collector_version       TEXT,
            UNIQUE(hostname, pid, start_time)
        );
        CREATE INDEX IF NOT EXISTS idx_wpr_host_user_start
            ON workstation_process_record(hostname, username, start_time);
        -- Partial index: queries about notable processes (>60s) are free;
        -- short-lived shell commands don't bloat the index.
        CREATE INDEX IF NOT EXISTS idx_wpr_long_running
            ON workstation_process_record(hostname, username, elapsed_seconds)
            WHERE elapsed_seconds > 60;

        -- Per-host ingestion watermark: avoids re-parsing the entire pacct
        -- file every collection cycle.
        CREATE TABLE IF NOT EXISTS workstation_pacct_cursor (
            hostname                TEXT PRIMARY KEY,
            last_exit_time          INTEGER NOT NULL,
            last_run_at             DATETIME NOT NULL,
            records_ingested_total  INTEGER DEFAULT 0
        );
    """),
    (7, "Add workstation_mount_state table", """
        -- Per-host per-mountpoint mount monitoring. Populated by
        -- nomad/collectors/mount_probe.py. Detects dead NFS mounts
        -- where the mount still appears in /proc/mounts but stat()
        -- hangs or times out.
        CREATE TABLE IF NOT EXISTS workstation_mount_state (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp               DATETIME NOT NULL,
            hostname                TEXT    NOT NULL,
            mountpoint              TEXT    NOT NULL,
            fstype                  TEXT,
            -- Full "server:/export" for NFS; device path for local.
            source                  TEXT,
            is_mounted              INTEGER NOT NULL,
            is_responsive           INTEGER NOT NULL,
            -- Milliseconds stat() took; equal to timeout_ms on timeout.
            response_ms             REAL,
            collected_at            INTEGER,
            probe_version           TEXT,
            collector_version       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_wms_host_ts
            ON workstation_mount_state(hostname, timestamp);
        CREATE INDEX IF NOT EXISTS idx_wms_mountpoint
            ON workstation_mount_state(hostname, mountpoint);
        CREATE INDEX IF NOT EXISTS idx_wms_responsive
            ON workstation_mount_state(hostname, mountpoint, is_responsive);
    """),
    (8, "Add per-user process tracking tables (Idea 18 Component 1)", """
        CREATE TABLE IF NOT EXISTS per_user_sample (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp               DATETIME NOT NULL,
            hostname                TEXT NOT NULL,
            role                    TEXT NOT NULL,
            username                TEXT NOT NULL,
            uid                     INTEGER NOT NULL,
            pid                     INTEGER NOT NULL,
            process_session_id      TEXT NOT NULL,
            command                 TEXT,
            cmdline                 TEXT,
            exe_path                TEXT,
            cpu_percent             REAL,
            memory_rss_bytes        INTEGER,
            memory_vms_bytes        INTEGER,
            num_threads             INTEGER,
            num_fds                 INTEGER,
            started_at              DATETIME,
            elapsed_seconds         REAL,
            ancestry_chain          TEXT,
            whitelist_match         TEXT,
            collector_version       TEXT,
            source                  TEXT NOT NULL DEFAULT 'psutil'
        );
        CREATE INDEX IF NOT EXISTS idx_pus_ts
            ON per_user_sample(timestamp);
        CREATE INDEX IF NOT EXISTS idx_pus_session
            ON per_user_sample(process_session_id);
        CREATE INDEX IF NOT EXISTS idx_pus_host_user_ts
            ON per_user_sample(hostname, username, timestamp);

        CREATE TABLE IF NOT EXISTS per_user_alert (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            fired_at                DATETIME NOT NULL,
            hostname                TEXT NOT NULL,
            role                    TEXT NOT NULL,
            username                TEXT NOT NULL,
            uid                     INTEGER NOT NULL,
            pid                     INTEGER NOT NULL,
            process_session_id      TEXT NOT NULL,
            rule_id                 TEXT NOT NULL,
            rule_type               TEXT NOT NULL,
            severity                TEXT NOT NULL,
            threshold_value         REAL,
            threshold_unit          TEXT,
            sustained_for_seconds   INTEGER,
            command                 TEXT,
            cmdline                 TEXT,
            ancestry_chain          TEXT,
            peak_cpu_percent        REAL,
            peak_memory_bytes       INTEGER,
            dedup_key               TEXT NOT NULL UNIQUE,
            occurrences             INTEGER NOT NULL DEFAULT 1,
            last_seen               DATETIME NOT NULL,
            acknowledged            BOOLEAN DEFAULT FALSE,
            resolved                BOOLEAN DEFAULT FALSE,
            resolved_at             DATETIME,
            notes                   TEXT,
            edu_template_id         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pua_fired
            ON per_user_alert(fired_at);
        CREATE INDEX IF NOT EXISTS idx_pua_user
            ON per_user_alert(hostname, username, fired_at);
        CREATE INDEX IF NOT EXISTS idx_pua_severity
            ON per_user_alert(severity, resolved);
        CREATE INDEX IF NOT EXISTS idx_pua_dedup
            ON per_user_alert(dedup_key);

        CREATE TABLE IF NOT EXISTS per_user_fd_sample (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp               DATETIME NOT NULL,
            hostname                TEXT NOT NULL,
            username                TEXT NOT NULL,
            uid                     INTEGER NOT NULL,
            pid                     INTEGER NOT NULL,
            process_session_id      TEXT NOT NULL,
            fs_bucket               TEXT NOT NULL,
            fd_count                INTEGER NOT NULL,
            representative_path     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pufd_ts
            ON per_user_fd_sample(timestamp);
        CREATE INDEX IF NOT EXISTS idx_pufd_session_bucket
            ON per_user_fd_sample(process_session_id, fs_bucket);
    """),
    (9, "Add avg_gpu_util to job_summary (per-job DCGM real utilization)", """
        ALTER TABLE job_summary ADD COLUMN avg_gpu_util REAL;
    """),
]


class MigrationManager:
    """Manages database schema migrations."""

    def __init__(self, db_path: Path):
        """Initialize migration manager."""
        self.db_path = db_path
        self.conn = None

    def __enter__(self):
        """Context manager entry."""
        self.conn = sqlite3.connect(self.db_path)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        if self.conn:
            self.conn.close()

    def get_current_version(self) -> int:
        """Get current database schema version."""
        cursor = self.conn.cursor()

        # Create migration tracking table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Get the highest applied version
        cursor.execute("SELECT MAX(version) FROM schema_migrations")
        result = cursor.fetchone()
        return result[0] if result[0] is not None else 0

    def apply_migration(self, version: int, description: str, sql: str) -> None:
        """Apply a single migration.

        Handles benign errors gracefully:
          - "duplicate column name" (column already exists from manual ALTER)
          - "already exists" (table/index created outside migrations)
        These are recorded as successful so the migration is not retried.
        """
        logger.info(f"Applying migration {version}: {description}")

        cursor = self.conn.cursor()
        try:
            # Execute the migration SQL
            cursor.executescript(sql)

            # Record the migration
            cursor.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (version, description)
            )

            self.conn.commit()
            logger.info(f"Migration {version} applied successfully")

        except sqlite3.OperationalError as e:
            err_msg = str(e).lower()
            if "duplicate column" in err_msg or "already exists" in err_msg:
                # Benign: schema element already present (manual ALTER, etc.)
                self.conn.rollback()
                try:
                    cursor.execute(
                        "INSERT INTO schema_migrations"
                        " (version, description) VALUES (?, ?)",
                        (version, description)
                    )
                    self.conn.commit()
                except Exception:
                    pass
                logger.warning(
                    f"Migration {version}: {e} (already applied, continuing)")
            else:
                self.conn.rollback()
                logger.error(f"Migration {version} failed: {e}")
                raise

        except Exception as e:
            self.conn.rollback()
            logger.error(f"Migration {version} failed: {e}")
            raise

    def migrate(self) -> int:
        """Run all pending migrations."""
        current_version = self.get_current_version()
        applied_count = 0

        for version, description, sql in MIGRATIONS:
            if version > current_version:
                self.apply_migration(version, description, sql)
                applied_count += 1

        if applied_count == 0:
            logger.info(f"Database is up to date (version {current_version})")
        else:
            new_version = self.get_current_version()
            logger.info(f"Applied {applied_count} migrations (now at version {new_version})")

        return applied_count

    def reset(self) -> None:
        """Reset database to fresh state (WARNING: destroys all data)."""
        logger.warning("Resetting database - all data will be lost!")

        cursor = self.conn.cursor()

        # Get all table names
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
        """)
        tables = cursor.fetchall()

        # Drop all tables
        for table in tables:
            cursor.execute(f"DROP TABLE IF EXISTS {table[0]}")

        self.conn.commit()
        logger.info("Database reset complete")

        # Re-run all migrations
        self.migrate()


def ensure_database(db_path: Path) -> None:
    """Ensure database exists and is up to date."""
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with MigrationManager(db_path) as mgr:
        mgr.migrate()
