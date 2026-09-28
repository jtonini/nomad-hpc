# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ Cloud Base Collector

Abstract base class for cloud provider collectors. Extends BaseCollector
with cloud-specific functionality:

    - Credential management (API keys, IAM roles, service accounts)
    - API pagination and rate-limit handling
    - Metric normalization: cloud-native metrics → NØMAÐ schema
    - Cost data integration (separate from monitoring metrics)
    - Instance-to-node mapping

Subclasses implement provider-specific API calls; the base class handles
schema normalization so that all downstream NØMAÐ components (derivatives,
TESSERA, alerting, dashboards) work unchanged.
"""

import logging
import time
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# Import from the existing collector framework
from nomad.collectors.base import BaseCollector, CollectionError

logger = logging.getLogger(__name__)


# ── Exceptions ──────────────────────────────────────────────────────────

class CloudAuthError(CollectionError):
    """Raised when cloud API authentication fails."""
    pass


class CloudAPIError(CollectionError):
    """Raised when a cloud API call fails after retries."""
    pass


class CloudQuotaError(CollectionError):
    """Raised when API rate limits are exceeded and backoff is exhausted."""
    pass


# ── Data classes ────────────────────────────────────────────────────────

@dataclass
class CloudCredential:
    """
    Provider-agnostic credential container.

    Each provider subclass populates the fields it needs:
        AWS  → access_key_id, secret_access_key, (optional) session_token
        Azure → tenant_id, client_id, client_secret  (or managed identity)
        GCP  → service_account_json path  (or workload identity)

    The ``provider`` field is set by the subclass and used for logging.
    """
    provider: str
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None        # AWS STS
    tenant_id: str | None = None            # Azure
    client_id: str | None = None            # Azure
    client_secret: str | None = None        # Azure
    service_account_json: str | None = None # GCP
    region: str | None = None
    profile: str | None = None              # AWS named profile
    role_arn: str | None = None             # AWS assume-role


@dataclass
class CloudMetric:
    """
    A single normalized metric reading, ready for the NØMAÐ schema.

    This is the contract between cloud collectors and the rest of the
    pipeline. Every provider-specific metric must be transformed into
    one or more CloudMetric instances.

    Fields mirror the on-prem collector schema so that downstream
    analysis (derivatives, TESSERA, alerting) sees no difference.
    """
    timestamp: datetime
    node_name: str          # maps to instance_id / VM name / GCE name
    cluster: str            # logical grouping (AWS account, Azure subscription, etc.)
    metric_name: str        # normalized name: cpu_util, mem_util, disk_read_bytes, ...
    value: float
    unit: str               # percent, bytes, bytes/sec, count, etc.
    source: str             # "aws", "azure", "gcp"

    # Optional enrichment
    instance_type: str | None = None
    availability_zone: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    # Cost (populated by cost collectors, None for monitoring metrics)
    cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for database insertion."""
        d = {
            "timestamp": self.timestamp.isoformat(),
            "node_name": self.node_name,
            "cluster": self.cluster,
            "metric_name": self.metric_name,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
        }
        if self.instance_type:
            d["instance_type"] = self.instance_type
        if self.availability_zone:
            d["availability_zone"] = self.availability_zone
        if self.tags:
            d["tags"] = str(self.tags)
        if self.cost_usd is not None:
            d["cost_usd"] = self.cost_usd
        return d


# ── Metric name normalization map ───────────────────────────────────────

# Provider-native metric names → NØMAÐ canonical names.
# Each provider subclass extends this with its own mappings.
CANONICAL_METRICS = {
    # CPU
    "cpu_util": "cpu_util",
    "cpu_utilization": "cpu_util",
    # Memory
    "mem_util": "mem_util",
    "mem_used_percent": "mem_util",
    "memory_utilization": "mem_util",
    # Disk I/O
    "disk_read_bytes": "disk_read_bytes",
    "disk_write_bytes": "disk_write_bytes",
    "disk_read_ops": "disk_read_ops",
    "disk_write_ops": "disk_write_ops",
    # Network
    "net_in_bytes": "net_recv_bytes",
    "net_out_bytes": "net_send_bytes",
    "network_in": "net_recv_bytes",
    "network_out": "net_send_bytes",
    # GPU (if applicable)
    "gpu_util": "gpu_util",
    "gpu_mem_util": "gpu_mem_util",
}


# ── Cloud base collector ────────────────────────────────────────────────

