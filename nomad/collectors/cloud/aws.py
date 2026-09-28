# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ AWS Cloud Collector

Collects metrics from AWS CloudWatch and (optionally) Cost Explorer,
normalizing them into the NØMAÐ schema for downstream analysis.

Supported resources:
    - EC2 instances (CPU, network, disk, status checks)
    - EBS volumes (read/write ops and bytes)
    - GPU instances (via CloudWatch agent metrics)

Authentication (in priority order):
    1. Explicit credentials in nomad.toml or environment variables
    2. AWS named profile (``profile`` config key)
    3. IAM instance role (when running on EC2)
    4. Assume-role via STS (``role_arn`` config key)

Configuration example (nomad.toml):
    [collectors.cloud.aws]
    enabled = true
    region = "us-east-1"
    profile = "research-account"
    lookback_minutes = 10
    collect_cost = true
    tag_filters = { "Environment" = "research" }
    metric_period = 300
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .cloud_base import (
    CloudBaseCollector,
    CloudAuthError,
    CloudMetric,
    CloudCredential,
)
from nomad.collectors.base import registry

logger = logging.getLogger(__name__)

# ── boto3 availability check ────────────────────────────────────────

try:
    import boto3
    from botocore.exceptions import (
        ClientError,
        NoCredentialsError,
        EndpointConnectionError,
    )
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False


# ── AWS metric → NØMAÐ canonical metric mapping ────────────────────

AWS_METRIC_MAP: dict[str, tuple[str, str]] = {
    # (CloudWatch MetricName) → (NØMAÐ canonical name, unit)
    # EC2
    "CPUUtilization":       ("cpu_util",         "percent"),
    "NetworkIn":            ("net_recv_bytes",    "bytes"),
    "NetworkOut":           ("net_send_bytes",    "bytes"),
    "DiskReadBytes":        ("disk_read_bytes",   "bytes"),
    "DiskWriteBytes":       ("disk_write_bytes",  "bytes"),
    "DiskReadOps":          ("disk_read_ops",     "count"),
    "DiskWriteOps":         ("disk_write_ops",    "count"),
    "StatusCheckFailed":    ("status_check_fail", "count"),
    # EBS
    "VolumeReadBytes":      ("disk_read_bytes",   "bytes"),
    "VolumeWriteBytes":     ("disk_write_bytes",  "bytes"),
    "VolumeReadOps":        ("disk_read_ops",     "count"),
    "VolumeWriteOps":       ("disk_write_ops",    "count"),
    "VolumeQueueLength":    ("disk_queue_length", "count"),
    # CloudWatch Agent (memory — requires CW agent on instances)
    "mem_used_percent":     ("mem_util",          "percent"),
    # CloudWatch Agent (GPU — requires CW agent + NVIDIA driver)
    "nvidia_smi_utilization_gpu": ("gpu_util",    "percent"),
    "nvidia_smi_utilization_memory": ("gpu_mem_util", "percent"),
}

# EC2 namespace metrics to collect by default
DEFAULT_EC2_METRICS = [
    "CPUUtilization",
    "NetworkIn",
    "NetworkOut",
    "DiskReadBytes",
    "DiskWriteBytes",
    "StatusCheckFailed",
]

# CW Agent metrics (only available if agent is installed)
CW_AGENT_METRICS = [
    "mem_used_percent",
    "nvidia_smi_utilization_gpu",
    "nvidia_smi_utilization_memory",
]


# ── AWS Collector ───────────────────────────────────────────────────

