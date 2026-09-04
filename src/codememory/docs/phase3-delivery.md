# Phase 3 delivery record

Status: implemented locally on 2026-09-04.

Phase 3 completes the first useful coding-memory vertical slice: visible agent
history is converted into evidence-backed observations, repeated observations
are consolidated into versioned cards, and every card remains inspectable from
the canonical SQLite event store.

## What shipped

### Versioned card projection

Migration `0005_memory_cards.sql` adds these rebuildable/governed records:

- `memory_cards`: one logical coding-memory card per project/kind/key;
- `memory_card_versions`: append-only statements, aliases, confidence,
  validity, and `supersedes` history;
- `memory_card_evidence`: candidate and event provenance for every version;
- `memory_card_bindings`: typed file/symbol targets with role, snapshot slot,
  status, and binding evidence;
- `memory_card_links`: explicit card/candidate/event/file/symbol/task relations;
- `consolidation_decisions`: deterministic, idempotent policy decisions;
- `memory_card_lifecycle_events`: auditable status transitions;
- `memory_card_fts`: a rebuildable local search projection.

Candidate rows remain immutable proposals. The consolidator validates that all
candidate evidence exists, matches cards by normalized coding text and targets,
merges repeated evidence, and creates a new version when a statement or binding
changes materially. It never lets a provider overwrite a stable card directly.
Forced extraction retries follow the same rule: unreferenced candidate
projections may be replaced transactionally, while candidates already used by a
card are retained and the retry is linked as a new observation.

Cards begin as `proposed` (or `uncertain` when the match is ambiguous). An
operator or a future code-snapshot verifier can move them through the guarded
state machine (`verified`, `stable`, `stale`, `superseded`, `rejected`). Every
transition records actor, reason, and time. Promotion is explicit; confidence
alone does not silently make a card stable.

### Coding-focused extraction

The offline `MockLLMProvider` is now a deterministic coding extractor rather
than a generic transcript summarizer. It:

- chooses a compact user-language intent;
- separates modified files from merely mentioned/context-hint files;
- emits route, decision, anti-binding, failure, and actual validation
  observations only when visible evidence supports them;
- returns an empty candidate list for empty/admin threads instead of polluting
  the long-term store with `unclassified` cards;
- keeps confidence and uncertainty explicit so a later local/cloud model can be
  compared against the same contract.

`OpenAICompatibleProvider` is an optional provider boundary for Ollama, LM
Studio, a local gateway, or an OpenAI-compatible endpoint. It is never called
unless selected with `--provider openai-compatible`/`local`; strict Pydantic
validation and evidence checks still run locally after the response.

### Complete visible Codex history replay

`CodexHistoryImporter` accepts the history-manager JSON export shape and the
portable `read_thread` shape. It processes one thread and bounded event chunks
at a time, so a large export does not require one giant transaction. Replaying
the same export is idempotent; changed identities are reported as conflicts
instead of overwriting an existing event.

The importer preserves thread id, host, title, cwd, source/model metadata,
history file-path hints, timestamps, and an explicit `partial`/`full`/
`summary` completeness value. Entity `created_at`/`updated_at` use source event
time, while `events.ingested_at` records the replay time; importing an old
archive therefore does not make it look like a newly edited task.

Only visible user/assistant/tool/file/command/validation records are retained.
Known host-injected blocks, hidden reasoning tags, and credentials are removed
before persistence. A visible history-manager export is marked `partial`
because it is a flattened index, not a claim that private Codex transcript
storage was copied.

## End-to-end local run

The raw export and SQLite backups belong outside this repository. A typical
repeatable run is:

```powershell
$db = "$env:LOCALAPPDATA\CodeSementicMemory\codememory.sqlite3"
$history = "C:\Users\Administrator\.codex-history-manager\exports\codememory-all-20260904.json"

py -X utf8 -m codememory import-codex-history $history --db $db --batch-size 250
py -X utf8 -m codememory extract-all --db $db --provider mock --limit 200 `
  --min-events 2 --consolidate --complete-outbox --summary-only
