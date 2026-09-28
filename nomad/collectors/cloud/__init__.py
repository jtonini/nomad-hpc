# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Cloud Collector Modules

Ingest metrics from cloud provider APIs and normalize them into the
same schema that on-prem collectors use. Everything downstream —
analysis engines, TESSERA, alerting, dashboards — works unchanged.

Modules:
    cloud_base  - Abstract base for all cloud collectors
    aws         - AWS CloudWatch / Cost Explorer collector
    azure       - (planned) Azure Monitor collector
    gcp         - (planned) GCP Cloud Monitoring collector
"""

from .cloud_base import CloudBaseCollector, CloudAuthError, CloudMetric

__all__ = [
    "CloudBaseCollector",
    "CloudAuthError",
    "CloudMetric",
]

# Conditional imports — only register collectors whose SDKs are installed
try:
    from .aws import AWSCollector  # noqa: F401
    __all__.append("AWSCollector")
except ImportError:
    pass
