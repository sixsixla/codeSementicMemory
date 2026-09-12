CREATE TABLE IF NOT EXISTS agent_memory_maintenance (
    maintenance_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES agent_memory_cycles(cycle_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    source_thread_id TEXT NOT NULL,
    request_turn_id TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'completed', 'failed', 'superseded')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 2 CHECK(max_attempts > 0),
    provider TEXT NOT NULL DEFAULT 'agent',
    model TEXT,
    note_count INTEGER NOT NULL DEFAULT 0 CHECK(note_count >= 0),
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK(candidate_count >= 0),
    last_error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    requested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(cycle_id, input_hash)
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_maintenance_cycle_status
    ON agent_memory_maintenance(cycle_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_memory_maintenance_project_status
    ON agent_memory_maintenance(project_id, status, updated_at DESC);
