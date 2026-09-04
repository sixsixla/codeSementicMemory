"""Bounded deterministic context assembly for coding-memory extraction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from ..storage.repository import MemoryRepository, json_text


@dataclass(frozen=True)
class AssembledContext:
    project_id: str
    task_id: str
    session_id: str | None
    event_ids: tuple[str, ...]
    events: tuple[dict[str, Any], ...]
    text: str
    input_hash: str
    truncated: bool
    stats: dict[str, int]
    extraction_run_id: str | None = None

    def with_run_id(self, run_id: str) -> "AssembledContext":
        return AssembledContext(
            project_id=self.project_id,
            task_id=self.task_id,
            session_id=self.session_id,
            event_ids=self.event_ids,
            events=self.events,
            text=self.text,
            input_hash=self.input_hash,
            truncated=self.truncated,
            stats=self.stats,
            extraction_run_id=run_id,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ContextAssembler:
    """Build a stable, size-bounded view of one task's evidence window.

    The assembler only reads canonical event rows.  It removes duplicate event
    ids, preserves chronological order, and hashes the exact selected JSON so a
    repeated extraction can be recognized without an embedding or an LLM.
    """

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        max_events: int = 500,
        max_chars: int = 120_000,
        max_event_chars: int = 12_000,
    ) -> None:
        self.repository = repository
        self.max_events = max(1, min(int(max_events), 5000))
        self.max_chars = max(1000, min(int(max_chars), 2_000_000))
        self.max_event_chars = max(200, min(int(max_event_chars), 100_000))

    @staticmethod
    def _payload_text(event: dict[str, Any]) -> str:
        payload = event.get("payload") or {}
        if isinstance(payload, dict):
            preferred = payload.get("text") or payload.get("message") or payload.get("summary")
            if isinstance(preferred, str) and preferred.strip():
                return preferred.strip()
            # A replayed compact message can retain only bookkeeping keys
            # (item_id/raw_item_type/turn_id) after host-content sanitization.
            # Serializing that metadata as natural-language text makes the
            # extractor mistake it for a user intent.  Message rows with no
            # visible text are deliberately represented as empty evidence;
            # structured tool/file rows still use their JSON projection below.
            if event.get("event_type") in {"user_message", "assistant_message"}:
                return ""
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _event_projection(event: dict[str, Any], text: str) -> dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        artifacts = event.get("artifacts") if isinstance(event.get("artifacts"), list) else []
        paths: list[str] = []
        for item in artifacts:
            if isinstance(item, dict) and item.get("path"):
                paths.append(str(item["path"]))
        if isinstance(payload, dict):
            for key in ("path", "file_path", "file", "paths", "files"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    paths.append(value.strip())
                elif isinstance(value, list):
                    paths.extend(str(item).strip() for item in value if str(item).strip())
        bounded_payload: Any = payload
        payload_json = json_text(payload)
        if len(payload_json) > 12_000:
            bounded_payload = {
                "text": text,
                "keys": sorted(str(key) for key in payload.keys())[:80],
                "truncated": True,
            }
        context = event.get("context") if isinstance(event.get("context"), dict) else {}
        context_projection = {
            key: context[key]
            for key in ("root_path", "branch", "codex_thread_id", "codex_host_id", "history_completeness", "history_file_paths")
            if key in context and context[key] not in (None, "", [])
        }
        return {
            "event_id": event.get("event_id"),
            "event_type": event.get("event_type"),
            "occurred_at": event.get("occurred_at"),
            "seq": event.get("seq"),
            "session_id": event.get("session_id"),
            "source": event.get("source"),
            "completeness": event.get("completeness"),
            "text": text,
            "paths": sorted(set(paths)),
            "payload": bounded_payload,
            "context": context_projection,
        }

    def assemble(self, task_id: str, *, session_id: str | None = None) -> AssembledContext:
        raw_events = self.repository.timeline(task_id, session_id=session_id, limit=5000)
        unique_events: list[dict[str, Any]] = []
        seen: set[str] = set()
        for event in raw_events:
            event_id = str(event.get("event_id", "")).strip()
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            unique_events.append(event)

        # Long coding tasks often put the useful file edits and validation at
        # the end of the transcript.  A simple ``LIMIT max_events`` silently
        # drops exactly that evidence, so reserve a deterministic tail window
        # while retaining the beginning for the original user intent.
        truncated = len(unique_events) > self.max_events
        if truncated:
            head_count = min(max(2 if self.max_events > 1 else 1, self.max_events // 3), 120)
            head_count = min(head_count, self.max_events)
            tail_count = max(0, self.max_events - head_count)
            window_events = [*unique_events[:head_count]]
            if tail_count:
                window_events.extend(unique_events[-tail_count:])
        else:
            window_events = unique_events

        projections: list[dict[str, Any]] = []
        for event in window_events:
            text = self._payload_text(event)
            if len(text) > self.max_event_chars:
                text = text[: self.max_event_chars] + "\n[…event text truncated…]"
                truncated = True
            projections.append(self._event_projection(event, text))

        # Keep both sides of a bounded window when a few verbose assistant
        # messages exhaust the character budget.  The head and tail budgets
        # are independent; omitted middle records remain represented by the
        # ``truncated`` flag and can be reassembled from canonical events.
        # ``truncated`` can also become true when a single event exceeds
        # ``max_event_chars`` even if the event count itself is below
        # ``max_events``.  Compute the split here (rather than only in the
        # earlier count-truncation branch) so verbose but short tasks do not
        # hit an unbound ``head_count`` error.
        if truncated and len(projections) > 1:
            window_count = min(self.max_events, len(projections))
            head_count = min(
                max(2 if window_count > 1 else 1, window_count // 3), 120
            )
            head_count = min(head_count, window_count)
            tail_count = max(0, window_count - head_count)
            head_budget = max(1, self.max_chars // 3)
            tail_budget = max(1, self.max_chars - head_budget)
            head: list[dict[str, Any]] = []
            used_head = 0
            for projection in projections[:head_count]:
                size = len(json_text(projection))
                if head and used_head + size > head_budget:
                    break
                head.append(projection)
                used_head += size
            tail: list[dict[str, Any]] = []
            used_tail = 0
            for projection in reversed(projections[-tail_count:] if tail_count else []):
                size = len(json_text(projection))
                if tail and used_tail + size > tail_budget:
                    break
                tail.append(projection)
                used_tail += size
            selected_ids = {id(item) for item in [*head, *tail]}
            selected = [item for item in projections if id(item) in selected_ids]
        else:
            selected = []
            used_chars = 0
            for projection in projections:
                encoded_len = len(json_text(projection))
                if selected and used_chars + encoded_len > self.max_chars:
                    truncated = True
                    break
                selected.append(projection)
                used_chars += encoded_len

        if not selected:
            raise ValueError(f"no canonical events found for task_id={task_id!r}")
        first = raw_events[0]
        project_id = str(first.get("project_id") or "unknown")
        session_ids = {
            str(event.get("session_id"))
            for event in raw_events
            if event.get("session_id")
        }
        effective_session_id = session_id
        if effective_session_id is None and len(session_ids) == 1:
            effective_session_id = next(iter(session_ids))
        event_ids = tuple(str(item["event_id"]) for item in selected)
        canonical = {
            "project_id": project_id,
            "task_id": task_id,
            "session_id": effective_session_id,
            "events": selected,
            "truncated": truncated,
        }
        input_hash = hashlib.sha256(json_text(canonical).encode("utf-8")).hexdigest()
        lines = [
            f"[{item['seq']}] {item['event_type']} {item['event_id']} :: {item['text']}"
            for item in selected
        ]
        return AssembledContext(
            project_id=project_id,
            task_id=task_id,
            session_id=effective_session_id,
            event_ids=event_ids,
            events=tuple(selected),
            text="\n".join(lines),
            input_hash=input_hash,
            truncated=truncated,
            stats={
                "events": len(selected),
                "source_events": len(raw_events),
                "characters": len("\n".join(lines)),
            },
        )
