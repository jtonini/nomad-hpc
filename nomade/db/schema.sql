-- NOMADE Database Schema
-- SQLite 3.35+

-- ============================================
-- INFRASTRUCTURE STATE (Monitoring)
-- ============================================

-- Filesystem usage snapshots
CREATE TABLE IF NOT EXISTS filesystems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    total_bytes INTEGER NOT NULL,
    used_bytes INTEGER NOT NULL,
    available_bytes INTEGER NOT NULL,
    used_percent REAL NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    
    -- Computed fields (updated by analysis)
    fill_rate_bytes_per_day REAL,
    days_until_full REAL,
    first_derivative REAL,  -- bytes/second
    second_derivative REAL  -- bytes/second²
);

CREATE INDEX idx_filesystems_path_ts ON filesystems(path, timestamp);

-- User/group quotas
CREATE TABLE IF NOT EXISTS quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filesystem_path TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('user', 'group')),
    entity_name TEXT NOT NULL,
    limit_bytes INTEGER,
    used_bytes INTEGER NOT NULL,
    used_percent REAL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_quotas_entity_ts ON quotas(entity_name, timestamp);

-- Node status
CREATE TABLE IF NOT EXISTS nodes (
    hostname TEXT PRIMARY KEY,
    partition TEXT,
    status TEXT NOT NULL,  -- UP, DOWN, DRAIN, FAIL, etc.
    drain_reason TEXT,
    cpu_count INTEGER,
    gpu_count INTEGER,
    memory_mb INTEGER,
    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    hardware_errors TEXT  -- JSON
);

-- Node metrics (time-series)
CREATE TABLE IF NOT EXISTS node_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hostname TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    cpu_load_1m REAL,
    cpu_load_5m REAL,
    cpu_load_15m REAL,
    memory_used_mb INTEGER,
    swap_used_mb INTEGER,
    cpu_temp_c REAL,
    gpu_temp_c REAL,
    nfs_latency_ms REAL,
    
    FOREIGN KEY (hostname) REFERENCES nodes(hostname)
);

CREATE INDEX idx_node_metrics_host_ts ON node_metrics(hostname, timestamp);

-- License server status
CREATE TABLE IF NOT EXISTS licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    software TEXT NOT NULL,
    server_host TEXT NOT NULL,
    server_port INTEGER,
    total_licenses INTEGER,
    in_use INTEGER,
    available INTEGER,
    server_status TEXT NOT NULL,  -- UP, DOWN, UNKNOWN
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_date DATE
);

CREATE INDEX idx_licenses_software_ts ON licenses(software, timestamp);

-- SLURM queue state
CREATE TABLE IF NOT EXISTS queue_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partition TEXT NOT NULL,
    pending_jobs INTEGER NOT NULL,
    running_jobs INTEGER NOT NULL,
    total_jobs INTEGER NOT NULL,
    avg_wait_seconds REAL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    
    -- Derivatives
    first_derivative REAL,  -- jobs/second
    second_derivative REAL  -- jobs/second²
);

CREATE INDEX idx_queue_state_partition_ts ON queue_state(partition, timestamp);

-- System I/O statistics (from iostat)
CREATE TABLE IF NOT EXISTS iostat_cpu (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    user_percent REAL,
    system_percent REAL,
    iowait_percent REAL,
    idle_percent REAL
);

CREATE INDEX idx_iostat_cpu_ts ON iostat_cpu(timestamp);

CREATE TABLE IF NOT EXISTS iostat_device (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    device TEXT NOT NULL,
    reads_per_sec REAL,
    read_kb_per_sec REAL,
    read_await_ms REAL,
    writes_per_sec REAL,
    write_kb_per_sec REAL,
    write_await_ms REAL,
    util_percent REAL,
    queue_length REAL
);

CREATE INDEX idx_iostat_device_ts ON iostat_device(timestamp, device);

-- Per-core CPU statistics (from mpstat)
CREATE TABLE IF NOT EXISTS mpstat_core (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    core_id INTEGER NOT NULL,
    user_percent REAL,
    nice_percent REAL,
    system_percent REAL,
    iowait_percent REAL,
    irq_percent REAL,
    soft_percent REAL,
    steal_percent REAL,
    idle_percent REAL,
    busy_percent REAL
);

