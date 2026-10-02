# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ Base Collector Framework

All collectors inherit from BaseCollector and implement the collect() method.
The framework handles scheduling, error handling, logging, and database storage.
"""

import logging
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Where system tools live that cron's PATH (/usr/bin:/bin) leaves out:
# nfs-utils puts nfsiostat in /usr/sbin, ZFS puts zpool there. nomad
# reported "nfsiostat not installed" at four sites where it was installed.
EXTRA_TOOL_DIRS = ("/usr/sbin", "/sbin", "/usr/local/sbin", "/usr/local/bin")


def find_tool(name: str) -> str | None:
    """Full path of a command, looking past cron's short PATH."""
    import shutil
    return shutil.which(name) or shutil.which(name, path=":".join(EXTRA_TOOL_DIRS))


def tool_env() -> dict:
    """The environment with EXTRA_TOOL_DIRS appended to PATH, for commands
    run through a shell."""
    import os
    env = dict(os.environ)
    path = env.get("PATH", "")
    extra = [d for d in EXTRA_TOOL_DIRS if d not in path.split(":")]
    env["PATH"] = ":".join([p for p in [path] if p] + extra)
    return env

# Alert integration (optional)
try:
    from nomad.alerts.thresholds import check_and_alert
    HAS_ALERTS = True
except ImportError:
    HAS_ALERTS = False


@dataclass
class CollectionResult:
    """Result of a collection run."""

    collector_name: str
    timestamp: datetime
    success: bool
    records_collected: int = 0
    duration_seconds: float = 0.0
    error_message: str | None = None
    data: list[dict[str, Any]] = field(default_factory=list)
    # Why a successful run collected nothing (or less than it could):
    # "nfsiostat not installed", "no network_tests configured".
    note: str | None = None

    def __repr__(self) -> str:
        status = "✓" if self.success else "✗"
        return (
            f"CollectionResult({status} {self.collector_name}: "
            f"{self.records_collected} records in {self.duration_seconds:.2f}s)"
        )


