CREATE TABLE IF NOT EXISTS agent_memory_cycles (
    cycle_id TEXT PRIMARY KEY,
    source_system TEXT NOT NULL DEFAULT 'codex',
    source_thread_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'checkpointed', 'closed', 'failed')),
    cursor TEXT,
    prompt_count INTEGER NOT NULL DEFAULT 0 CHECK(prompt_count >= 0),
    last_event_seq INTEGER NOT NULL DEFAULT -1 CHECK(last_event_seq >= -1),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE(source_system, source_thread_id, project_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_cycles_project_status
    ON agent_memory_cycles(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS memory_card_feedback (
    feedback_id TEXT PRIMARY KEY,
    feedback_key TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    card_id TEXT NOT NULL REFERENCES memory_cards(card_id) ON DELETE RESTRICT,
    turn_id TEXT NOT NULL,
    feedback_type TEXT NOT NULL
        CHECK(feedback_type IN ('presented', 'used', 'outcome')),
    outcome TEXT NOT NULL DEFAULT 'unknown'
        CHECK(outcome IN ('success', 'failed', 'partial', 'unknown')),
    used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0, 1)),
    reason TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_card_feedback_card
    ON memory_card_feedback(card_id, feedback_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_card_feedback_task
    ON memory_card_feedback(task_id, session_id, turn_id);