CREATE INDEX idx_mpstat_core_ts ON mpstat_core(timestamp);
CREATE INDEX idx_mpstat_core_id ON mpstat_core(core_id, timestamp);

-- CPU summary statistics (from mpstat)
CREATE TABLE IF NOT EXISTS mpstat_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    num_cores INTEGER,
    avg_busy_percent REAL,
    max_busy_percent REAL,
    min_busy_percent REAL,
    std_busy_percent REAL,
    avg_iowait_percent REAL,
    max_iowait_percent REAL,
    busy_spread REAL,
    imbalance_ratio REAL,
    cores_idle INTEGER,
    cores_saturated INTEGER
);

CREATE INDEX idx_mpstat_summary_ts ON mpstat_summary(timestamp);

-- Memory and swap statistics (from vmstat)
CREATE TABLE IF NOT EXISTS vmstat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    procs_runnable INTEGER,
    procs_blocked INTEGER,
    swap_used_kb INTEGER,
    free_kb INTEGER,
    buffer_kb INTEGER,
    cache_kb INTEGER,
    swap_in_kb INTEGER,
    swap_out_kb INTEGER,
    blocks_in INTEGER,
    blocks_out INTEGER,
    interrupts INTEGER,
    context_switches INTEGER,
    cpu_user INTEGER,
    cpu_system INTEGER,
    cpu_idle INTEGER,
    cpu_iowait INTEGER,
    cpu_steal INTEGER,
    memory_pressure REAL
);

CREATE INDEX idx_vmstat_ts ON vmstat(timestamp);

-- SLURM node state (from scontrol)
CREATE TABLE IF NOT EXISTS node_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    node_name TEXT NOT NULL,
    state TEXT,
    cpus_total INTEGER,
    cpus_alloc INTEGER,
    cpu_load REAL,
    memory_total_mb INTEGER,
    memory_alloc_mb INTEGER,
    memory_free_mb INTEGER,
    cpu_alloc_percent REAL,
    memory_alloc_percent REAL,
    partitions TEXT,
    reason TEXT,
    features TEXT,
    gres TEXT,
    is_healthy INTEGER
);

CREATE INDEX idx_node_state_ts ON node_state(timestamp);
CREATE INDEX idx_node_state_name ON node_state(node_name, timestamp);

-- GPU statistics (from nvidia-smi)
CREATE TABLE IF NOT EXISTS gpu_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    gpu_index INTEGER,
    gpu_name TEXT,
    gpu_util_percent REAL,
    memory_util_percent REAL,
    memory_used_mb INTEGER,
    memory_total_mb INTEGER,
    memory_free_mb INTEGER,
    temperature_c INTEGER,
    power_draw_w REAL,
    power_limit_w REAL,
    compute_processes INTEGER
);

CREATE INDEX idx_gpu_stats_ts ON gpu_stats(timestamp);
CREATE INDEX idx_gpu_stats_gpu ON gpu_stats(gpu_index, timestamp);

-- NFS I/O statistics (from nfsiostat)
CREATE TABLE IF NOT EXISTS nfs_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    mount_point TEXT NOT NULL,
    server TEXT,
    ops_per_sec REAL,
    read_ops_per_sec REAL,
    write_ops_per_sec REAL,
    read_kb_per_sec REAL,
    write_kb_per_sec REAL,
    avg_rtt_ms REAL,
    avg_exe_ms REAL,
    retrans_percent REAL
);

CREATE INDEX idx_nfs_stats_ts ON nfs_stats(timestamp);
CREATE INDEX idx_nfs_stats_mount ON nfs_stats(mount_point, timestamp);

-- ============================================
-- JOB DATA (Prediction)
-- ============================================

-- Job metadata
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    user_name TEXT NOT NULL,
    group_name TEXT,
    partition TEXT,
    node_list TEXT,  -- Comma-separated
    job_name TEXT,
    submit_time DATETIME,
    start_time DATETIME,
    end_time DATETIME,
    state TEXT,  -- PENDING, RUNNING, COMPLETED, FAILED, TIMEOUT, etc.
    exit_code INTEGER,      -- Exit status (0-255)
    exit_signal INTEGER,    -- Signal number if killed (e.g., 9=SIGKILL, 11=SIGSEGV)
    
    -- Failure classification (categorical factor)
    -- 0=success, 1=timeout, 2=cancelled, 3=failed_generic, 
    -- 4=oom, 5=segfault, 6=node_fail, 7=dependency
    failure_reason INTEGER DEFAULT 0,
    
    -- Requested resources
    req_cpus INTEGER,
    req_mem_mb INTEGER,
    req_gpus INTEGER,
    req_time_seconds INTEGER,
    
    -- Computed
    runtime_seconds INTEGER,
    wait_time_seconds INTEGER
);

