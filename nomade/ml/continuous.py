"""
Continuous Learning Pipeline for NØMADE.

Monitors database for new jobs and triggers retraining when needed.

Strategies:
1. Time-based: Retrain every N hours
2. Count-based: Retrain after N new jobs
3. Drift-based: Retrain when predictions degrade

Usage:
    # CLI
    nomade learn --strategy count --threshold 100
    
    # Cron (every 6 hours)
    0 */6 * * * nomade learn --strategy time
    
    # Daemon
    nomade learn --daemon --interval 3600
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ContinuousLearner:
    """Manages continuous model retraining."""
    
    def __init__(self, db_path: str, config: dict = None):
        self.db_path = Path(db_path)
        self.config = config or {}
        
        # Learning configuration
        learn_config = self.config.get('learning', {})
        self.strategy = learn_config.get('strategy', 'count')  # time, count, drift
        self.time_interval_hours = learn_config.get('interval_hours', 6)
        self.job_threshold = learn_config.get('job_threshold', 100)
        self.min_jobs = learn_config.get('min_jobs', 50)
        self.epochs = learn_config.get('epochs', 100)
        
        # Initialize tracking table
        self._init_tracking_table()
    
    def _init_tracking_table(self):
        """Create table to track learning runs."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ml_training_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                strategy TEXT,
                trigger_reason TEXT,
                jobs_trained INTEGER,
                jobs_since_last INTEGER,
                prediction_id INTEGER,
                gnn_accuracy REAL,
                lstm_accuracy REAL,
                ae_f1 REAL,
                status TEXT,
                error_message TEXT
            )
        ''')
        conn.commit()
        conn.close()
    
    def get_training_status(self) -> dict:
        """Get current training status and statistics."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        
        # Last training run
        last_run = conn.execute('''
            SELECT * FROM ml_training_runs 
            WHERE status = 'completed'
            ORDER BY completed_at DESC LIMIT 1
        ''').fetchone()
        
        # Total jobs
        total_jobs = conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        
        # Jobs since last training
        jobs_since = total_jobs
        last_trained_at = None
        if last_run:
            last_trained_at = last_run['completed_at']
            jobs_since = total_jobs - (last_run['jobs_trained'] or 0)
        
        # Latest prediction
        latest_pred = conn.execute('''
            SELECT * FROM ml_predictions 
            ORDER BY created_at DESC LIMIT 1
        ''').fetchone()
        
        conn.close()
        
        return {
            'total_jobs': total_jobs,
            'jobs_since_last_training': jobs_since,
            'last_trained_at': last_trained_at,
            'last_run': dict(last_run) if last_run else None,
            'latest_prediction_id': latest_pred['id'] if latest_pred else None,
            'strategy': self.strategy,
            'thresholds': {
                'time_hours': self.time_interval_hours,
                'job_count': self.job_threshold
            }
        }
    
    def should_retrain(self) -> tuple[bool, str]:
        """
        Check if retraining is needed based on strategy.
        
        Returns:
            (should_retrain, reason)
        """
        status = self.get_training_status()
        
        # Always train if never trained
        if status['last_trained_at'] is None:
            if status['total_jobs'] >= self.min_jobs:
                return True, f"Initial training ({status['total_jobs']} jobs available)"
            else:
                return False, f"Not enough jobs ({status['total_jobs']} < {self.min_jobs})"
        
        # Strategy: time-based
        if self.strategy == 'time':
            last_time = datetime.fromisoformat(status['last_trained_at'])
            hours_since = (datetime.now() - last_time).total_seconds() / 3600
            if hours_since >= self.time_interval_hours:
                return True, f"Time interval reached ({hours_since:.1f}h >= {self.time_interval_hours}h)"
            return False, f"Time interval not reached ({hours_since:.1f}h < {self.time_interval_hours}h)"
        
        # Strategy: count-based
        elif self.strategy == 'count':
            jobs_since = status['jobs_since_last_training']
            if jobs_since >= self.job_threshold:
                return True, f"Job threshold reached ({jobs_since} >= {self.job_threshold})"
            return False, f"Job threshold not reached ({jobs_since} < {self.job_threshold})"
        
        # Strategy: drift-based (detect degrading predictions)
        elif self.strategy == 'drift':
            return self._check_drift()
        
        return False, f"Unknown strategy: {self.strategy}"
    
    def _check_drift(self) -> tuple[bool, str]:
        """Check if model predictions are drifting (accuracy degrading)."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        
        # Get recent jobs with predictions
        recent_jobs = conn.execute('''
            SELECT j.job_id, j.failure_reason, jp.predicted_class
            FROM jobs j
            LEFT JOIN job_predictions jp ON j.job_id = jp.job_id
            WHERE jp.predicted_class IS NOT NULL
            ORDER BY j.submit_time DESC
            LIMIT 100
        ''').fetchall()
        
        conn.close()
        
        if len(recent_jobs) < 20:
            return False, "Not enough recent predictions to detect drift"
        
        # Calculate recent accuracy
        correct = sum(1 for j in recent_jobs if j['failure_reason'] == j['predicted_class'])
        accuracy = correct / len(recent_jobs)
        
        # Compare to historical accuracy from last training
        status = self.get_training_status()
        if status['last_run']:
            historical_acc = status['last_run'].get('gnn_accuracy', 0)
            if historical_acc > 0 and accuracy < historical_acc * 0.8:  # 20% drop
                return True, f"Accuracy drift detected ({accuracy:.1%} vs {historical_acc:.1%})"
        
        return False, f"No significant drift (accuracy: {accuracy:.1%})"
    
    def train(self, force: bool = False, verbose: bool = True) -> dict:
        """
        Run training if needed (or forced).
        
        Args:
            force: Train regardless of strategy check
            verbose: Show training progress
        
        Returns:
            Training result dict
        """
        from .ensemble import train_and_save_ensemble
        from .persistence import load_predictions_from_db
        
        # Check if training needed
        should_train, reason = self.should_retrain()
        
        if not should_train and not force:
            logger.info(f"Training not needed: {reason}")
            return {
                'status': 'skipped',
                'reason': reason
            }
        
        if force:
            reason = "Forced training"
        
        logger.info(f"Starting training: {reason}")
        
        # Record training start
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.execute('''
            INSERT INTO ml_training_runs (started_at, strategy, trigger_reason, status)
            VALUES (?, ?, ?, ?)
        ''', (datetime.now().isoformat(), self.strategy, reason, 'running'))
        run_id = cursor.lastrowid
        conn.commit()
        
        status = self.get_training_status()
        jobs_since = status['jobs_since_last_training']
        
        try:
            # Run training
            result = train_and_save_ensemble(
                str(self.db_path),
                epochs=self.epochs,
                verbose=verbose
            )
            
            # Update training record
            conn.execute('''
                UPDATE ml_training_runs SET
                    completed_at = ?,
                    jobs_trained = ?,
                    jobs_since_last = ?,
                    prediction_id = ?,
                    gnn_accuracy = ?,
                    lstm_accuracy = ?,
                    ae_f1 = ?,
                    status = ?
                WHERE id = ?
            ''', (
                datetime.now().isoformat(),
                result.get('n_jobs', 0),
                jobs_since,
                result.get('prediction_id'),
                result.get('summary', {}).get('gnn_accuracy'),
                result.get('summary', {}).get('lstm_accuracy'),
                result.get('summary', {}).get('ae_precision'),  # Using precision as proxy
                'completed',
                run_id
            ))
            conn.commit()
            conn.close()
            
            logger.info(f"Training completed: prediction_id={result.get('prediction_id')}")
            
            return {
                **result,
                'status': 'completed',
                'reason': reason,
                'run_id': run_id
            }
            
        except Exception as e:
            # Record failure
            conn.execute('''
                UPDATE ml_training_runs SET
                    completed_at = ?,
                    status = ?,
                    error_message = ?
                WHERE id = ?
            ''', (datetime.now().isoformat(), 'failed', str(e), run_id))
            conn.commit()
            conn.close()
            
            logger.error(f"Training failed: {e}")
            
            return {
                'status': 'failed',
                'reason': reason,
                'error': str(e)
            }
    
    def run_daemon(self, check_interval: int = 300, verbose: bool = False):
        """
        Run as a daemon, checking periodically for retraining needs.
        
        Args:
            check_interval: Seconds between checks (default: 5 minutes)
            verbose: Show training progress
        """
        logger.info(f"Starting continuous learning daemon (strategy: {self.strategy})")
        logger.info(f"Check interval: {check_interval}s")
        
        while True:
            try:
                should_train, reason = self.should_retrain()
                
                if should_train:
                    logger.info(f"Triggering training: {reason}")
                    result = self.train(verbose=verbose)
                    logger.info(f"Training result: {result['status']}")
                else:
                    logger.debug(f"No training needed: {reason}")
                
            except Exception as e:
                logger.error(f"Daemon error: {e}")
            
            time.sleep(check_interval)
    
    def get_training_history(self, limit: int = 10) -> list[dict]:
        """Get recent training runs."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        
        rows = conn.execute('''
            SELECT * FROM ml_training_runs
            ORDER BY started_at DESC
            LIMIT ?
        ''', (limit,)).fetchall()
        
        conn.close()
        
        return [dict(row) for row in rows]
