"""Convert a bounded Codex task export into canonical coding events.

The adapter accepts the JSON shape returned by the Codex app's ``read_thread``
surface as well as a smaller portable ``threads`` export.  It keeps visible
user/assistant text and file/tool evidence, deliberately omitting reasoning
items so hidden chain-of-thought is never copied into the memory store.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..domain.events import EventEnvelope, EventType
from ..ingest.service import IngestService
from ..storage.repository import ConflictError


_DETERMINISTIC_FALLBACK_TIME = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Codex Desktop exports can contain host-injected context blocks in an
# otherwise visible user turn (for example the plugin catalog and runtime
# metadata).  They are transport instructions, not coding evidence.  Strip
# only the well-known tagged blocks before they reach the canonical event
# store; ordinary user text and assistant answers remain untouched.
_HOST_CONTEXT_BLOCK_RE = re.compile(
    r"<(?P<tag>recommended_plugins|environment_context|skills_instructions|"
    r"permissions instructions|app-context|plugins_instructions|multi_agent_mode|memory|"
    r"oai-mem-citation)>.*?</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_HIDDEN_REASONING_BLOCK_RE = re.compile(
    r"<(?:analysis|thinking|reasoning)>.*?</(?:analysis|thinking|reasoning)>",
    re.IGNORECASE | re.DOTALL,
)
_HIDDEN_REASONING_OPEN_RE = re.compile(
    r"<(?:analysis|thinking|reasoning)>.*$",
    re.IGNORECASE | re.DOTALL,
)
_HOST_CONTEXT_OPEN_RE = re.compile(
    r"<(?:recommended_plugins|environment_context|skills_instructions|"
    r"permissions instructions|app-context|plugins_instructions|multi_agent_mode|"
    r"memory|oai-mem-citation)>.*$",
    re.IGNORECASE | re.DOTALL,
)
_SYNTHETIC_LINE_OPEN_RE = re.compile(
    r"(?im)^\s*#\s*(?:AGENTS\.md instructions|Files mentioned by the user\s*:).*$",
)
_REQUEST_HEADING_RE = re.compile(
    r"(?im)^\s*#{1,6}\s*my request\s*:?\s*",
)
_INSTRUCTION_ONLY_RE = re.compile(
    r"(?is)^\s*(?:#\s*AGENTS\.md instructions\b|"
    r"#\s*Files mentioned by the user\s*:\s*|<skill\b)",
)


@dataclass(frozen=True)
class HistoryImportSummary:
    path: str
    threads: int
    events: int
    accepted: int
    duplicates: int
    conflicts: int
    tasks: tuple[str, ...]
    source_completeness: dict[str, str]
    skipped: int = 0
    invalid_threads: int = 0
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CodexHistoryImporter:
    """Build deterministic envelopes from selected Codex history JSON."""

    adapter_name = "codex-history"
    adapter_version = "0.2"

    def __init__(self, service: IngestService, *, project_id: str | None = None):
        self.service = service
        self.project_id = project_id

    @staticmethod
    def load(path: str | Path) -> list[dict[str, Any]]:
        file_path = Path(path)
        raw = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            candidates = raw
        elif isinstance(raw, dict) and isinstance(raw.get("threads"), list):
            candidates = raw["threads"]
        elif isinstance(raw, dict) and isinstance(raw.get("thread"), dict):
            candidates = [raw]
        elif isinstance(raw, dict):
            # A single compact thread may put metadata at the top level.
            candidates = [raw]
        else:
            raise ValueError("Codex history JSON must be a thread object or a threads list")
        result = [item for item in candidates if isinstance(item, dict)]
        if not result:
            raise ValueError("Codex history JSON contains no thread objects")
        return result

    def import_file(
        self,
        path: str | Path,
        *,
        atomic: bool = False,
        batch_size: int = 250,
        max_threads: int | None = None,
        since: datetime | None = None,
    ) -> HistoryImportSummary:
        """Import a history export in bounded transactions.

        The original Phase 2A implementation assembled every thread before
        writing it.  A complete local Codex export can contain thousands of
        threads, so the default path now processes one thread at a time and
        flushes event chunks through ``IngestService.ingest_many``.  ``atomic``
        retains the all-or-nothing behavior for small fixtures and migrations.
        """

        batch_size = max(1, min(int(batch_size), 5000))
        threads = self.load(path)
        completeness: dict[str, str] = {}
        task_ids: list[str] = []
        selected = 0
        generated_events = 0
        accepted = 0
        duplicates = 0
        conflicts = 0
        skipped = 0
        invalid_threads = 0
        errors: list[str] = []
        all_events: list[EventEnvelope] = []
        max_selected = None if max_threads is None else max(0, int(max_threads))

        def include_thread(thread_payload: dict[str, Any]) -> bool:
            nonlocal skipped
            thread = (
                thread_payload.get("thread")
                if isinstance(thread_payload.get("thread"), dict)
                else thread_payload
            )
            updated = _timestamp(
                thread.get("updatedAt")
                or thread.get("updated_at")
                or thread.get("updated_at_ms")
            )
            if since is not None and (updated is None or updated < since):
                skipped += 1
                return False
            if max_selected is not None and selected >= max_selected:
                skipped += 1
                return False
            return True

        def account(result: Any) -> None:
            nonlocal accepted, duplicates
            if result.status == "accepted":
                accepted += 1
            elif result.status == "duplicate":
                duplicates += 1

        def ingest_chunk(chunk: list[EventEnvelope]) -> None:
            nonlocal conflicts
            try:
                results = self.service.ingest_many(chunk, atomic=True)
                for result in results:
                    account(result)
                return
            except ConflictError:
                # A changed identity should not roll back unrelated records in
                # a large export. Retry this bounded chunk individually.
                pass
            for event in chunk:
                try:
                    account(self.service.ingest(event))
                except ConflictError:
                    conflicts += 1
                    errors.append(f"conflict:{event.event_id}")
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}:{event.event_id}:{exc}")

        for thread_payload in threads:
            if not include_thread(thread_payload):
                continue
            try:
                events, task_id, complete = self.thread_events(thread_payload)
            except (TypeError, ValueError, KeyError) as exc:
                invalid_threads += 1
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            selected += 1
            generated_events += len(events)
            task_ids.append(task_id)
            completeness[task_id] = complete
            if atomic:
                all_events.extend(events)
                continue
            for offset in range(0, len(events), batch_size):
                ingest_chunk(events[offset : offset + batch_size])

        if atomic:
            results = self.service.ingest_many(all_events, atomic=True)
            accepted = sum(result.status == "accepted" for result in results)
            duplicates = sum(result.status == "duplicate" for result in results)

        return HistoryImportSummary(
            path=str(Path(path).resolve()),
            threads=selected,
            events=generated_events,
            accepted=accepted,
            duplicates=duplicates,
            conflicts=conflicts,
            tasks=tuple(task_ids),
            source_completeness=completeness,
            skipped=skipped,
            invalid_threads=invalid_threads,
            errors=tuple(errors[:100]),
        )

    def thread_events(
        self, payload: dict[str, Any]
    ) -> tuple[list[EventEnvelope], str, str]:
        thread = payload.get("thread") if isinstance(payload.get("thread"), dict) else payload
        thread_id = str(thread.get("id") or thread.get("thread_id") or "").strip()
        if not thread_id:
            raise ValueError("Codex thread is missing thread.id")
        host_id = str(thread.get("hostId") or thread.get("host_id") or "local")
        # The history index may copy a platform-generated first-user prompt
        # into ``title``.  It can contain plugin catalogs, runtime blocks, or
        # a very large transcript.  Titles are used as the canonical task
        # label, so apply the same visible-content filter as message text
        # before they reach the entity table.
        title = _bound(
            _sanitize_visible_text(str(thread.get("title") or thread.get("name") or thread_id)).strip(),
            500,
        ).strip() or thread_id
        cwd = str(thread.get("cwd") or "").strip() or None
        created = _timestamp(thread.get("createdAt") or thread.get("created_at") or thread.get("created_at_ms"))
        updated = _timestamp(thread.get("updatedAt") or thread.get("updated_at") or thread.get("updated_at_ms"))
        fallback_time = created or updated or _DETERMINISTIC_FALLBACK_TIME
        explicit_completeness = str(
            payload.get("completeness") or thread.get("completeness") or "partial"
        ).lower()
        completeness = explicit_completeness if explicit_completeness in {"full", "partial", "summary"} else "partial"
        project_id = self.project_id or self._project_id(cwd)
        task_id = f"codex-thread:{thread_id}"
        session_id = f"codex-session:{thread_id}"
        history_paths = _code_paths(thread.get("file_paths"))
        thread_metadata = {
            "codex_source": _bound(str(thread.get("source") or ""), 200) or None,
            "codex_thread_source": _bound(str(thread.get("thread_source") or ""), 200) or None,
            "codex_model": _bound(str(thread.get("model") or ""), 200) or None,
            "codex_model_provider": _bound(str(thread.get("model_provider") or ""), 200) or None,
            "codex_cli_version": _bound(str(thread.get("cli_version") or ""), 100) or None,
            "codex_archived": bool(thread.get("archived")) if thread.get("archived") is not None else None,
            "history_file_paths": history_paths,
        }
        thread_metadata = {key: value for key, value in thread_metadata.items() if value not in (None, [], "")}
        turns = payload.get("turns") if isinstance(payload.get("turns"), list) else []
        # ``read_thread`` returns pages newest-first.  Normalize to the
        # chronological order expected by the canonical ``seq`` contract;
        # compact exports without a page marker keep their supplied order.
        page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
        if str(page.get("order") or "").lower() == "newest_first":
            turns = list(reversed(turns))
        items = list(self._visible_items(turns))
        events: list[EventEnvelope] = []
        previous_id: str | None = None
        seq = 0
        start_id = self._event_id(thread_id, "session-start", title)
        events.append(
            self._event(
                event_id=start_id,
                external_id=f"{host_id}:{thread_id}:session-start",
                event_type=EventType.SESSION_STARTED,
                occurred_at=created or fallback_time,
                project_id=project_id,
                task_id=task_id,
                session_id=session_id,
                seq=seq,
                parent_event_id=None,
                context=self._context(cwd, thread_id, host_id, title, completeness, thread_metadata),
                payload={
                    "text": title,
                    "thread_id": thread_id,
                    "host_id": host_id,
                    "title": title,
                    "history_item_count": len(items),
                    "history_completeness": completeness,
                    # The history-manager index exposes touched file paths
                    # even when the compact export has no structured tool
                    # records.  Keep them as low-confidence context hints;
                    # extraction must not treat them as verified edits.
                    "paths": history_paths,
                },
                artifacts=[],
                completeness=completeness,
            )
        )
        previous_id = start_id
        seq += 1
        for ordinal, item in enumerate(items):
            item["thread_id"] = thread_id
            event_type, event_payload, artifacts = self._item_to_event(item)
            if event_type is None:
                continue
            item_id = str(item.get("id") or f"item-{ordinal}")
            event_id = self._event_id(thread_id, item_id, json.dumps(event_payload, ensure_ascii=False, sort_keys=True))
            event = self._event(
                event_id=event_id,
                external_id=f"{host_id}:{thread_id}:{item_id}",
                event_type=event_type,
                occurred_at=(
                    _timestamp(item.get("timestamp_ms") or item.get("timestamp"))
                    or updated
                    or created
                    or fallback_time
                ),
                project_id=project_id,
                task_id=task_id,
                session_id=session_id,
                seq=seq,
                parent_event_id=previous_id,
                context=self._context(cwd, thread_id, host_id, title, completeness, thread_metadata),
                payload=event_payload,
                artifacts=artifacts,
                completeness=completeness,
            )
            events.append(event)
            previous_id = event_id
            seq += 1
        end_time = updated or created or fallback_time
        end_id = self._event_id(thread_id, "session-end", str(seq))
        events.append(
            self._event(
                event_id=end_id,
                external_id=f"{host_id}:{thread_id}:session-end",
                event_type=EventType.SESSION_ENDED,
                occurred_at=end_time,
                project_id=project_id,
                task_id=task_id,
                session_id=session_id,
                seq=seq,
                parent_event_id=previous_id,
                context=self._context(cwd, thread_id, host_id, title, completeness, thread_metadata),
                payload={"text": "Codex history export ended", "thread_id": thread_id},
                artifacts=[],
                completeness=completeness,
            )
        )
        return events, task_id, completeness

    def _visible_items(self, turns: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        for turn_index, turn in enumerate(turns):
            if not isinstance(turn, dict):
                continue
            turn_id = str(turn.get("id") or turn.get("turn_id") or f"turn-{turn_index}")
            items = turn.get("items")
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_copy = dict(item)
                    item_copy.setdefault("turn_id", turn_id)
                    item_type = str(item_copy.get("type") or item_copy.get("kind") or "").lower()
                    if item_type in {"usermessage", "user_message", "user", "agentmessage", "assistantmessage", "assistant_message", "assistant"}:
                        if not self._item_text(item_copy):
                            continue
                    yield item_copy
            else:
                # Compact history-manager rows already contain role/text.
                if turn.get("role") or turn.get("text"):
                    compact_id = turn.get("item_id") or turn.get("idx") or turn_id
                    role = str(turn.get("role") or "").lower()
                    text = _sanitize_visible_text(str(turn.get("text") or ""))
                    # Synthetic host/instruction rows become empty after
                    # sanitization.  Do not manufacture a blank user/agent
                    # event for them; canonical sequence remains deterministic
                    # for all retained visible records.
                    if role in {"user", "assistant"} and not text:
                        continue
                    yield {
                        # ``idx`` is stable across history-manager exports;
                        # prefer it over the position in a page so replaying
                        # a refreshed index does not manufacture new ids.
                        "id": f"compact-{compact_id}",
                        "turn_id": turn_id,
                        "type": "userMessage" if role == "user" else "agentMessage",
                        "text": text,
                        "timestamp_ms": turn.get("timestamp_ms") or turn.get("timestamp"),
                    }

    def _item_to_event(self, item: dict[str, Any]) -> tuple[EventType | None, dict[str, Any], list[dict[str, Any]]]:
        item_type = str(item.get("type") or item.get("kind") or "").strip()
        item_id = str(item.get("id") or "")
        turn_id = str(item.get("turn_id") or "")
        text = self._item_text(item)
        common: dict[str, Any] = {
            "thread_id": item.get("thread_id"),
            "turn_id": turn_id,
            "item_id": item_id,
            "raw_item_type": item_type,
        }
        if text:
            common["text"] = _bound(text, 40_000)
        if item_type in {"userMessage", "user_message", "user"}:
            return EventType.USER_MESSAGE, common, self._text_artifacts(text)
        if item_type in {"agentMessage", "assistantMessage", "assistant_message", "assistant"}:
            common["phase"] = item.get("phase")
            return EventType.ASSISTANT_MESSAGE, common, self._text_artifacts(text)
        if item_type == "fileChange" or "changes" in item:
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            paths: list[str] = []
            summaries: list[dict[str, Any]] = []
            for change in changes:
                if not isinstance(change, dict):
                    continue
                path = change.get("path") or change.get("file_path")
                if path:
                    paths.append(str(path))
                diff = change.get("diff") if isinstance(change.get("diff"), dict) else {}
                diff_text = diff.get("text") if isinstance(diff, dict) else None
                summaries.append(
                    {
                        "path": path,
                        "kind": (change.get("kind") or {}).get("type") if isinstance(change.get("kind"), dict) else change.get("kind"),
                        "diff_excerpt": _bound(str(diff_text or ""), 6000),
                        "truncated": bool(diff.get("truncated")) if isinstance(diff, dict) else False,
                    }
                )
            common["paths"] = paths
            common["changes"] = summaries
            return EventType.FILE_EDIT, common, [
                {"kind": "source_file", "path": path, "metadata": {"thread_id": item.get("thread_id"), "turn_id": turn_id}}
                for path in paths
            ]
        if item_type in {"commandExecution", "command_execution", "command"}:
            command = item.get("command") or item.get("cmd") or text
            common["command"] = _bound(str(command or ""), 8000)
            event_type = EventType.VALIDATION_RUN if re.search(
                r"(pytest|test|compile|ruff|lint|build|验证|编译)", str(command), re.IGNORECASE
            ) else EventType.COMMAND_RUN
            common["status"] = item.get("status") or item.get("exitCode") or item.get("exit_code")
            return event_type, common, []
        if item_type in {"mcpToolCall", "mcp_tool_call", "toolCall", "tool_call"}:
            common["server"] = item.get("server")
            common["tool"] = item.get("tool") or item.get("name")
            common["arguments"] = _safe_json(item.get("arguments"))
            common["status"] = item.get("status")
            return EventType.TOOL_CALL, common, []
        if item_type in {"toolResult", "tool_result", "commandResult"}:
            common["result"] = _safe_json(item.get("result") or item.get("output") or text)
            common["status"] = item.get("status")
            return EventType.TOOL_RESULT, common, []
        if item_type in {"contextCompaction", "reasoning", "analysis", "thinking"}:
            # Do not persist hidden reasoning.  A count/omission marker is not
            # needed for extraction and would only add noise.
            return None, {}, []
        # Preserve other visible agent records as tool results, but never copy
        # opaque binary blobs or unbounded nested objects.
        if item_type:
            common["metadata"] = {"keys": sorted(str(key) for key in item.keys())[:50]}
            return EventType.TOOL_RESULT, common, []
        return None, {}, []

    @staticmethod
    def _item_text(item: dict[str, Any]) -> str:
        if isinstance(item.get("text"), str):
            return _sanitize_visible_text(item["text"])
        content = item.get("content")
        if isinstance(content, list):
            pieces = [
                _sanitize_visible_text(str(part.get("text", "")))
                for part in content
                if isinstance(part, dict) and part.get("text")
            ]
            return "\n".join(part for part in pieces if part)
        return ""

    @staticmethod
    def _text_artifacts(text: str) -> list[dict[str, Any]]:
        paths = sorted(
            set(
                re.findall(
                    r"(?<![\w])(?:[A-Za-z]:[\\/])?[^\s`\"'<>|]+\.(?:cs|py|ts|tsx|js|jsx|go|rs|java|cpp|c|h|hpp|json|yaml|yml|sql|prefab|asset|shader|asmdef|md|xlsx)(?=$|[\s`\"'<>|),.;:，。；：、])",
                    text,
                    re.IGNORECASE,
                )
            )
        )
        return [{"kind": "mentioned_file", "path": path} for path in paths[:50]]

    def _event(
        self,
        *,
        event_id: str,
        external_id: str,
        event_type: EventType,
        occurred_at: datetime,
        project_id: str,
        task_id: str,
        session_id: str,
        seq: int,
        parent_event_id: str | None,
        context: dict[str, Any],
        payload: dict[str, Any],
        artifacts: list[dict[str, Any]],
        completeness: str,
    ) -> EventEnvelope:
        # Payloads can contain nested tool arguments or diff excerpts.  They
        # are still visible evidence, but known host-injected tagged blocks
        # should not become coding facts merely because they were nested in a
        # JSON value.  Recursive sanitization is deterministic and leaves
        # ordinary source text untouched.
        payload = _sanitize_visible_value(payload)
        artifacts = _sanitize_visible_value(artifacts)
        return EventEnvelope.model_validate(
            {
                "schema_version": "codememory.event.v1",
                "event_id": event_id,
                "external_event_id": external_id,
                "event_type": event_type.value,
                "occurred_at": occurred_at.isoformat(),
                "producer": {
                    "agent_id": "codex-local",
                    "adapter": self.adapter_name,
                    "adapter_version": self.adapter_version,
                },
                "project_id": project_id,
                "task_id": task_id,
                "session_id": session_id,
                "seq": seq,
                "parent_event_id": parent_event_id,
                "context": context,
                "payload": payload,
                "artifacts": artifacts,
                "source": "replay",
                "completeness": completeness,
            }
        )

    @staticmethod
    def _event_id(thread_id: str, item_id: str, content: str) -> str:
        digest = hashlib.sha256(f"{thread_id}\n{item_id}\n{content}".encode("utf-8")).hexdigest()[:28]
        return f"codex-event-{digest}"

    @staticmethod
    def _project_id(cwd: str | None) -> str:
        normalized = (cwd or "unknown").replace("\\", "/").lower()
        return f"codex-project-{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def _context(
        cwd: str | None,
        thread_id: str,
        host_id: str,
        title: str,
        completeness: str,
        thread_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = {
            "root_path": cwd,
            "cwd": cwd,
            "branch": None,
            "codex_thread_id": thread_id,
            "codex_host_id": host_id,
            "codex_title": title,
            "history_completeness": completeness,
        }
        if thread_metadata:
            context.update(thread_metadata)
        return context


def _timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                return None
    return None


def _bound(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "\n[…truncated by history adapter…]"


def _safe_json(value: Any) -> Any:
    if value is None:
        return None
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return _bound(str(value), 4000)
    if isinstance(value, str):
        return _bound(value, 20_000)
    return value


def _sanitize_visible_value(value: Any) -> Any:
    """Remove known host/reasoning blocks from nested visible values."""

    if isinstance(value, str):
        return _sanitize_visible_text(value)
    if isinstance(value, list):
        return [_sanitize_visible_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_visible_value(item) for key, item in value.items()}
    return value


_CODE_PATH_RE = re.compile(
    r"\.(?:cs|py|ts|tsx|js|jsx|go|rs|java|cpp|c|h|hpp|json|yaml|yml|sql|prefab|asset|shader|asmdef|md)$",
    re.IGNORECASE,
)


def _code_paths(values: Any, *, limit: int = 100) -> list[str]:
    """Normalize the history index's touched-file hints without guessing edits."""

    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        path = str(value or "").strip().replace("\\", "/")
        if not path or not _CODE_PATH_RE.search(path):
            continue
        if path not in result:
            result.append(path)
        if len(result) >= limit:
            break
    return result


