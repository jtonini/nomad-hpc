# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Derivative trend projection for the NØMAD energy module.

Aggregate extrapolation only -- deliberately NOT per-entity prediction
(that is `nomad energy predict`, via TESSERA). This bins a metric into
equal time buckets across the window, fits an ordinary least-squares line,
and projects it to a horizon. No heavy dependencies.

Two series are projected side by side:
    consumed     total energy drawn (capacity-planning view)
    recoverable  energy waste (is the inefficiency trending down?)

The reported growth rate is the slope expressed as a percentage of the
window-mean per projection period, so "consumption +12%/quarter" is read
directly. A flat or declining recoverable series is the signal that
interventions are working at the aggregate level.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


# Horizon -> (label, days) for projection.
HORIZONS = {
    "30d": ("30 days", 30),
    "quarter": ("quarter", 91),
    "semester": ("semester", 120),
    "year": ("year", 365),
}


@dataclass
class TrendProjection:
    """A single metric's fitted trend and projection."""
    name: str                      # "consumed" | "recoverable"
    unit: str                      # "kWh"
    bucket_values: list[float] = field(default_factory=list)  # kWh per bucket
    bucket_days: float = 1.0       # width of each bucket, days
    slope_per_day: float = 0.0     # kWh/day
    intercept: float = 0.0         # kWh at window start (fit)
    window_mean: float = 0.0       # mean bucket value, kWh
    current_rate_per_day: float = 0.0  # fitted value at window end / bucket_days
    r_squared: float = 1.0         # goodness of linear fit (0..1); low = trend is noisy

    def project_total(self, horizon_days: float) -> float:
        """Projected cumulative kWh over the horizon, from the fitted line."""
        # Integrate the line over [end, end + horizon] in per-day terms.
        end_day = len(self.bucket_values) * self.bucket_days
        daily_at = lambda d: (self.intercept + self.slope_per_day * d) / self.bucket_days
        # trapezoidal over the horizon
        start_rate = max(0.0, daily_at(end_day))
        end_rate = max(0.0, daily_at(end_day + horizon_days))
        return (start_rate + end_rate) / 2.0 * horizon_days

    def growth_pct(self, horizon_days: float) -> float:
        """Slope as % of window mean over the horizon period."""
        if self.window_mean <= 0:
            return 0.0
        change = self.slope_per_day * horizon_days
        return change / self.window_mean * 100.0

    def fit_quality(self) -> str:
        """Plain-language reliability of the linear fit."""
        if self.r_squared >= 0.75:
            return "strong"
        if self.r_squared >= 0.4:
            return "moderate"
        return "weak"


def _fit_line(values: list[float]) -> tuple[float, float]:
    """Ordinary least-squares slope and intercept over bucket index 0..n-1.

    Returns (slope_per_bucket, intercept). Pure Python, no numpy.
    """
    n = len(values)
    if n < 2:
        return 0.0, (values[0] if values else 0.0)
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0, my
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, values)) / denom
    intercept = my - slope * mx
    return slope, intercept


def build_trend(name: str, bucket_values: list[float], bucket_days: float) -> TrendProjection:
    """Fit a TrendProjection from bucketed kWh values."""
    slope_bucket, intercept = _fit_line(bucket_values)
    n = len(bucket_values)
    mean = sum(bucket_values) / n if n else 0.0
    # coefficient of determination: 1 - SS_res / SS_tot
    ss_tot = sum((y - mean) ** 2 for y in bucket_values)
    ss_res = sum((y - (intercept + slope_bucket * i)) ** 2
                 for i, y in enumerate(bucket_values))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    tp = TrendProjection(
        name=name, unit="kWh",
        bucket_values=bucket_values, bucket_days=bucket_days,
        slope_per_day=slope_bucket / bucket_days if bucket_days else 0.0,
        intercept=intercept, window_mean=mean,
        r_squared=max(0.0, min(1.0, r2)),
    )
    # current daily rate = fitted value in the last bucket, per day
    fitted_last = intercept + slope_bucket * (n - 1) if n else 0.0
    tp.current_rate_per_day = max(0.0, fitted_last / bucket_days) if bucket_days else 0.0
    return tp


def bucket_windows(start: datetime, end: datetime, n_buckets: int) -> list[tuple]:
    """Split [start, end] into n_buckets equal (start, end) windows."""
    total = (end - start).total_seconds()
    if total <= 0 or n_buckets < 1:
        return [(start, end)]
    step = total / n_buckets
    out = []
    for i in range(n_buckets):
        b0 = start + timedelta(seconds=step * i)
        b1 = start + timedelta(seconds=step * (i + 1))
        out.append((b0, b1))
    return out
