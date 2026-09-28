"""
Tests for NØMAÐ disk collector and derivative analysis.

Run with: pytest tests/test_disk_and_derivatives.py -v
"""

import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nomad.analysis import (
    AlertLevel,
    DerivativeAnalyzer,
    Trend,
    analyze_disk_trend,
)
from nomad.collectors import DiskCollector, registry


# ============================================
# FIXTURES
# ============================================

@pytest.fixture
def temp_db():
    """Create a temporary database with schema."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    
    # Load schema
    schema_path = Path(__file__).parent.parent / 'nomad' / 'db' / 'schema.sql'
    
    conn = sqlite3.connect(db_path)
    with open(schema_path) as f:
        conn.executescript(f.read())
    conn.close()
    
    yield db_path
    
    # Cleanup
    db_path.unlink(missing_ok=True)


@pytest.fixture
def disk_collector(temp_db):
    """Create a DiskCollector instance."""
    config = {
        'filesystems': ['/tmp'],  # Use /tmp for testing
        'quota_enabled': False,
        'use_shutil': True,  # Use Python shutil (more portable)
    }
    return DiskCollector(config, temp_db)


# ============================================
# DISK COLLECTOR TESTS
# ============================================

class TestDiskCollector:
    """Tests for DiskCollector."""
    
    def test_collector_registered(self):
        """Test that DiskCollector is registered."""
        assert 'disk' in registry.list_collectors()
        assert registry.get('disk') is DiskCollector
    
    def test_collect_filesystem(self, disk_collector):
        """Test collecting filesystem data."""
        result = disk_collector.run()
        
        assert result.success
        assert result.records_collected > 0
        assert len(result.data) > 0
        
        # Check data structure
        record = result.data[0]
        assert record['type'] == 'filesystem'
        assert record['path'] == '/tmp'
        assert 'total_bytes' in record
        assert 'used_bytes' in record
        assert 'used_percent' in record
    
    def test_collect_stores_data(self, disk_collector, temp_db):
        """Test that collected data is stored in database."""
        disk_collector.run()
        
        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        
        rows = conn.execute("SELECT * FROM filesystems").fetchall()
        assert len(rows) > 0
        
        row = dict(rows[0])
        assert row['path'] == '/tmp'
        assert row['total_bytes'] > 0
        
        conn.close()
    
    def test_get_latest(self, disk_collector):
        """Test retrieving latest data."""
        disk_collector.run()
        
        latest = disk_collector.get_latest('/tmp')
        assert latest is not None
        assert latest['path'] == '/tmp'
    
    def test_nonexistent_path(self, temp_db):
        """Test handling of nonexistent path."""
        config = {
            'filesystems': ['/nonexistent/path/xyz'],
            'use_shutil': True,
        }
        collector = DiskCollector(config, temp_db)
        
        # Should not raise, but should have no data
        result = collector.run()
        # May fail or succeed with 0 records depending on implementation
        # The important thing is it doesn't crash


# ============================================
# DERIVATIVE ANALYSIS TESTS
# ============================================

class TestDerivativeAnalyzer:
    """Tests for DerivativeAnalyzer."""
    
    def test_insufficient_data(self):
        """Test with insufficient data points."""
        analyzer = DerivativeAnalyzer(min_points=3)
        
        now = datetime.now()
        analyzer.add_point(now, 100)
        analyzer.add_point(now + timedelta(hours=1), 110)
        
        result = analyzer.analyze()
        assert result.trend == Trend.UNKNOWN
        assert result.first_derivative is None
    
    def test_stable_trend(self):
        """Test detection of stable (flat) trend."""
        analyzer = DerivativeAnalyzer()
        
        now = datetime.now()
        for i in range(5):
            analyzer.add_point(now + timedelta(hours=i), 100)  # Constant value
        
        result = analyzer.analyze()
        assert result.trend == Trend.STABLE
        assert result.alert_level == AlertLevel.NONE
    
    def test_linear_increasing(self):
        """Test detection of linear increase."""
        analyzer = DerivativeAnalyzer(smoothing='none')
        
        now = datetime.now()
        for i in range(5):
            analyzer.add_point(now + timedelta(hours=i), 100 + i * 10)
        
        result = analyzer.analyze()
        assert result.trend == Trend.INCREASING_LINEAR
        assert result.first_derivative is not None
        assert result.first_derivative > 0
    
    def test_linear_decreasing(self):
        """Test detection of linear decrease."""
        analyzer = DerivativeAnalyzer(smoothing='none')
        
        now = datetime.now()
        for i in range(5):
            analyzer.add_point(now + timedelta(hours=i), 100 - i * 10)
        
        result = analyzer.analyze()
        assert result.trend == Trend.DECREASING_LINEAR
        assert result.first_derivative is not None
        assert result.first_derivative < 0
    
    def test_accelerating_growth(self):
        """Test detection of accelerating growth (exponential-like)."""
        analyzer = DerivativeAnalyzer(smoothing='none')
        
        now = datetime.now()
        # Quadratic growth: 100, 110, 140, 190, 260 (differences: 10, 30, 50, 70)
        values = [100, 110, 140, 190, 260]
        for i, v in enumerate(values):
            analyzer.add_point(now + timedelta(hours=i), v)
        
        result = analyzer.analyze()
        assert result.trend == Trend.ACCELERATING_GROWTH
        assert result.second_derivative is not None
        assert result.second_derivative > 0
    
    def test_decelerating_growth(self):
        """Test detection of decelerating growth."""
        analyzer = DerivativeAnalyzer(smoothing='none')
        
        now = datetime.now()
        # Slowing growth: 100, 150, 180, 200, 210 (differences: 50, 30, 20, 10)
        values = [100, 150, 180, 200, 210]
        for i, v in enumerate(values):
            analyzer.add_point(now + timedelta(hours=i), v)
        
        result = analyzer.analyze()
        assert result.trend == Trend.DECELERATING_GROWTH
        assert result.first_derivative > 0
        assert result.second_derivative < 0
    
    def test_projection_linear(self):
        """Test linear projection."""
        analyzer = DerivativeAnalyzer(smoothing='none')
        
        now = datetime.now()
        # 10 units per hour increase
        for i in range(5):
            analyzer.add_point(now + timedelta(hours=i), 100 + i * 10)
        
        result = analyzer.analyze()
        
        # Should project ~340 in 1 day (100 + 4*10 + 24*10 = 380... approximately)
        assert result.projected_value_1d is not None
        assert result.projected_value_1d > result.current_value
    
    def test_days_until_limit(self):
        """Test days-until-limit calculation."""
        analyzer = DerivativeAnalyzer(smoothing='none')
        
        now = datetime.now()
        # 100 units per day increase, starting at 500, limit 1000
        for i in range(5):
            analyzer.add_point(now + timedelta(days=i), 500 + i * 100)
        
        result = analyzer.analyze(limit=1000)
        
        assert result.days_until_limit is not None
        # At 900 now, growing 100/day, should hit 1000 in ~1 day
        assert 0 < result.days_until_limit < 2
    
    def test_alert_on_acceleration(self):
        """Test alert level for accelerating growth."""
        analyzer = DerivativeAnalyzer(smoothing='none')
        
        now = datetime.now()
        # Strong acceleration
        values = [100, 120, 180, 300, 500]
        for i, v in enumerate(values):
            analyzer.add_point(now + timedelta(hours=i), v)
        
        result = analyzer.analyze(
            acceleration_warning_threshold=10,
            acceleration_critical_threshold=100,
        )
        
        # Should trigger at least warning due to acceleration
        assert result.alert_level in [AlertLevel.WARNING, AlertLevel.CRITICAL]


class TestAnalyzeDiskTrend:
    """Tests for the analyze_disk_trend convenience function."""
    
    def test_analyze_disk_history(self):
        """Test analyzing disk history."""
        now = datetime.now()
        
        history = [
            {'timestamp': (now - timedelta(hours=4)).isoformat(), 'used_bytes': 100_000_000_000},
            {'timestamp': (now - timedelta(hours=3)).isoformat(), 'used_bytes': 110_000_000_000},
            {'timestamp': (now - timedelta(hours=2)).isoformat(), 'used_bytes': 120_000_000_000},
            {'timestamp': (now - timedelta(hours=1)).isoformat(), 'used_bytes': 130_000_000_000},
            {'timestamp': now.isoformat(), 'used_bytes': 140_000_000_000},
        ]
        
        result = analyze_disk_trend(history, limit_bytes=200_000_000_000)
        
        assert result.current_value == 140_000_000_000
        assert result.first_derivative is not None
        assert result.first_derivative > 0  # Growing
        assert result.trend in [Trend.INCREASING_LINEAR, Trend.ACCELERATING_GROWTH, Trend.DECELERATING_GROWTH]


# ============================================
# INTEGRATION TESTS
# ============================================

class TestIntegration:
    """Integration tests combining collector and analysis."""
    
    def test_collect_and_analyze(self, disk_collector, temp_db):
        """Test collecting data and analyzing trends."""
        # Collect multiple times with simulated time gaps
        # (In real usage, this would happen over hours/days)
        
        for _ in range(3):
            disk_collector.run()
        
        # Get history
        history = disk_collector.get_history('/tmp', hours=24)
        
        # Should have data
        assert len(history) > 0
        
        # Analyze (won't show real trend since data is collected instantly)
        # but should not crash
        if len(history) >= 3:
            result = analyze_disk_trend(history)
            assert result is not None


# ============================================
# RUN TESTS
# ============================================

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