CREATE INDEX idx_jobs_user ON jobs(user_name);
CREATE INDEX idx_jobs_partition ON jobs(partition);
CREATE INDEX idx_jobs_submit ON jobs(submit_time);
CREATE INDEX idx_jobs_state ON jobs(state);
CREATE INDEX idx_jobs_failure ON jobs(failure_reason);

-- Job metrics (time-series during job)
CREATE TABLE IF NOT EXISTS job_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    
    -- Compute
    cpu_percent REAL,  -- 0-100 per core, so can exceed 100
    memory_gb REAL,
    vram_gb REAL,
    swap_gb REAL,
    
    -- I/O
    nfs_read_gb REAL,
    nfs_write_gb REAL,
    local_read_gb REAL,
    local_write_gb REAL,
    io_wait_percent REAL,
    
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE INDEX idx_job_metrics_job_ts ON job_metrics(job_id, timestamp);

-- Job I/O samples (from job monitor daemon)
CREATE TABLE IF NOT EXISTS job_io_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    timestamp DATETIME NOT NULL,
    total_read_bytes INTEGER,
    total_write_bytes INTEGER,
    nfs_write_bytes INTEGER,
    local_write_bytes INTEGER,
    nfs_ratio REAL,
    pid_count INTEGER,
    
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE INDEX idx_job_io_samples_job ON job_io_samples(job_id, timestamp);

-- Job summary (computed at job end)
CREATE TABLE IF NOT EXISTS job_summary (
    job_id TEXT PRIMARY KEY,
    
    -- Peak values
    peak_cpu_percent REAL,
    peak_memory_gb REAL,
    peak_vram_gb REAL,
    peak_swap_gb REAL,
    peak_io_wait_percent REAL,
    
    -- Average values
    avg_cpu_percent REAL,
    avg_memory_gb REAL,
    avg_vram_gb REAL,
    avg_io_wait_percent REAL,
    
    -- Total I/O
    total_nfs_read_gb REAL,
    total_nfs_write_gb REAL,
    total_local_read_gb REAL,
    total_local_write_gb REAL,
    
    -- Derived metrics
    nfs_ratio REAL,  -- nfs_write / (nfs_write + local_write)
    used_gpu BOOLEAN,
    had_swap BOOLEAN,
    
    -- Health and prediction
    health_score REAL,  -- 0.0 to 1.0
    cluster_id INTEGER,
    is_anomaly BOOLEAN DEFAULT FALSE,
    anomaly_distance REAL,
    
    -- Feature vector (JSON for flexibility)
    feature_vector TEXT,
    
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

-- ============================================
-- SIMILARITY NETWORK
-- ============================================

-- Similarity edges between jobs
CREATE TABLE IF NOT EXISTS job_similarity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id_a TEXT NOT NULL,
    job_id_b TEXT NOT NULL,
    similarity REAL NOT NULL,  -- Cosine similarity, 0-1
    
    FOREIGN KEY (job_id_a) REFERENCES jobs(job_id),
    FOREIGN KEY (job_id_b) REFERENCES jobs(job_id),
    UNIQUE(job_id_a, job_id_b)
);

CREATE INDEX idx_similarity_a ON job_similarity(job_id_a);
CREATE INDEX idx_similarity_b ON job_similarity(job_id_b);

