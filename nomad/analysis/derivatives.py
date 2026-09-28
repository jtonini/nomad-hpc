# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ Derivative Analysis

Analyzes time series using first and second derivatives to detect:
- Trends (increasing, decreasing, stable)
- Acceleration (exponential growth detection)
- Sudden changes (spikes in second derivative)

Key insight: Second derivative catches exponential growth BEFORE 
linear projections would predict a problem.
"""

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class Trend(Enum):
    """Classification of time series trend."""

    UNKNOWN = "unknown"
    STABLE = "stable"
    INCREASING_LINEAR = "increasing_linear"
    DECREASING_LINEAR = "decreasing_linear"
    ACCELERATING_GROWTH = "accelerating_growth"      # d1 > 0, d2 > 0 - DANGER!
    DECELERATING_GROWTH = "decelerating_growth"      # d1 > 0, d2 < 0 - slowing down
    ACCELERATING_DECLINE = "accelerating_decline"    # d1 < 0, d2 < 0
    DECELERATING_DECLINE = "decelerating_decline"    # d1 < 0, d2 > 0 - leveling off


class AlertLevel(Enum):
    """Alert level based on derivative analysis."""

    NONE = "none"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class DerivativeAnalysis:
    """Result of derivative analysis on a time series."""

    current_value: float
    first_derivative: float | None      # Rate of change (units/day)
    second_derivative: float | None     # Acceleration (units/day²)
    trend: Trend
    alert_level: AlertLevel

    # Projections
    projected_value_1d: float | None    # Value in 1 day
    projected_value_7d: float | None    # Value in 7 days
    days_until_limit: float | None      # Days until limit reached (if set)

    # Raw data
    n_points: int
    time_span_hours: float

    def to_dict(self) -> dict[str, Any]:
        return {
            'current_value': self.current_value,
            'first_derivative': self.first_derivative,
            'second_derivative': self.second_derivative,
            'trend': self.trend.value,
            'alert_level': self.alert_level.value,
            'projected_value_1d': self.projected_value_1d,
            'projected_value_7d': self.projected_value_7d,
            'days_until_limit': self.days_until_limit,
            'n_points': self.n_points,
            'time_span_hours': self.time_span_hours,
        }

    def __repr__(self) -> str:
        d1_str = f"{self.first_derivative:+.2f}/day" if self.first_derivative else "N/A"
        d2_str = f"{self.second_derivative:+.3f}/day²" if self.second_derivative else "N/A"
        return (
            f"DerivativeAnalysis(value={self.current_value:.2f}, "
            f"d1={d1_str}, d2={d2_str}, "
            f"trend={self.trend.value}, alert={self.alert_level.value})"
        )


class DerivativeAnalyzer:
    """
    Analyzes time series using first and second derivatives.
    
    The analyzer maintains a sliding window of data points and computes:
    - First derivative: Rate of change
    - Second derivative: Acceleration (rate of change of rate of change)
    
    This enables detection of:
    - Exponential growth (d2 > 0 when d1 > 0)
    - Sudden changes (spikes in d2)
    - Trend changes (d2 changes sign)
    
    Example:
        analyzer = DerivativeAnalyzer(
            window_size=10,
            smoothing='exponential',
            alpha=0.3,
        )
        
        # Add historical data points
        for timestamp, value in history:
            analyzer.add_point(timestamp, value)
        
        # Get analysis
        result = analyzer.analyze(limit=1000)  # Alert if approaching 1000
        print(f"Trend: {result.trend}")
        print(f"Days until limit: {result.days_until_limit}")
    """

    def __init__(
        self,
        window_size: int = 10,
        smoothing: str = 'exponential',
        alpha: float = 0.3,
        min_points: int = 3,
    ):
        """
        Initialize the analyzer.
        
        Args:
            window_size: Number of points to keep in history
            smoothing: 'none', 'moving_average', or 'exponential'
            alpha: Smoothing factor for exponential (0-1, higher = less smooth)
            min_points: Minimum points required for derivative calculation
        """
        self.window_size = window_size
        self.smoothing = smoothing
        self.alpha = alpha
        self.min_points = min_points

        self.history: deque[dict] = deque(maxlen=window_size)
        self.smoothed_history: deque[dict] = deque(maxlen=window_size)

    def clear(self) -> None:
        """Clear all history."""
        self.history.clear()
        self.smoothed_history.clear()

    def add_point(self, timestamp: datetime, value: float) -> None:
        """
        Add a new data point.
        
        Args:
            timestamp: When the measurement was taken
            value: The measured value
        """
        self.history.append({'t': timestamp, 'v': value})

        # Apply smoothing
        if self.smoothing == 'exponential' and len(self.smoothed_history) > 0:
            smoothed = self.alpha * value + (1 - self.alpha) * self.smoothed_history[-1]['v']
        elif self.smoothing == 'moving_average' and len(self.history) >= 3:
            recent = list(self.history)[-3:]
            smoothed = sum(p['v'] for p in recent) / len(recent)
        else:
            smoothed = value

        self.smoothed_history.append({'t': timestamp, 'v': smoothed})

    def add_points(self, points: list[tuple[datetime, float]]) -> None:
        """Add multiple points at once."""
        for timestamp, value in points:
            self.add_point(timestamp, value)

    def first_derivative(self) -> float | None:
        """
        Compute first derivative (rate of change).
        
        Returns:
            Rate of change in units per second, or None if insufficient data.
        """
        if len(self.smoothed_history) < 2:
            return None

        p1 = self.smoothed_history[-2]
        p2 = self.smoothed_history[-1]

        dt = (p2['t'] - p1['t']).total_seconds()
        if dt == 0:
            return None

        dv = p2['v'] - p1['v']
        return dv / dt

    def second_derivative(self) -> float | None:
        """
        Compute second derivative (acceleration).
        
        Returns:
            Acceleration in units per second², or None if insufficient data.
            
            Positive: Rate is increasing (accelerating growth or decelerating decline)
            Negative: Rate is decreasing (decelerating growth or accelerating decline)
        """
        if len(self.smoothed_history) < 3:
            return None

        p1 = self.smoothed_history[-3]
        p2 = self.smoothed_history[-2]
        p3 = self.smoothed_history[-1]

        dt1 = (p2['t'] - p1['t']).total_seconds()
        dt2 = (p3['t'] - p2['t']).total_seconds()

        if dt1 == 0 or dt2 == 0:
            return None

        # First derivatives at two points
        d1 = (p2['v'] - p1['v']) / dt1
        d2 = (p3['v'] - p2['v']) / dt2

        # Second derivative (rate of change of first derivative)
        avg_dt = (dt1 + dt2) / 2
        return (d2 - d1) / avg_dt

    def analyze(
        self,
        limit: float | None = None,
        acceleration_warning_threshold: float = 0.0,
        acceleration_critical_threshold: float = 0.0,
    ) -> DerivativeAnalysis:
        """
        Perform full derivative analysis.
        
        Args:
            limit: Optional upper limit (e.g., disk capacity) for projection
            acceleration_warning_threshold: d2 threshold for warning (units/day²)
            acceleration_critical_threshold: d2 threshold for critical (units/day²)
        
        Returns:
            DerivativeAnalysis with trend, projections, and alert level.
        """
        if len(self.smoothed_history) < self.min_points:
            return DerivativeAnalysis(
                current_value=self.smoothed_history[-1]['v'] if self.smoothed_history else 0,
                first_derivative=None,
                second_derivative=None,
                trend=Trend.UNKNOWN,
                alert_level=AlertLevel.NONE,
                projected_value_1d=None,
                projected_value_7d=None,
                days_until_limit=None,
                n_points=len(self.smoothed_history),
                time_span_hours=0,
            )

        current = self.history[-1]['v'] if self.history else self.smoothed_history[-1]['v']
        d1 = self.first_derivative()
        d2 = self.second_derivative()

        # Convert to per-day for human readability
        d1_per_day = d1 * 86400 if d1 is not None else None
        d2_per_day = d2 * 86400 * 86400 if d2 is not None else None

        # Calculate time span
        time_span = (
            self.smoothed_history[-1]['t'] - self.smoothed_history[0]['t']
        ).total_seconds() / 3600

        # Classify trend
        trend = self._classify_trend(d1, d2)

        # Compute projections
        proj_1d = self._project(days=1, d1=d1, d2=d2)
        proj_7d = self._project(days=7, d1=d1, d2=d2)

        # Days until limit
        days_until = None
        if limit is not None and d1 is not None and d1 > 0:
            days_until = self._days_until_limit(limit, d1, d2)

        # Determine alert level
        alert_level = self._compute_alert_level(
            d1, d2, d1_per_day, d2_per_day,
            acceleration_warning_threshold,
            acceleration_critical_threshold,
        )

        return DerivativeAnalysis(
            current_value=current,
            first_derivative=d1_per_day,
            second_derivative=d2_per_day,
            trend=trend,
            alert_level=alert_level,
            projected_value_1d=proj_1d,
            projected_value_7d=proj_7d,
            days_until_limit=days_until,
            n_points=len(self.smoothed_history),
            time_span_hours=time_span,
        )

    def _classify_trend(self, d1: float | None, d2: float | None) -> Trend:
        """Classify the trend based on derivatives."""
        if d1 is None:
            return Trend.UNKNOWN

        # Threshold for "essentially zero" (relative to typical values)
        d1_threshold = 1e-10
        d2_threshold = 1e-15

        if abs(d1) < d1_threshold:
            return Trend.STABLE

        if d2 is None or abs(d2) < d2_threshold:
            # Linear trend
            return Trend.INCREASING_LINEAR if d1 > 0 else Trend.DECREASING_LINEAR

        # Non-linear trends
        if d1 > 0:
            if d2 > 0:
                return Trend.ACCELERATING_GROWTH  # DANGER!
            else:
                return Trend.DECELERATING_GROWTH
        else:
            if d2 < 0:
                return Trend.ACCELERATING_DECLINE
            else:
                return Trend.DECELERATING_DECLINE

    def _project(
        self,
        days: float,
        d1: float | None,
        d2: float | None,
    ) -> float | None:
        """
        Project future value using quadratic model.
        
        Uses: v(t) = v₀ + d1·t + ½·d2·t²
        
        More accurate than linear when d2 ≠ 0.
        """
        if len(self.smoothed_history) == 0 or d1 is None:
            return None

        current = self.smoothed_history[-1]['v']
        t = days * 86400  # Convert to seconds

        if d2 is None or abs(d2) < 1e-15:
            # Linear projection
            return current + d1 * t
        else:
            # Quadratic projection
            return current + d1 * t + 0.5 * d2 * t * t

    def _days_until_limit(
        self,
        limit: float,
        d1: float,
        d2: float | None,
    ) -> float | None:
        """
        Calculate days until limit is reached.
        
        For linear growth: t = (limit - current) / d1
        For quadratic: solve v₀ + d1·t + ½·d2·t² = limit
        """
        if len(self.smoothed_history) == 0:
            return None

        current = self.smoothed_history[-1]['v']

        if current >= limit:
            return 0.0

        if d1 <= 0:
            return None  # Not approaching limit

        remaining = limit - current

        if d2 is None or abs(d2) < 1e-15:
            # Linear: t = remaining / d1
            t_seconds = remaining / d1
        else:
            # Quadratic: solve ½d2·t² + d1·t - remaining = 0
            # Using quadratic formula: t = (-d1 ± √(d1² + 2·d2·remaining)) / d2
            discriminant = d1 * d1 + 2 * d2 * remaining

            if discriminant < 0:
                # No real solution (decelerating and won't reach limit)
                return None

            if d2 > 0:
                # Accelerating - take the smaller positive root
                t_seconds = (-d1 + discriminant ** 0.5) / d2
            else:
                # Decelerating - take the larger root
                t_seconds = (-d1 - discriminant ** 0.5) / d2

            if t_seconds < 0:
                return None

        return t_seconds / 86400  # Convert to days

    def _compute_alert_level(
        self,
        d1: float | None,
        d2: float | None,
        d1_per_day: float | None,
        d2_per_day: float | None,
        warning_threshold: float,
        critical_threshold: float,
    ) -> AlertLevel:
        """
        Determine alert level based on derivatives.
        
        Key insight: Accelerating growth (d2 > 0 when d1 > 0)
        is more dangerous than linear growth!
        """
        if d1 is None or d2 is None:
            return AlertLevel.NONE

        # Accelerating growth is the most dangerous
        if d1 > 0 and d2 > 0:
            if d2_per_day and critical_threshold > 0 and d2_per_day > critical_threshold:
                return AlertLevel.CRITICAL
            if d2_per_day and warning_threshold > 0 and d2_per_day > warning_threshold:
                return AlertLevel.WARNING

            # Even without thresholds, accelerating growth is concerning
            # if acceleration is significant relative to rate
            if d1_per_day and d2_per_day:
                accel_ratio = abs(d2_per_day) / (abs(d1_per_day) + 0.001)
                if accel_ratio > 0.1:  # Acceleration > 10% of rate per day
                    return AlertLevel.CRITICAL
                elif accel_ratio > 0.05:
                    return AlertLevel.WARNING

        return AlertLevel.NONE


def analyze_disk_trend(
    history: list[dict[str, Any]],
    limit_bytes: int | None = None,
    smoothing: str = 'exponential',
    alpha: float = 0.3,
) -> DerivativeAnalysis:
    """
    Convenience function to analyze disk usage trend.
    
    Args:
        history: List of dicts with 'timestamp' and 'used_bytes' keys
        limit_bytes: Total disk capacity (for days-until-full projection)
        smoothing: Smoothing method
        alpha: Smoothing factor
    
    Returns:
        DerivativeAnalysis with trend and projections.
    
    Example:
        history = disk_collector.get_history('/home', hours=48)
        analysis = analyze_disk_trend(history, limit_bytes=1_000_000_000_000)
        
        if analysis.alert_level == AlertLevel.CRITICAL:
            print(f"CRITICAL: Disk will be full in {analysis.days_until_limit:.1f} days!")
    """
    analyzer = DerivativeAnalyzer(
        window_size=max(10, len(history)),
        smoothing=smoothing,
        alpha=alpha,
    )

    for record in history:
        timestamp = record.get('timestamp')
        value = record.get('used_bytes')

        if timestamp is None or value is None:
            continue

        # Parse timestamp if string
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)

        analyzer.add_point(timestamp, value)

    # Convert limit to GB for human-readable derivatives
    # (actually keep in bytes for accurate projection)
    return analyzer.analyze(
        limit=limit_bytes,
        acceleration_warning_threshold=1e9,   # 1 GB/day² acceleration
        acceleration_critical_threshold=5e9,  # 5 GB/day² acceleration
    )


def analyze_queue_trend(
    history: list[dict[str, Any]],
    smoothing: str = 'moving_average',
) -> DerivativeAnalysis:
    """
    Convenience function to analyze queue depth trend.
    
    Args:
        history: List of dicts with 'timestamp' and 'pending_jobs' keys
        smoothing: Smoothing method
    
    Returns:
        DerivativeAnalysis for queue depth.
    """
    analyzer = DerivativeAnalyzer(
        window_size=max(10, len(history)),
        smoothing=smoothing,
        alpha=0.4,
    )

    for record in history:
        timestamp = record.get('timestamp')
        value = record.get('pending_jobs')

        if timestamp is None or value is None:
            continue

        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)

        analyzer.add_point(timestamp, value)

    return analyzer.analyze(
        acceleration_warning_threshold=5,   # 5 jobs/hour² acceleration
        acceleration_critical_threshold=20, # 20 jobs/hour² acceleration
    )