py -X utf8 -m codememory health --db $db
py -X utf8 -m codememory cards --db $db --limit 20
py -X utf8 -m codememory serve --db $db --host 127.0.0.1 --port 8876
```

`--limit 200` is a deliberate first-pass quality/performance bound for recent
high-value development tasks. The canonical importer can ingest all threads;
extraction can be resumed in later batches by changing `--limit`/`--since`.
Progress is written to stderr, while `--summary-only` keeps stdout machine
readable. Use `--provider local` with `CODEMEMORY_LLM_BASE_URL` and
`CODEMEMORY_LLM_MODEL` when a local model is available.

## Real local Codex replay evidence

The authorized local history export was replayed into the application database
on 2026-09-04. The importer saw 2,573 visible, locally indexed threads and
created 54,684 canonical events across 16 derived projects (2,573 tasks,
2,573 sessions, and 13,179 bounded artifact records). Replaying the same
source is idempotent. The import report recorded 23 changed-identity
conflicts; those records were reported and skipped rather than overwriting an
existing event.

The first coding-focused extraction/consolidation pass intentionally prioritized
the recent/high-frequency workspaces and the CodeSementicMemory workspace. It
contains 1,082 successful runs from that pass plus one resumed task run (1,083
successful runs total), 2,304 candidate observations, 1,798 proposed cards,
1,959 card versions, 28,368 card-evidence rows, 9,969 typed bindings, 41,789
relation links, and 2,304 consolidation decisions. The remaining imported tasks
stay available for bounded `extract-all` continuation; no claim is made that
every imported task has already been LLM-extracted.

The replay path strips known host-injected blocks, reasoning/context-compaction
items, synthetic `AGENTS.md`/skill preambles, and common credential patterns
before persistence. A post-rebuild scan found no retained
`AGENTS.md instructions`, `<skill>`, `recommended_plugins`, or hidden
reasoning markers in candidate statements. This is a deterministic policy
check, not a guarantee that arbitrary secrets in an untrusted source can never
exist; raw exports and database backups therefore remain outside Git.

The default `codememory health` command and `GET /v1/health?deep=false` use
SQLite `quick_check`/no-scan semantics suitable for the local UI. Use
`codememory health --deep` (or `GET /v1/health?deep=true`) for the exhaustive
integrity scan during maintenance. The delivered database completed the
exhaustive CLI doctor check with `integrity_check=ok`.

## Inspection surface

REST endpoints include:

- `POST /v1/tasks/{task_id}/consolidate`;
- `GET /v1/cards`, `/v1/cards/search`, `/v1/cards/{card_id}`;
- `GET /v1/cards/{card_id}/history` and `/relations`;
- `POST /v1/cards/{card_id}/transition`;
- `GET /v1/graph`, which extends the candidate graph with card/version/file/
  symbol nodes and removes dangling edges.

The local Three.js inspector at `/` displays tasks, candidate memories,
versioned cards, confidence/status, bindings, version history, and source
metadata. The graph is a UI projection; SQLite facts and provenance remain the
authority and can rebuild it.

## Verification boundary

Automated checks cover schema migration, idempotent replay, redaction,
bounded/head-tail context assembly, strict provider validation, card merge and
version behavior, lifecycle guards, graph closure, API routes, and JavaScript
syntax. They do not claim Unity runtime behavior, current code-symbol validity,
production test success, or multiplayer/Wwise correctness. Those require the
future snapshot/AST/LSP integration and project-specific QA gates.

The repository regression gate for this delivery is `53 passed` (one upstream
Starlette/httpx deprecation warning), plus passing Ruff, Python bytecode
compilation, JavaScript syntax, and `git diff --check`. The local HTTP smoke
returned 200 for `/`, `/v1/health`, `/v1/tasks`, `/v1/cards`, `/v1/cards/search`,
and `/v1/graph`; the bounded graph response was closed (no dangling edges).

## Remaining slices

1. **Phase 4 — code snapshot verification:** resolve paths/qualified symbols
   against a chosen Git/P4 snapshot, mark bindings verified/missing/renamed,
   and derive stale/supersede events from diffs.
2. **Phase 5 — retrieval/adapters:** add optional embeddings as a rebuildable
   projection, hybrid structured/FTS/vector retrieval, and a narrow MCP/agent
   adapter that submits `codememory.event.v1` events.
3. **Phase 6 — operations:** incremental history checkpoints, retention and
   redaction audits, UI filtering/layout, and benchmark fixtures based on
   accepted/rejected coding routes.

Neither embeddings, a graph database, nor a cloud memory vendor is required
for the SQLite core delivered here.