@registry.register
class AWSCollector(CloudBaseCollector):
    """
    Collect EC2 / EBS / GPU metrics from AWS CloudWatch and normalize
    them into the NØMAÐ schema.

    All metrics land in the ``cloud_metrics`` table with
    ``source = 'aws'``, where they are indistinguishable from on-prem
    metrics for downstream analysis, TESSERA, and alerting.
    """

    name = "aws"
    description = "AWS CloudWatch and Cost Explorer collector"
    default_interval = 300  # matches CloudWatch basic monitoring period

    def __init__(self, config: dict[str, Any] | None = None, db_path: str | None = None) -> None:
        if not HAS_BOTO3:
            raise ImportError(
                "AWS collector requires boto3. "
                "Install with: pip install boto3"
            )
        super().__init__(config, db_path)

        self._metric_period: int = self._config.get("metric_period", 300)
        self._ec2_metrics: list[str] = self._config.get(
            "ec2_metrics", DEFAULT_EC2_METRICS
        )
        self._collect_cw_agent: bool = self._config.get(
            "collect_cw_agent_metrics", True
        )
        self._account_alias: str = self._config.get("account_alias", "aws")

        # SDK clients (set in _authenticate)
        self._cw_client: Any = None
        self._ec2_client: Any = None
        self._ce_client: Any = None  # Cost Explorer

    # ── Authentication ──────────────────────────────────────────────

    def _authenticate(self) -> None:
        """
        Set up boto3 clients using the loaded credential.

        Supports: explicit keys, named profile, instance role, assume-role.
        """
        cred = self._credential
        if cred is None:
            raise CloudAuthError("No credential loaded for AWS collector")

        session_kwargs: dict[str, Any] = {}

        if cred.profile:
            session_kwargs["profile_name"] = cred.profile
        if cred.region:
            session_kwargs["region_name"] = cred.region

        try:
            session = boto3.Session(**session_kwargs)

            # If assume-role is configured, get temporary credentials via STS
            if cred.role_arn:
                sts = session.client(
                    "sts",
                    aws_access_key_id=cred.access_key_id,
                    aws_secret_access_key=cred.secret_access_key,
                )
                assumed = sts.assume_role(
                    RoleArn=cred.role_arn,
                    RoleSessionName="nomad-cloud-collector",
                    DurationSeconds=3600,
                )["Credentials"]
                session = boto3.Session(
                    aws_access_key_id=assumed["AccessKeyId"],
                    aws_secret_access_key=assumed["SecretAccessKey"],
                    aws_session_token=assumed["SessionToken"],
                    region_name=cred.region,
                )

            # Build SDK clients
            client_kwargs: dict[str, Any] = {}
            if cred.access_key_id and not cred.role_arn:
                client_kwargs["aws_access_key_id"] = cred.access_key_id
                client_kwargs["aws_secret_access_key"] = cred.secret_access_key
                if cred.session_token:
                    client_kwargs["aws_session_token"] = cred.session_token

            self._cw_client = session.client("cloudwatch", **client_kwargs)
            self._ec2_client = session.client("ec2", **client_kwargs)

            if self._collect_cost_enabled:
                # Cost Explorer is only available in us-east-1
                self._ce_client = session.client(
                    "ce",
                    region_name="us-east-1",
                    **client_kwargs,
                )

            # Validate credentials with a lightweight call
            self._api_call_with_retry(
                self._ec2_client.describe_account_attributes,
                AttributeNames=["default-vpc"],
            )

            self._authenticated = True
            logger.info(
                "AWS collector authenticated (region=%s, profile=%s)",
                cred.region or "default",
                cred.profile or "none",
            )

        except NoCredentialsError as exc:
            raise CloudAuthError(
                "No AWS credentials found. Configure credentials in "
                "nomad.toml, environment variables, or use an IAM role."
            ) from exc
        except (ClientError, EndpointConnectionError) as exc:
            raise CloudAuthError(
                f"AWS authentication failed: {exc}"
            ) from exc

    # ── Instance discovery ──────────────────────────────────────────

    def _list_instances(self) -> list[dict[str, Any]]:
        """
        Enumerate EC2 instances, optionally filtered by tags.

        Returns list of dicts with:
            instance_id, instance_type, availability_zone, state, tags, name
        """
        filters: list[dict[str, Any]] = [
            {"Name": "instance-state-name", "Values": ["running"]},
        ]

        # Apply tag-based filters from config
        for tag_key, tag_value in self._tag_filters.items():
            filters.append({
                "Name": f"tag:{tag_key}",
                "Values": [tag_value],
            })

        instances: list[dict[str, Any]] = []
        paginator = self._ec2_client.get_paginator("describe_instances")

        for page in self._api_call_with_retry(
            lambda: paginator.paginate(Filters=filters)
        ):
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    tags = {
                        t["Key"]: t["Value"]
                        for t in inst.get("Tags", [])
                    }
                    instances.append({
                        "instance_id": inst["InstanceId"],
                        "instance_type": inst["InstanceType"],
                        "availability_zone": inst["Placement"]["AvailabilityZone"],
                        "state": inst["State"]["Name"],
                        "tags": tags,
                        "name": tags.get("Name", inst["InstanceId"]),
                    })

        logger.info("AWS: discovered %d running instances", len(instances))
        return instances

    # ── Metric collection ───────────────────────────────────────────

    def _collect_metrics(
        self, start: datetime, end: datetime
    ) -> list[CloudMetric]:
        """
        Fetch CloudWatch metrics for all discovered instances.

        Collects:
            - EC2 namespace metrics (CPU, network, disk, status)
            - CWAgent namespace metrics (memory, GPU) if agent is installed
        """
        instances = self._list_instances()
        if not instances:
            logger.warning("AWS: no instances found matching filters")
            return []

        metrics: list[CloudMetric] = []

        for instance in instances:
            iid = instance["instance_id"]

            # EC2 namespace metrics
            for metric_name in self._ec2_metrics:
                raw = self._get_metric_data(
                    namespace="AWS/EC2",
                    metric_name=metric_name,
                    dimensions=[
                        {"Name": "InstanceId", "Value": iid},
                    ],
                    start=start,
                    end=end,
                )
                for point in raw:
                    metrics.append(
                        self._normalize_metric(point, instance)
                    )

            # CW Agent metrics (best-effort — may not be installed)
            if self._collect_cw_agent:
                for metric_name in CW_AGENT_METRICS:
                    try:
                        raw = self._get_metric_data(
                            namespace="CWAgent",
                            metric_name=metric_name,
                            dimensions=[
                                {"Name": "InstanceId", "Value": iid},
                            ],
                            start=start,
                            end=end,
                        )
                        for point in raw:
                            metrics.append(
                                self._normalize_metric(point, instance)
                            )
                    except Exception:
                        # CW Agent metrics are optional; skip silently
                        pass

        return metrics

    def _get_metric_data(
        self,
        namespace: str,
        metric_name: str,
        dimensions: list[dict[str, str]],
        start: datetime,
        end: datetime,
        stat: str = "Average",
    ) -> list[dict[str, Any]]:
        """
        Fetch a single metric's datapoints from CloudWatch.

        Returns list of dicts with: timestamp, value, metric_name, namespace.
        """
        response = self._api_call_with_retry(
            self._cw_client.get_metric_statistics,
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=start,
            EndTime=end,
            Period=self._metric_period,
            Statistics=[stat],
        )

        points = []
        for dp in response.get("Datapoints", []):
            points.append({
                "timestamp": dp["Timestamp"],
                "value": dp[stat],
                "metric_name": metric_name,
                "namespace": namespace,
            })

        return sorted(points, key=lambda p: p["timestamp"])

    # ── Cost collection ─────────────────────────────────────────────

    def _collect_cost(
        self, start: datetime, end: datetime
    ) -> list[CloudMetric]:
        """
        Fetch per-instance cost data from AWS Cost Explorer.

        Cost Explorer has a minimum granularity of DAILY and charges
        $0.01 per API request, so we only call it when explicitly enabled
        and limit to daily aggregation.
        """
        if self._ce_client is None:
            return []

        # Cost Explorer requires date strings, not datetimes
        # Use yesterday → today for daily cost (CE data has ~24h lag)
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)

        try:
            response = self._api_call_with_retry(
                self._ce_client.get_cost_and_usage,
                TimePeriod={
                    "Start": yesterday.isoformat(),
                    "End": today.isoformat(),
                },
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
                GroupBy=[
                    {"Type": "DIMENSION", "Key": "SERVICE"},
                    {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
                ],
            )
        except Exception as exc:
            logger.warning("AWS Cost Explorer call failed: %s", exc)
            return []

        cost_metrics: list[CloudMetric] = []

        for result in response.get("ResultsByTime", []):
            period_start = result["TimePeriod"]["Start"]
            for group in result.get("Groups", []):
                keys = group["Keys"]
                service = keys[0] if len(keys) > 0 else "unknown"
                usage_type = keys[1] if len(keys) > 1 else "unknown"
                amount = float(
                    group["Metrics"]["UnblendedCost"]["Amount"]
                )

                if amount > 0:
                    cost_metrics.append(CloudMetric(
                        timestamp=datetime.fromisoformat(period_start).replace(
                            tzinfo=timezone.utc
                        ),
                        node_name=f"{service}/{usage_type}",
                        cluster=self._account_alias,
                        metric_name="daily_cost_usd",
                        value=amount,
                        unit="usd",
                        source="aws",
                        cost_usd=amount,
                    ))

        logger.info(
            "AWS: collected %d cost line items for %s",
            len(cost_metrics), yesterday.isoformat(),
        )
        return cost_metrics

    # ── Metric normalization ────────────────────────────────────────

    def _normalize_metric(
        self, raw_metric: dict[str, Any], instance: dict[str, Any]
    ) -> CloudMetric:
        """
        Convert a CloudWatch datapoint + instance metadata into a
        CloudMetric aligned with the NØMAÐ schema.
        """
        cw_name = raw_metric["metric_name"]
        canonical_name, unit = AWS_METRIC_MAP.get(
            cw_name, (self._canonicalize(cw_name), "unknown")
        )

        ts = raw_metric["timestamp"]
        if not isinstance(ts, datetime):
            ts = datetime.fromisoformat(str(ts))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        return CloudMetric(
            timestamp=ts,
            node_name=instance.get("name", instance["instance_id"]),
            cluster=self._account_alias,
            metric_name=canonical_name,
            value=raw_metric["value"],
            unit=unit,
            source="aws",
            instance_type=instance.get("instance_type"),
            availability_zone=instance.get("availability_zone"),
            tags=instance.get("tags", {}),
        )