def _sanitize_visible_text(value: str) -> str:
    """Remove transport instructions while retaining the user's request.

    Codex history indexes sometimes materialize workspace ``AGENTS.md`` text,
    skill documents, or an attachment preamble as a synthetic *user* turn.
    Treating those records as conversation causes a coding extractor to learn
    platform instructions as if they were code decisions.  ``Files mentioned``
    records are slightly different: they contain a real request after the
    preamble, so only that tail is retained.  The function is intentionally
    conservative for ordinary text and is also used by the extraction layer
    when replaying an older, already-imported event store.
    """

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    stripped = text.lstrip()
    if _INSTRUCTION_ONLY_RE.match(stripped):
        # Attachment metadata is followed by the actual user prompt.  Keep
        # that prompt, but drop the synthetic file list and its instructions.
        request_match = _REQUEST_HEADING_RE.search(stripped)
        if request_match:
            text = stripped[request_match.end() :]
        else:
            return ""

    # Remove complete tagged blocks first, then cut an opening tag whose
    # closing marker was lost by a compact/truncated history export.
    text = _HOST_CONTEXT_BLOCK_RE.sub("", text)
    text = _HIDDEN_REASONING_BLOCK_RE.sub("", text)
    text = _HOST_CONTEXT_OPEN_RE.sub("", text)
    text = _HIDDEN_REASONING_OPEN_RE.sub("", text)
    # The same synthetic markers can be appended after real prose in a
    # compacted turn.  Cut from the marker to the end without disturbing the
    # preceding request.
    marker = _SYNTHETIC_LINE_OPEN_RE.search(text)
    if marker:
        text = text[: marker.start()]

    # A few exports put a standalone skill invocation on a line after the
    # request.  It is routing metadata rather than a coding fact; remove only
    # the line, leaving surrounding user prose intact.
    text = re.sub(r"\[\$[^\]\r\n]+\]\([^\r\n]*\)", "", text)
    return text.strip()
