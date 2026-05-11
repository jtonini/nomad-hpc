import pytest


# --- Idea 18 Component 1 fixtures ------------------------------------------

def _bootstrap_db_with_migrations(db_path: str) -> None:
    """Apply all NOMAD migrations to a fresh DB. Used by per_user tests."""
    import sqlite3
    from nomad.db.migrations import MIGRATIONS
    with sqlite3.connect(db_path) as conn:
        for _version, _description, sql in MIGRATIONS:
            try:
                conn.executescript(sql)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column" in msg or "already exists" in msg:
                    continue
                raise


@pytest.fixture
def db_path(tmp_path) -> str:
    """Fresh SQLite DB with the full NOMAD schema applied."""
    p = str(tmp_path / "test.db")
    _bootstrap_db_with_migrations(p)
    return p


@pytest.fixture
def db_with_alerts(tmp_path) -> str:
    """DB with per_user_alert rows mirroring the validation findings."""
    import sqlite3
    from datetime import datetime
    p = str(tmp_path / "test_alerts.db")
    _bootstrap_db_with_migrations(p)

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        (now, "spydur", "headnode", "ia3nk", 10001, 5001,
         "sid_ia3nk", "cpu_10pct_5min", "cpu", "actionable", 10.0, "percent",
         9660, "gmx_mpi", "gmx_mpi -f production.tpr",
         '["sshd","bash","singularity","bash","gmx"]',
         95.2, 2_500_000_000, "spydur|sid_ia3nk|cpu_10pct_5min", 1, now,
         "head_node_cpu_sustained"),
        (now, "spydur", "headnode", "perickso", 10002, 5002,
         "sid_peri", "memory_16gb_2min", "memory", "actionable", 16.0, "gb",
         6540, "R", "R --no-save",
         '["sshd","bash"]',
         670.0, 75 * 1024**3, "spydur|sid_peri|memory_16gb_2min", 1, now,
         "head_node_memory_high"),
        (now, "arachne", "headnode", "abezerra", 10003, 5003,
         "sid_abe", "memory_4gb_10min", "memory", "informational", 4.0, "gb",
         23040, "language_server", "node language_server_linux_x64",
         '["sshd","bash","sh","node","node"]',
         34.0, 2_400_000_000, "arachne|sid_abe|memory_4gb_10min", 4, now,
         "head_node_memory_moderate"),
    ]
    with sqlite3.connect(p) as conn:
        conn.executemany(
            """
            INSERT INTO per_user_alert (
                fired_at, hostname, role, username, uid, pid,
                process_session_id, rule_id, rule_type, severity,
                threshold_value, threshold_unit, sustained_for_seconds,
                command, cmdline, ancestry_chain, peak_cpu_percent,
                peak_memory_bytes, dedup_key, occurrences, last_seen,
                edu_template_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return p