class BaseCollector(ABC):
    """
    Abstract base class for all NØMAÐ collectors.
    
    Subclasses must implement:
        - collect() -> list[dict]: Gather data from the source
        - store(data) -> None: Store data in the database
    
    The framework provides:
        - Error handling and retry logic
        - Logging
        - Timing and metrics
        - Database connection management
    
    Example:
        class DiskCollector(BaseCollector):
            name = "disk"
            
            def collect(self) -> list[dict]:
                # Gather filesystem data
                return [{'path': '/', 'used_percent': 50.0}]
            
            def store(self, data: list[dict]) -> None:
                # Store in database
                ...
    """

    # Subclasses should override these
    name: str = "base"
    description: str = "Base collector"
    default_interval: int = 300  # seconds

    def __init__(
        self,
        config: dict[str, Any],
        db_path: Path | str,
    ):
        """
        Initialize the collector.
        
        Args:
            config: Collector-specific configuration dict
            db_path: Path to SQLite database
        """
        self.config = config
        self.db_path = Path(db_path)
        self._last_run: datetime | None = None
        self._consecutive_failures = 0
        self._max_retries = config.get('max_retries', 3)
        self._retry_delay = config.get('retry_delay', 5)  # seconds
        # Set by collect() when it has nothing to do for a known reason, so
        # the run says why instead of logging "success, 0 records".
        self.note: str | None = None

        logger.info(f"Initialized {self.name} collector")

    @abstractmethod
    def collect(self) -> list[dict[str, Any]]:
        """
        Collect data from the source.
        
        Returns:
            List of dictionaries containing collected data.
            Each dict should have keys matching the database schema.
        
        Raises:
            CollectionError: If data collection fails.
        """
        pass

    @abstractmethod
    def store(self, data: list[dict[str, Any]]) -> None:
        """
        Store collected data in the database.
        
        Args:
            data: List of data dictionaries from collect()
        """
        pass

    def get_db_connection(self) -> sqlite3.Connection:
        """Get a database connection with row factory.

        Waits up to 30 s for a lock: another collector, or a sync copying the
        database, holds it briefly. The default 5 s made runs fail with
        "database is locked" (about 1 in 100 on busy hosts).
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def count_records(self, data: list[dict[str, Any]]) -> int:
        """Records a run collected. Collectors that return one envelope
        (groups, per_user) override this to count what is inside it."""
        return len(data)

    def run(self) -> CollectionResult:
        """
        Execute a collection run with error handling and timing.
        
        Returns:
            CollectionResult with success status and metrics.
        """
        start_time = time.time()
        timestamp = datetime.now()
        self.note = None

        logger.debug(f"Starting {self.name} collection")

        try:
            # Collect data with retry logic
            data = self._collect_with_retry()

            # Store data
            if data:
                self.store(data)

                # Check thresholds and trigger alerts
                if HAS_ALERTS and self.config.get('alerts_enabled', True):
                    try:
                        import socket
                        host = socket.gethostname()
                        # Get full config from registry if available
                        full_config = getattr(registry, '_config', {})
                        check_and_alert(self.name, data, full_config, host=host)
                    except Exception as e:
                        logger.debug(f"Alert check skipped: {e}")

            duration = time.time() - start_time
            self._last_run = timestamp
            self._consecutive_failures = 0

            count = self.count_records(data) if data else 0
            result = CollectionResult(
                collector_name=self.name,
                timestamp=timestamp,
                success=True,
                records_collected=count,
                duration_seconds=duration,
                data=data,
                note=self.note,
            )

            logger.info(f"{self.name}: Collected {count} records in {duration:.2f}s"
                        + (f" ({self.note})" if self.note else ""))
            self._log_collection_run(result)

            return result

        except Exception as e:
            duration = time.time() - start_time
            self._consecutive_failures += 1

            result = CollectionResult(
                collector_name=self.name,
                timestamp=timestamp,
                success=False,
                duration_seconds=duration,
                error_message=str(e),
            )

            logger.error(f"{self.name}: Collection failed - {e}")
            self._log_collection_run(result)

            return result

    def _collect_with_retry(self) -> list[dict[str, Any]]:
        """Attempt collection with retries on failure."""
        last_error = None

        for attempt in range(self._max_retries):
            try:
                return self.collect()
            except MissingToolError:
                raise
            except Exception as e:
                last_error = e
                if attempt < self._max_retries - 1:
                    logger.warning(
                        f"{self.name}: Attempt {attempt + 1} failed, "
                        f"retrying in {self._retry_delay}s: {e}"
                    )
                    time.sleep(self._retry_delay)

        raise CollectionError(
            f"Collection failed after {self._max_retries} attempts: {last_error}"
        )

    def _log_collection_run(self, result: CollectionResult) -> None:
        """Log collection run to database."""
        try:
            with self.get_db_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO collection_log 
                    (collector, started_at, completed_at, success, records_collected, error_message)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.collector_name,
                        result.timestamp.isoformat(),
                        datetime.now().isoformat(),
                        result.success,
                        result.records_collected,
                        # On a successful run, the note saying why it
                        # collected nothing (or less than it could).
                        result.error_message or result.note,
                    )
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to log collection run: {e}")

    @property
    def interval(self) -> int:
        """Get collection interval from config or default."""
        return self.config.get('interval', self.default_interval)

    @property
    def enabled(self) -> bool:
        """Check if collector is enabled in config."""
        return self.config.get('enabled', True)

    @property
    def last_run(self) -> datetime | None:
        """Timestamp of last successful run."""
        return self._last_run

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive failed runs."""
        return self._consecutive_failures

    def should_run(self) -> bool:
        """Check if collector should run based on interval."""
        if not self.enabled:
            return False
        if self._last_run is None:
            return True
        elapsed = (datetime.now() - self._last_run).total_seconds()
        return elapsed >= self.interval


class CollectionError(Exception):
    """Raised when data collection fails."""
    pass


class MissingToolError(CollectionError):
    """A command the collector needs isn't installed: retrying won't help,
    so the run fails at once with the reason (it used to retry twice, 5 s
    apart, on every run)."""
    pass


class CollectorRegistry:
    """Registry of available collectors."""

    def __init__(self):
        self._collectors: dict[str, type[BaseCollector]] = {}

    def register(self, collector_class: type[BaseCollector]) -> type[BaseCollector]:
        """
        Register a collector class.
        
        Can be used as a decorator:
            @registry.register
            class MyCollector(BaseCollector):
                ...
        """
        self._collectors[collector_class.name] = collector_class
        logger.debug(f"Registered collector: {collector_class.name}")
        return collector_class

    def get(self, name: str) -> type[BaseCollector] | None:
        """Get a collector class by name."""
        return self._collectors.get(name)

    def list_collectors(self) -> list[str]:
        """List all registered collector names."""
        return list(self._collectors.keys())

    def create(
        self,
        name: str,
        config: dict[str, Any],
        db_path: Path | str,
    ) -> BaseCollector:
        """
        Create a collector instance by name.
        
        Args:
            name: Collector name
            config: Collector configuration
            db_path: Database path
            
        Returns:
            Configured collector instance
            
        Raises:
            ValueError: If collector name not found
        """
        collector_class = self.get(name)
        if collector_class is None:
            raise ValueError(f"Unknown collector: {name}")
        return collector_class(config, db_path)


# Global registry
registry = CollectorRegistry()