-- Job clusters
CREATE TABLE IF NOT EXISTS clusters (
    cluster_id INTEGER PRIMARY KEY,
    name TEXT,
    description TEXT,
    centroid TEXT,  -- JSON feature vector
    job_count INTEGER,
    avg_health REAL,
    failure_rate REAL,
    dominant_issue TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- SIMULATION MODEL
-- ============================================

-- Empirical distributions (learned from data)
CREATE TABLE IF NOT EXISTS distributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_name TEXT NOT NULL,
    profile_name TEXT,  -- Optional: per-profile distributions
    distribution_type TEXT NOT NULL,  -- normal, lognormal, beta, etc.
    parameters TEXT NOT NULL,  -- JSON: {mu, sigma} or {alpha, beta}
    min_value REAL,
    max_value REAL,
    sample_count INTEGER,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Simulation runs
CREATE TABLE IF NOT EXISTS simulation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date DATETIME DEFAULT CURRENT_TIMESTAMP,
    n_simulated INTEGER,
    coverage_percent REAL,
    avg_distance_to_real REAL,
    anomaly_rate REAL,
    notes TEXT
);

-- ============================================
-- ALERTS & EVENTS
-- ============================================

-- Alert definitions (rules)
CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    category TEXT NOT NULL,  -- disk, queue, node, job, license
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    condition_type TEXT NOT NULL,  -- threshold, derivative, custom
    condition_config TEXT NOT NULL,  -- JSON
    message_template TEXT NOT NULL,
    cooldown_seconds INTEGER DEFAULT 3600,
    enabled BOOLEAN DEFAULT TRUE
);

-- Alert instances
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    source TEXT,  -- Filesystem path, node name, job_id, etc.
    message TEXT NOT NULL,
    details TEXT,  -- JSON with context
    
    -- State
    acknowledged BOOLEAN DEFAULT FALSE,
    acknowledged_by TEXT,
    acknowledged_at DATETIME,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_at DATETIME,
    
    -- Deduplication
    dedup_key TEXT,
    occurrence_count INTEGER DEFAULT 1,
    last_occurrence DATETIME,
    
    FOREIGN KEY (rule_id) REFERENCES alert_rules(id)
);

CREATE INDEX idx_alerts_ts ON alerts(timestamp);
CREATE INDEX idx_alerts_severity ON alerts(severity);
CREATE INDEX idx_alerts_category ON alerts(category);
CREATE INDEX idx_alerts_resolved ON alerts(resolved);
CREATE INDEX idx_alerts_dedup ON alerts(dedup_key);

-- Alert dispatch log
CREATE TABLE IF NOT EXISTS alert_dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    channel TEXT NOT NULL,  -- email, slack, webhook
    recipient TEXT NOT NULL,
    sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    success BOOLEAN NOT NULL,
    error_message TEXT,
    
    FOREIGN KEY (alert_id) REFERENCES alerts(id)
);

-- ============================================
-- RECOMMENDATIONS
-- ============================================

-- Data-driven defaults
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_name TEXT NOT NULL,
    threshold REAL NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('above', 'below')),
    success_rate REAL NOT NULL,
    improvement REAL NOT NULL,  -- Percentage point improvement
    sample_size INTEGER NOT NULL,
    confidence REAL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Per-user recommendations
CREATE TABLE IF NOT EXISTS user_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_name TEXT NOT NULL,
    recommendation_type TEXT NOT NULL,
    message TEXT NOT NULL,
    priority INTEGER DEFAULT 1,
    based_on_jobs INTEGER,  -- Number of jobs analyzed
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    dismissed BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_user_recs_user ON user_recommendations(user_name);

-- ============================================
-- METADATA & SYSTEM
-- ============================================

-- System configuration
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Collection runs log
CREATE TABLE IF NOT EXISTS collection_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collector TEXT NOT NULL,
    started_at DATETIME NOT NULL,
    completed_at DATETIME,
    success BOOLEAN,
    records_collected INTEGER,
    error_message TEXT
);

CREATE INDEX idx_collection_log_collector ON collection_log(collector, started_at);

-- Schema version
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

INSERT INTO schema_version (version, description) VALUES (1, 'Initial schema');
INSERT OR IGNORE INTO schema_version (version, description) VALUES (2, 'Added mpstat, vmstat, node_state, gpu_stats, nfs_stats tables');
INSERT OR IGNORE INTO schema_version (version, description) VALUES (3, 'Added exit_signal, failure_reason to jobs table');

-- ============================================
-- VIEWS (Convenience)
-- ============================================

-- Recent alerts summary
CREATE VIEW IF NOT EXISTS v_recent_alerts AS
SELECT 
    a.id,
    a.timestamp,
    a.severity,
    a.category,
    a.source,
    a.message,
    a.acknowledged,
    a.resolved,
    a.occurrence_count
