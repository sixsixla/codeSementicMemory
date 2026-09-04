-- Preserve the exact normalized snapshot contract used for each verification.
-- The manifest is content-addressed by ``manifest_hash`` and contains only
-- paths/symbols/revision metadata supplied by an adapter, never source bytes.

ALTER TABLE code_snapshots
    ADD COLUMN manifest_json TEXT NOT NULL DEFAULT '{}';
