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
py -m codememory agent query "<business term or symbol>" --project-id $project --db $db
```

For local visual inspection, start `py -m codememory serve --db $db` and open
`http://127.0.0.1:8765/`. A result with conflicts, dead outbox jobs, or unexpected
cross-project evidence must be reported for review rather than auto-promoted.