FROM alerts a
WHERE a.timestamp > datetime('now', '-7 days')
ORDER BY a.timestamp DESC;

-- Job health overview
CREATE VIEW IF NOT EXISTS v_job_health AS
SELECT 
    j.job_id,
    j.user_name,
    j.partition,
    j.state,
    j.runtime_seconds,
    js.health_score,
    js.nfs_ratio,
    js.used_gpu,
    js.had_swap,
    js.is_anomaly,
    c.name as cluster_name
FROM jobs j
LEFT JOIN job_summary js ON j.job_id = js.job_id
LEFT JOIN clusters c ON js.cluster_id = c.cluster_id
WHERE j.end_time IS NOT NULL
ORDER BY j.end_time DESC;

-- Filesystem trends
CREATE VIEW IF NOT EXISTS v_filesystem_trends AS
SELECT 
    path,
    used_percent,
    fill_rate_bytes_per_day / (1024*1024*1024) as fill_rate_gb_per_day,
    first_derivative * 86400 / (1024*1024*1024) as rate_gb_per_day,
    second_derivative * 86400 * 86400 / (1024*1024*1024) as acceleration_gb_per_day2,
    days_until_full,
    timestamp
FROM filesystems
WHERE timestamp > datetime('now', '-24 hours')
ORDER BY path, timestamp;

-- User failure rates
CREATE VIEW IF NOT EXISTS v_user_failure_rates AS
SELECT 
    j.user_name,
    COUNT(*) as total_jobs,
    SUM(CASE WHEN j.state = 'COMPLETED' AND js.health_score > 0.5 THEN 1 ELSE 0 END) as successful_jobs,
    AVG(js.health_score) as avg_health,
    AVG(js.nfs_ratio) as avg_nfs_ratio
FROM jobs j
LEFT JOIN job_summary js ON j.job_id = js.job_id
WHERE j.end_time > datetime('now', '-30 days')
GROUP BY j.user_name
HAVING total_jobs >= 5
ORDER BY avg_health ASC;

-- ============================================
-- INTERACTIVE SESSIONS
-- ============================================

-- Interactive server definitions
CREATE TABLE IF NOT EXISTS interactive_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    method TEXT NOT NULL CHECK (method IN ('local', 'ssh')),
    ssh_host TEXT,
    ssh_user TEXT,
    enabled BOOLEAN DEFAULT TRUE,
    last_collection DATETIME
);

-- Interactive session snapshots
CREATE TABLE IF NOT EXISTS interactive_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    server_id TEXT NOT NULL,
    user TEXT NOT NULL,
    session_type TEXT NOT NULL,  -- RStudio, Jupyter (Python), Jupyter (R), Jupyter Server
    pid INTEGER,
    cpu_percent REAL,
    mem_percent REAL,
    mem_mb REAL,
    mem_virtual_mb REAL,
    start_time DATETIME,
    age_hours REAL,
    is_idle BOOLEAN,
    
    FOREIGN KEY (server_id) REFERENCES interactive_servers(id)
);

CREATE INDEX idx_interactive_sessions_ts ON interactive_sessions(timestamp);
CREATE INDEX idx_interactive_sessions_server ON interactive_sessions(server_id, timestamp);
CREATE INDEX idx_interactive_sessions_user ON interactive_sessions(user, timestamp);

-- Interactive session summary (aggregated per collection)
CREATE TABLE IF NOT EXISTS interactive_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    server_id TEXT NOT NULL,
    total_sessions INTEGER,
    idle_sessions INTEGER,
    total_memory_mb REAL,
    unique_users INTEGER,
    rstudio_sessions INTEGER,
    jupyter_python_sessions INTEGER,
    jupyter_r_sessions INTEGER,
    stale_sessions INTEGER,      -- idle > threshold hours
    memory_hog_sessions INTEGER, -- mem > threshold MB
    
    FOREIGN KEY (server_id) REFERENCES interactive_servers(id)
);

CREATE INDEX idx_interactive_summary_ts ON interactive_summary(timestamp);
CREATE INDEX idx_interactive_summary_server ON interactive_summary(server_id, timestamp);

INSERT OR IGNORE INTO schema_version (version, description) VALUES (4, 'Added interactive session tables');
