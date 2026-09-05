# CodeMemory 操作参考

## Accepted history input

`import-codex-history` accepts either:

```json
{"threads": [{"id": "thread-id", "title": "...", "cwd": "...", "turns": []}]}
```

or one thread object. A turn may contain `items` with visible `userMessage`,
`assistantMessage`, `fileChange`, `commandExecution`, `toolCall`, and `toolResult`
records. The importer creates deterministic `codex-thread:<id>` task and
`codex-session:<id>` session identities. It filters known host blocks and hidden
reasoning before ingestion.

If the Codex app exposes a `read_thread` result, keep the visible thread metadata
and turns, and write only a bounded temporary JSON package. Do not copy the whole
private application database.

## New history: import, extract, consolidate

Use the repository root and an explicit database path:

```powershell
py -m codememory import-codex-history $history `
  --db $db `
  --project-id $project `
  --max-threads 10 `
  --extract `
  --consolidate `
  --provider mock `
  --summary-only
```

The command is safe to replay. Existing canonical event identities become
`duplicate`; changed/new visible evidence is appended and creates a new extraction
input window.

Use `--since <ISO timestamp>` or a small `--max-threads` for incremental imports.
Use `--atomic` only for small, trusted fixtures; bounded imports are safer for large
history files.

## Already imported events: refresh projections

For one task:

```powershell
py -m codememory extract $task_id --db $db --provider mock
py -m codememory consolidate --db $db --task-id $task_id
```

For a bounded project refresh:

```powershell
py -m codememory extract-all --db $db --project-id $project `
  --limit 100 `
  --consolidate `
  --complete-outbox `
  --summary-only
```

Unchanged extraction windows return `duplicate`. Use `--force` only for an explicit
policy/model/prompt change or a user-requested correction.

## Provider selection

`mock` is offline and deterministic; it is useful for validating the complete data
flow but does not call an LLM. For a configured OpenAI-compatible or local endpoint,
set `CODEMEMORY_LLM_BASE_URL`, `CODEMEMORY_LLM_API_KEY` when required, and
`CODEMEMORY_LLM_MODEL`, then select `--provider openai-compatible` or `--provider
local`. Keep secrets in the environment, never in history JSON or event payloads.

## Validation and inspection

After an update, inspect:

```powershell
py -m codememory health --db $db
py -m codememory quality-report --db $db --project-id $project
py -m codememory agent query "<business term or symbol>" --project-id $project \
  --retrieval-mode route --db $db
```

For local visual inspection, start `py -m codememory serve --db $db` and open
`http://127.0.0.1:8765/`. A result with conflicts, dead outbox jobs, or unexpected
cross-project evidence must be reported for review rather than auto-promoted.

### Retrieval modes

`route` is the default coding-agent mode. It intentionally keeps non-quarantined
`proposed` and `review` cards visible as low-confidence entry hints, while ranking
`stable` and `verified` cards first. Each result includes `route.trust_level`,
`route.score`, binding status, and quality decision so the agent can verify the
current source before editing.

Use `trusted` when only quality-accepted `verified`/`stable` cards should be
returned. Use `audit` to inspect terminal and lifecycle states; combine it with
`--include-quarantine` only for explicit diagnostics.

## Automatic cycle API

The same flow is available to an adapter without reading a Codex transcript:

```powershell
py -m codememory agent cycle open cycle-open.json --db $db
py -m codememory agent cycle prompt cycle-prompt.json --db $db
py -m codememory agent cycle checkpoint cycle-checkpoint.json --db $db
py -m codememory agent cycle close cycle-close.json --db $db
```

For the current Codex LLM's structured extraction, first request a bounded
packet and then submit the resulting notes:

```powershell
py -m codememory agent cycle prepare --project-id $project `
  --source-thread-id $thread --session-id $session --db $db
py -m codememory agent cycle learn cycle-learn.json --db $db
```

`cycle-learn.json` contains `input_hash` from `prepare` and at most eight
`notes`; every note must reference exact `evidence_event_ids`, and every binding
must reference evidence belonging to that note. The service runs the normal
quality gate and consolidator after validating the current-agent output.

The JSON payloads use `project_id`, `source_thread_id`, optional `task_id` and
`session_id`, and the operation-specific fields. `turn_id` makes prompt and
checkpoint retries idempotent. The API equivalents are
`/v1/agent/cycle/open`, `/prompt`, `/checkpoint`, and `/close`.

The Codex hook maps the current working directory to a project (`Project_J` to
`project_j`, the CodeMemory repository to `codeSementicMemory`, or an explicit
`CODEMEMORY_PROJECT_ID`). Set `CODEMEMORY_DB` to override the local database and
`CODEMEMORY_HOOK_PROVIDER` to use a configured extraction provider.
