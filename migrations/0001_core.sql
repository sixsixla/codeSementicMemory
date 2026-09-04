-- CodeSementicMemory first-round canonical schema.
-- SQLite is the source of truth; search and graph tables are rebuildable.

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repositories (
    repo_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    root_path TEXT NOT NULL,
    vcs_type TEXT NOT NULL DEFAULT 'unknown',
    remote_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, root_path)
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'completed', 'abandoned', 'unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    agent_id TEXT NOT NULL,
    adapter TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'ended', 'abandoned', 'unknown')),
    last_seq INTEGER NOT NULL DEFAULT -1 CHECK(last_seq >= -1),
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES repositories(repo_id) ON DELETE RESTRICT,
    vcs_revision TEXT,
    branch TEXT,
    captured_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'unknown',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    uri TEXT,
    path TEXT,
    mime_type TEXT,
    size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    external_event_id TEXT,
    schema_version TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    producer_agent_id TEXT NOT NULL,
    producer_adapter TEXT NOT NULL,
    producer_adapter_version TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    session_id TEXT REFERENCES sessions(session_id) ON DELETE RESTRICT,
    seq INTEGER NOT NULL CHECK(seq >= 0),
    -- Parent links are intentionally not a foreign key: adapters may deliver
    -- events out of order. The link can be reconciled when the parent arrives.
    parent_event_id TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    payload_json TEXT NOT NULL DEFAULT '{}',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    redaction_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'unknown',
    completeness TEXT NOT NULL DEFAULT 'full',
    event_hash TEXT NOT NULL,
    UNIQUE(event_hash, event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_external_identity
    ON events(producer_agent_id, producer_adapter, external_event_id)
    WHERE external_event_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_session_seq
    ON events(session_id, seq)
    WHERE session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_events_task_time ON events(task_id, occurred_at, seq);
CREATE INDEX IF NOT EXISTS idx_events_project_time ON events(project_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_parent ON events(parent_event_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(
    event_id UNINDEXED,
    project_id UNINDEXED,
    task_id UNINDEXED,
    text,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS outbox (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'processing', 'retry', 'completed', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    available_at TEXT NOT NULL,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_type, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_outbox_ready ON outbox(status, available_at);
CREATE INDEX IF NOT EXISTS idx_outbox_lease ON outbox(status, lease_until);
