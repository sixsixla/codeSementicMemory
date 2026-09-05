---
name: codememory-memory
description: Import visible Codex coding history into local CodeSementicMemory, extract candidate memories, consolidate cards, and verify project-scoped retrieval. Use when the user asks to remember, learn from, refresh, or query past coding conversations; do not use for generic chat summaries.
---

# CodeMemory 历史记忆维护

Use this skill to operate CodeSementicMemory from Codex. The user should describe the
history or project in natural language; the skill performs the underlying local
commands and reports the result. Do not ask the user to manually type the commands.

## Safety and scope

- Capture only visible coding evidence: user requests, visible assistant summaries,
  file/tool evidence, validations, and outcomes.
- Never copy hidden reasoning, system instructions, credentials, plugin catalogs, or
  unrelated conversation text into memory.
- Do not read or modify Codex Desktop private databases. Use a provided history JSON,
  a visible `read_thread` result when that tool is available, or ask for an export.
- Do not write SQLite directly. Use the repository CLI/API so redaction, idempotency,
  quality gates, outbox, and projections remain active.
- Preserve the caller's explicit `project_id`; never infer that two similarly named
  projects are the same without evidence.

## Workflow

Read [references/operations.md](references/operations.md) before handling a history
import or refresh. Then:

1. Resolve the CodeMemory repository and database. Prefer the repository's `py -m
   codememory` entry point and an explicit database path; use `CODEMEMORY_DB` or the
   platform default only when the user has not supplied one.
2. Resolve the source. For a referenced/current Codex task, use the available
   `read_thread` capability to obtain visible turns and normalize them into a bounded
   `threads` JSON object. If no visible source is available, ask for a thread id or
   export instead of fabricating history.
3. Choose the smallest bounded update: one or a few selected threads for a new
   import, one task for a targeted refresh, or `extract-all` with project/time/event
   limits for a deliberate batch refresh.
4. Import new history with `import-codex-history`, then request extraction and
   consolidation in the same run. For already imported events, use `extract` or
   bounded `extract-all` followed by consolidation.
5. Select the provider explicitly. Use `openai-compatible`/`local` only when the
   corresponding `CODEMEMORY_LLM_*` environment is configured; otherwise use the
   deterministic `mock` provider and clearly report that no LLM call was made. Never
   invent or print API keys.
6. Avoid `--force` unless the user explicitly requests a re-extraction or the
   extractor/prompt/schema policy changed. Normal reruns should remain idempotent.
7. Validate the result with health, quality, and a project-scoped memory query. If
   conflicts, quarantined candidates, or cross-project evidence appear, stop and
   report them instead of silently promoting cards.

## Response contract

Report, in plain language:

- source threads/tasks and project scope;
- imported events: accepted, duplicate, conflict, skipped;
- extraction status, candidate counts, quality decisions;
- card actions: created, merged, new versions, rejected/uncertain;
- one or two representative memory-query results with file/symbol evidence;
- warnings such as summary completeness, missing live binding verification, or a
  deterministic mock fallback.

Treat cards as proposals backed by evidence, not as a replacement for checking the
current source tree. The normal memory update is append-only at the event layer and
versioned at the card layer.

## Codex automatic loop

The repository ships `integrations/codex/codememory_hook.py` and a matching
`integrations/codex/hooks.json` template. The installed global Codex hook uses
the same cycle service automatically:

- `SessionStart` opens or resumes a durable cycle cursor.
- `UserPromptSubmit` records the visible prompt and injects route-first cards as
  additional context. These are hypotheses, not authoritative code facts.
- `PostToolUse` records bounded visible file-read/edit evidence without running
  extraction on every tool call.
- `Stop` checkpoints the visible assistant result, runs bounded extraction and
  consolidation, and records any explicitly used card ids.
- `SessionEnd` closes the cycle without a second expensive extraction pass.

Hooks fail open and only read visible hook fields. If Codex asks for hook review,
approve the project/global hook once in the app; this is a product security
boundary, not a memory workflow decision. The manual commands remain the
fallback for imported historical threads and adapters without lifecycle hooks.