class CloudBaseCollector(BaseCollector):
    """
    Abstract base for cloud provider collectors.

    Extends :class:`BaseCollector` with:

    1. **Credential lifecycle** — load / validate / refresh.
    2. **Paginated API calls** with exponential backoff.
    3. **Metric normalization** to :class:`CloudMetric`.
    4. **Cost data** as a parallel collection stream.

    Subclasses must implement:
        - :meth:`_authenticate`      — set up the API client
        - :meth:`_collect_metrics`    — fetch monitoring metrics
        - :meth:`_collect_cost`       — fetch cost/billing data (optional)
        - :meth:`_normalize_metric`   — provider-native → CloudMetric
        - :meth:`_list_instances`     — enumerate monitored resources
    """

    name: str = "cloud_base"
    description: str = "Cloud provider base collector"
    default_interval: int = 300  # 5 minutes, matches CloudWatch basic monitoring

    # Retry / rate-limit defaults
    MAX_RETRIES: int = 3
    RETRY_BACKOFF_BASE: float = 1.0   # seconds; doubles each retry
    RATE_LIMIT_SLEEP: float = 1.0     # seconds between paginated calls

    def __init__(self, config: dict[str, Any] | None = None, db_path: str | None = None) -> None:
        cfg = config or {}
        db = db_path or cfg.get("db_path", "nomad.db")
        super().__init__(cfg, db)
        self._config = cfg
        self._credential: CloudCredential | None = None
        self._client: Any = None  # provider SDK client, set by subclass
        self._authenticated = False

        # Collection window: how far back to fetch on each cycle
        self._lookback_minutes: int = self._config.get("lookback_minutes", 10)

        # Instance filters (tag-based)
        self._tag_filters: dict[str, str] = self._config.get("tag_filters", {})

        # Cost collection toggle
        self._collect_cost_enabled: bool = self._config.get("collect_cost", False)

    # ── Credential management ───────────────────────────────────────

    def _load_credential(self) -> CloudCredential:
        """
        Build a CloudCredential from config.

        Config keys (in ``nomad.toml`` under ``[collectors.cloud.<provider>]``):
            access_key_id, secret_access_key, region, profile, role_arn, ...

        Environment variables (``NOMAD_AWS_ACCESS_KEY``, etc.) override
        config-file values, following 12-factor conventions.
        """
        import os

        def _env_or_cfg(env_key: str, cfg_key: str) -> str | None:
            return os.environ.get(env_key) or self._config.get(cfg_key)

        return CloudCredential(
            provider=self.name,
            access_key_id=_env_or_cfg("NOMAD_AWS_ACCESS_KEY_ID", "access_key_id"),
            secret_access_key=_env_or_cfg(
                "NOMAD_AWS_SECRET_ACCESS_KEY", "secret_access_key"
            ),
            session_token=_env_or_cfg("NOMAD_AWS_SESSION_TOKEN", "session_token"),
            region=_env_or_cfg("NOMAD_AWS_REGION", "region"),
            profile=_env_or_cfg("NOMAD_AWS_PROFILE", "profile"),
            role_arn=_env_or_cfg("NOMAD_AWS_ROLE_ARN", "role_arn"),
            tenant_id=_env_or_cfg("NOMAD_AZURE_TENANT_ID", "tenant_id"),
            client_id=_env_or_cfg("NOMAD_AZURE_CLIENT_ID", "client_id"),
            client_secret=_env_or_cfg("NOMAD_AZURE_CLIENT_SECRET", "client_secret"),
            service_account_json=_env_or_cfg(
                "NOMAD_GCP_SERVICE_ACCOUNT", "service_account_json"
            ),
        )

    @abstractmethod
    def _authenticate(self) -> None:
        """
        Set up the provider SDK client using ``self._credential``.

        Must set ``self._client`` and ``self._authenticated = True``
        on success, or raise :class:`CloudAuthError`.
        """
        ...

    def _ensure_authenticated(self) -> None:
        """Authenticate if not already done."""
        if not self._authenticated:
            self._credential = self._load_credential()
            self._authenticate()

    # ── Paginated API calls with retry ──────────────────────────────

    def _api_call_with_retry(
        self,
        call_fn,
        *args,
        max_retries: int | None = None,
        **kwargs,
    ) -> Any:
        """
        Execute ``call_fn(*args, **kwargs)`` with exponential backoff.

        Handles:
            - Throttling (HTTP 429 / TooManyRequests)
            - Transient server errors (5xx)
            - Token expiration → re-authenticate once, then retry

        Returns the raw API response on success.
        Raises :class:`CloudAPIError` after all retries exhausted.
        """
        retries = max_retries if max_retries is not None else self.MAX_RETRIES

        for attempt in range(retries + 1):
            try:
                return call_fn(*args, **kwargs)
            except Exception as exc:
                exc_str = str(exc).lower()
                is_throttle = any(
                    t in exc_str
                    for t in ("throttl", "rate", "toomanyrequests", "429")
                )
                is_auth = any(
                    t in exc_str
                    for t in ("expired", "credential", "unauthorized", "403")
                )
                is_server = any(t in exc_str for t in ("500", "502", "503", "504"))

                if is_auth and attempt == 0:
                    logger.warning(
                        "%s: credential error, re-authenticating: %s",
                        self.name, exc,
                    )
                    self._authenticated = False
                    self._ensure_authenticated()
                    continue

                if (is_throttle or is_server) and attempt < retries:
                    sleep_time = self.RETRY_BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "%s: retryable error (attempt %d/%d), "
                        "sleeping %.1fs: %s",
                        self.name, attempt + 1, retries, sleep_time, exc,
                    )
                    time.sleep(sleep_time)
                    continue

                raise CloudAPIError(
                    f"{self.name}: API call failed after {attempt + 1} "
                    f"attempts: {exc}"
                ) from exc

        raise CloudAPIError(f"{self.name}: API call failed after {retries} retries")

    # ── Abstract provider methods ───────────────────────────────────

    @abstractmethod
    def _list_instances(self) -> list[dict[str, Any]]:
        """
        Return metadata for all monitored instances/VMs.

        Each dict should contain at minimum:
            instance_id, instance_type, availability_zone, tags, state
        """
        ...

    @abstractmethod
    def _collect_metrics(
        self, start: datetime, end: datetime
    ) -> list[CloudMetric]:
        """
        Fetch monitoring metrics for the collection window.

        ``start`` and ``end`` define the time range (UTC).
        Returns a list of normalized :class:`CloudMetric` instances.
        """
        ...

    def _collect_cost(
        self, start: datetime, end: datetime
    ) -> list[CloudMetric]:
        """
        Fetch cost/billing data for the collection window.

        Optional — default implementation returns empty list.
        Override to pull from AWS Cost Explorer, Azure Cost Management, etc.
        """
        return []

    @abstractmethod
    def _normalize_metric(
        self, raw_metric: dict[str, Any], instance: dict[str, Any]
    ) -> CloudMetric:
        """
        Convert a provider-native metric reading into a :class:`CloudMetric`.

        ``raw_metric`` is whatever the provider API returned.
        ``instance`` is the instance metadata from :meth:`_list_instances`.
        """
        ...

    # ── Metric name normalization helper ────────────────────────────

    def _canonicalize(self, metric_name: str) -> str:
        """
        Map a provider-native metric name to the NØMAÐ canonical name.

        Falls through to the original name if no mapping exists, so that
        provider-specific metrics are still collected (just not normalized).
        """
        return CANONICAL_METRICS.get(metric_name.lower(), metric_name)

    # ── BaseCollector interface ─────────────────────────────────────

    def collect(self) -> list[dict[str, Any]]:
        """
        Main collection entry point (implements BaseCollector.collect).

        1. Authenticate
        2. Determine collection window
        3. Fetch monitoring metrics
        4. Optionally fetch cost data
        5. Return normalized dicts for store()
        """
        self._ensure_authenticated()

        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=self._lookback_minutes)

        # Monitoring metrics
        metrics = self._collect_metrics(start, now)
        logger.info(
            "%s: collected %d monitoring metrics (%s → %s)",
            self.name, len(metrics),
            start.strftime("%H:%M:%S"), now.strftime("%H:%M:%S"),
        )

        # Cost metrics (if enabled)
        if self._collect_cost_enabled:
            cost_metrics = self._collect_cost(start, now)
            metrics.extend(cost_metrics)
            logger.info(
                "%s: collected %d cost metrics", self.name, len(cost_metrics)
            )

        return [m.to_dict() for m in metrics]

    def store(self, data: list[dict[str, Any]]) -> None:
        """
        Store normalized cloud metrics in the NØMAÐ database.

        Uses the same ``cloud_metrics`` table for all providers, with a
        ``source`` column to distinguish AWS / Azure / GCP.
        """
        if not data:
            return

        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()

            cursor.executescript("""
                CREATE TABLE IF NOT EXISTS cloud_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    cluster TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    value REAL NOT NULL,
                    unit TEXT NOT NULL,
                    source TEXT NOT NULL,
                    instance_type TEXT,
                    availability_zone TEXT,
                    tags TEXT,
                    cost_usd REAL
                );

                CREATE INDEX IF NOT EXISTS idx_cloud_metrics_ts
                    ON cloud_metrics(timestamp);

                CREATE INDEX IF NOT EXISTS idx_cloud_metrics_node
                    ON cloud_metrics(node_name, timestamp);

                CREATE INDEX IF NOT EXISTS idx_cloud_metrics_source
                    ON cloud_metrics(source, timestamp);

                CREATE INDEX IF NOT EXISTS idx_cloud_metrics_metric
                    ON cloud_metrics(metric_name, source, timestamp);
            """)

            cursor.executemany(
                """
                INSERT INTO cloud_metrics
                    (timestamp, node_name, cluster, metric_name, value,
                     unit, source, instance_type, availability_zone,
                     tags, cost_usd)
                VALUES
                    (:timestamp, :node_name, :cluster, :metric_name, :value,
                     :unit, :source, :instance_type, :availability_zone,
                     :tags, :cost_usd)
                """,
                [
                    {
                        "timestamp": d["timestamp"],
                        "node_name": d["node_name"],
                        "cluster": d["cluster"],
                        "metric_name": d["metric_name"],
                        "value": d["value"],
                        "unit": d["unit"],
                        "source": d["source"],
                        "instance_type": d.get("instance_type"),
                        "availability_zone": d.get("availability_zone"),
                        "tags": d.get("tags"),
                        "cost_usd": d.get("cost_usd"),
                    }
                    for d in data
                ],
            )

            conn.commit()
            logger.info(
                "%s: stored %d metrics in cloud_metrics table",
                self.name, len(data),
            )
        finally:
            conn.close()
