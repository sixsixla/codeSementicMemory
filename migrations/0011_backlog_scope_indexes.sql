-- Backlog planning is task-coalesced.  These indexes keep the planner from
-- rescanning extraction runs for every event job and from parsing every
-- outbox payload while joining a task batch.
CREATE INDEX IF NOT EXISTS idx_extraction_runs_task_status
    ON extraction_runs(task_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_outbox_task_status
    ON outbox(json_extract(payload_json, '$.task_id'), status, job_type, created_at);
