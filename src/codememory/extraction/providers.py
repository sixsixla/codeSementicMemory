"""Replaceable extraction providers.

``MockLLMProvider`` is deliberately deterministic.  It is useful for local
development, fixtures, and an offline first run; a real provider can implement
the same protocol later without changing storage or validation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Protocol

from .context import AssembledContext
from ..history.codex import _sanitize_visible_text as _sanitize_history_text
from .models import (
    Binding,
    BindingRole,
    Candidate,
    CandidateKind,
    ExtractionBatch,
    ExtractorInfo,
    LifecycleHint,
)


class ExtractionProvider(Protocol):
    provider_name: str
    model_name: str
    prompt_version: str

    def extract(self, context: AssembledContext) -> ExtractionBatch | dict[str, Any]:
        """Return strict-compatible candidate JSON for one assembled window."""


class ProviderError(RuntimeError):
    """A provider could not return a usable response."""


_PATH_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/])?[^\s`\"'<>|()\[\]，。；：、]+?\.(?:cs|py|ts|tsx|js|jsx|go|rs|java|cpp|c|h|hpp|json|yaml|yml|sql|prefab|asset|shader|asmdef|md|xlsx)(?=$|[\s`\"'<>|()\[\]),.;:，。；：、])",
    re.IGNORECASE,
)
_SYMBOL_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9_]*(?:Component|Manager|Service|Controller|Panel|Provider|Resolver|Checker|Data|System|Skill|Question|Host|Presenter)?\b"
)
_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff][\w\u4e00-\u9fff-]{1,}", re.UNICODE)
_CODING_TERMS = re.compile(
    r"(?i)(?:\b(?:code|coding|class|method|function|file|repo|repository|git|p4|unity|wwise|"
    r"compile|build|test|bug|fix|debug|refactor|mcp|vsg|prefab|shader|api|interface)\b|"
    r"代码|源码|文件|工程|项目|功能|实现|修改|修复|排查|定位|编译|构建|测试|验证|断点|"
    r"配置|流程图|脚本|组件|模块|接口|逻辑|玩法|性能|重构|导出|提交|分支)")
_FAILURE_TERMS = re.compile(
    r"(?i)(?:\b(?:bug|error|exception|failed|failure|broken|regression|crash)\b|"
    r"错误|失败|报错|异常|未生效|不起作用|不工作|崩溃|根因|问题在|问题是|排查)")
_DECISION_TERMS = re.compile(
    r"(?i)(?:\b(?:must|should|shall|do not|don't|avoid|prefer|use .* instead|decision)\b|"
    r"结论|原则|规则|必须|应当|应该|不要|不能|不应|只能|保留|复用|采用|统一|区分|"
    r"不需要|无需|建议|约束|边界)")
_NOISE_ONLY = re.compile(
    r"(?i)^(?:the following is the codex agent history|[0-9a-f]{20,}|#?\s*files mentioned by the user:?)")
_ORCHESTRATION_PROMPT_RE = re.compile(
    r"(?is)^\s*(?:role\s*:|goal\s*:|task\s*:\s*(?:validate|investigate)|"
    r"project[_ ]j\s+primary\s+domain|you are an?\s+(?:investigator|subagent)|"
    r"act as the delivery lead|\{\s*[\"']?(?:assignment_id|agent_run_id|verdict|findings)\b)"
)
_LOW_INFORMATION_INTENT_RE = re.compile(
    r"(?i)^\s*(?:同意|好的?|可以|收到|明白|确认|继续|执行修改|按(?:上面|最终|结论|计划).{0,40}(?:执行|修改)|"
    r"continue|proceed|approved?)\s*[。.!！]?\s*$"
)
_AUXILIARY_PATH_RE = re.compile(
    r"(?i)(?:^|/)(?:agents\.md|skill\.md)$|(?:^|/)(?:\.codex|codex-skills|recommended_plugins)(?:/|$)"
)


def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"cand-{digest}"


def _event_text(event: dict[str, Any]) -> str:
    """Return visible, non-host text for both new and legacy event rows."""

    return _sanitize_history_text(str(event.get("text") or ""))


def _is_auxiliary_path(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1].casefold()
    return bool(
        basename in {"agents.md", "skill.md"}
        or _AUXILIARY_PATH_RE.search(normalized)
    )


def _normalize_path_hint(value: Any) -> str:
    """Trim prose/punctuation accidentally captured around a source path."""

    path = str(value or "").strip().strip("`'\"(),.;:，。；：、")
    # A compact user sentence can place a path immediately after Chinese
    # punctuation (``功能，Assets/...``).  Keep the path portion instead of
    # turning the whole sentence fragment into a non-resolvable binding.
    match = re.search(
        r"(?i)(?:[A-Za-z]:[\\/]|(?:assets|src|tests|game_client|project_j|packages)[\\/])"
        r"[^\s`\"'<>|()\[\]，。；：、]+?\.(?:cs|py|ts|tsx|js|jsx|go|rs|java|cpp|c|h|hpp|json|yaml|yml|sql|prefab|asset|shader|asmdef|md|xlsx)(?=$|[\s`\"'<>|()\[\]),.;:，。；：、])",
        path,
    )
    if match:
        path = match.group(0)
    path = re.sub(r":\d+(?=$|[^\d])", "", path)
    path = path.rstrip("`'\"(),.;:，。；：、）】")
    return path


class MockLLMProvider:
    """A transparent heuristic that emits auditable route observations.

    It intentionally uses only visible user/assistant/file/validation text from
    the assembled context.  Reasoning summaries and hidden chain-of-thought are
    never read or persisted.
    """

    provider_name = "mock"
    model_name = "heuristic-v1"
    prompt_version = "coding-extraction-v6"

    def _legacy_extract(self, context: AssembledContext) -> ExtractionBatch:
        user_events = [e for e in context.events if e.get("event_type") == "user_message"]
        assistant_events = [e for e in context.events if e.get("event_type") == "assistant_message"]
        file_events = [
            e
            for e in context.events
            if e.get("event_type") in {"file_read", "file_edit", "vcs_change"}
        ]
        validation_events = [
            e
            for e in context.events
            if e.get("event_type") in {"validation_run", "command_run"}
        ]
        visible_text = "\n".join(str(e.get("text") or "") for e in context.events)
        intent = _short(
            str((user_events[0] if user_events else context.events[0]).get("text") or "coding task"),
            320,
        )
        paths = self._paths(context.events)
        symbols = self._symbols(visible_text)
        evidence = list(context.event_ids)
        candidates: list[Candidate] = []

        if user_events or paths:
            route_evidence = self._ids(user_events + file_events + validation_events, evidence)
            bindings: list[Binding] = []
            for index, path in enumerate(paths[:30]):
                path_events = self._ids(
                    [e for e in file_events if path in self._paths([e])], route_evidence
                )
                if not path_events:
                    path_events = route_evidence[:1]
                role = BindingRole.MODIFIED_FILE if any(
                    e.get("event_type") == "file_edit" and path in self._paths([e])
                    for e in file_events
                ) else BindingRole.FEATURE_INTEGRATION
                bindings.append(
                    Binding(
                        role=role,
                        path=path,
                        symbol=self._stem(path),
                        evidence=path_events,
                    )
                )
            for symbol in symbols[:20]:
                symbol_events = self._ids(
                    [e for e in context.events if symbol in str(e.get("text") or "")],
                    route_evidence,
                )
                if symbol_events:
                    bindings.append(
                        Binding(
                            role=BindingRole.SUPPORTING_SYMBOL,
                            symbol=symbol,
                            qualified_symbol=symbol,
                            evidence=symbol_events,
                        )
                    )
            confidence = 0.78 if self._has_success(validation_events, assistant_events) else 0.58
            uncertainty = (
                "Heuristic observation; validation evidence was not explicit."
                if confidence < 0.7
                else "Heuristic observation backed by a visible validation/success signal."
            )
            aliases = self._aliases(intent, paths, symbols)
            candidates.append(
                Candidate(
                    candidate_id=_stable_id(context.task_id, context.input_hash, "route", intent),
                    kind=CandidateKind.ROUTE_OBSERVATION,
                    statement=f"Coding route observed: {intent}",
                    aliases=aliases,
                    bindings=bindings,
                    evidence_event_ids=route_evidence or evidence[:1],
                    confidence=confidence,
                    uncertainty=uncertainty,
                    lifecycle_hint=LifecycleHint.CANDIDATE,
                    relation_hints=[
                        {"relation": "observed_in_task", "target_type": "task", "target_id": context.task_id}
                    ],
                )
            )

        decision_text = self._decision_text(user_events, assistant_events)
        if decision_text:
            decision_evidence = self._ids(user_events + assistant_events, evidence)
            candidates.append(
                Candidate(
                    candidate_id=_stable_id(context.task_id, context.input_hash, "decision", decision_text),
                    kind=CandidateKind.DECISION,
                    statement=_short(decision_text, 1000),
                    aliases=self._aliases(decision_text, paths, symbols),
                    bindings=[],
                    evidence_event_ids=decision_evidence or evidence[:1],
                    confidence=0.54 if context.truncated else 0.66,
                    uncertainty="Extracted from natural-language discussion; source code confirmation is pending.",
                    lifecycle_hint=LifecycleHint.CANDIDATE,
                )
            )

        failure_text = self._failure_text(user_events, assistant_events)
        if failure_text:
            failure_evidence = self._ids(user_events + assistant_events, evidence)
            candidates.append(
                Candidate(
                    candidate_id=_stable_id(context.task_id, context.input_hash, "failure", failure_text),
                    kind=CandidateKind.FAILURE,
                    statement=_short(f"Observed issue or rejected path: {failure_text}", 1200),
                    aliases=self._aliases(failure_text, paths, symbols),
                    evidence_event_ids=failure_evidence or evidence[:1],
                    confidence=0.63,
                    uncertainty="The conversation reports a problem; resolution status may require a later validation event.",
                    lifecycle_hint=LifecycleHint.OBSERVED,
                )
            )

        if validation_events:
            validation_evidence = self._ids(validation_events + assistant_events, evidence)
            result = "validation activity was recorded"
            if self._has_success(validation_events, assistant_events):
                result = "a validation or compile success signal was recorded"
            candidates.append(
                Candidate(
                    candidate_id=_stable_id(context.task_id, context.input_hash, "validation", result),
                    kind=CandidateKind.VALIDATION,
                    statement=f"Coding outcome: {result}.",
                    aliases=["编译通过", "validation", "compile"],
                    evidence_event_ids=validation_evidence or evidence[:1],
                    confidence=0.7 if self._has_success(validation_events, assistant_events) else 0.45,
                    uncertainty="Validation evidence is retained as an observation, not a proof of production behavior.",
                    lifecycle_hint=LifecycleHint.OBSERVED,
                )
            )

        # A context with no extractable signal still gets a transparent
        # decision candidate, making the run inspectable rather than silently
        # disappearing.
        if not candidates:
            candidates.append(
                Candidate(
                    candidate_id=_stable_id(context.task_id, context.input_hash, "unclassified"),
                    kind=CandidateKind.DECISION,
                    statement=f"Unclassified coding context: {intent}",
                    aliases=self._aliases(intent, paths, symbols),
                    evidence_event_ids=evidence[:1],
                    confidence=0.25,
                    uncertainty="No route, decision, failure, or validation signal was confidently detected.",
                    lifecycle_hint=LifecycleHint.OBSERVED,
                )
            )

        return ExtractionBatch(
            extraction_run_id=context.extraction_run_id or _stable_id(context.task_id, context.input_hash, "run"),
            project_id=context.project_id,
            task_id=context.task_id,
            session_id=context.session_id,
            source_event_ids=evidence,
            extractor=ExtractorInfo(
                provider=self.provider_name,
                model=self.model_name,
                prompt_version=self.prompt_version,
            ),
            candidates=candidates,
        )

    @staticmethod
    def _ids(events: list[dict[str, Any]], allowed: list[str] | tuple[str, ...]) -> list[str]:
        allowed_set = set(allowed)
        result: list[str] = []
        for event in events:
            event_id = str(event.get("event_id") or "")
            if event_id in allowed_set and event_id not in result:
                result.append(event_id)
        return result

    @staticmethod
    def _paths(events: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> list[str]:
        result: list[str] = []
        for event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            values: list[Any] = []
            # Session-start ``paths`` are history-index hints and often
            # contain AGENTS/skill documents.  They are useful provenance but
            # are not evidence that the coding task touched those files.
            if event.get("event_type") != "session_started":
                for key in ("path", "file_path", "file", "paths", "files"):
                    value = payload.get(key)
                    values.extend(value if isinstance(value, list) else [value] if value else [])
                values.extend(event.get("paths") or [])
            text = _event_text(event)
            values.extend(_PATH_RE.findall(text))
            for value in values:
                path = _normalize_path_hint(value).replace("\\\\", "\\")
                if (
                    path
                    and not _is_auxiliary_path(path)
                    and ("." in path or "\\" in path or "/" in path)
                    and path not in result
                ):
                    result.append(path)
        return result

    @staticmethod
    def _stem(path: str) -> str:
        name = re.split(r"[\\/]", path)[-1]
        return re.sub(r"\.[^.]+$", "", name)

    @staticmethod
    def _symbols(text: str) -> list[str]:
        result: list[str] = []
        stop_words = {
            "P4Workspace",
            "Project_J",
            "Assets",
            "Script",
            "Game",
            "Common",
            "Modules",
            "Server",
            "Client",
            "Panel",
            "Prefab",
            "Existing",
            "Current",
            "Apply",
            "BUFF",
            "NPC",
            "QA",
            "HTTP",
            "JSON",
            "SQLite",
            "Three",
            "Codex",
            "OpenAI",
            "PowerShell",
            "Python",
        }
        for symbol in _SYMBOL_RE.findall(text):
            has_code_shape = (
                symbol not in stop_words
                and (
                    "_" in symbol
                    or re.search(r"[a-z][A-Z]", symbol) is not None
                    or re.search(r"(Component|Manager|Service|Controller|Panel|Provider|Resolver|Checker|Data|System|Skill|Question|Host|Presenter)$", symbol)
                    is not None
                )
            )
            if len(symbol) > 2 and has_code_shape and symbol not in result:
                result.append(symbol)
        return result

    @staticmethod
    def _aliases(intent: str, paths: list[str], symbols: list[str]) -> list[str]:
        result: list[str] = []
        for value in [intent, *symbols[:8], *[MockLLMProvider._stem(p) for p in paths[:8]]]:
            value = _short(value, 120)
            if value and value not in result:
                result.append(value)
        # Add a few meaningful natural-language tokens, but avoid a bag of
        # stop words that would pollute future retrieval.
        for token in _TOKEN_RE.findall(intent):
            if len(token) >= 2 and token not in result and token.lower() not in {"the", "and", "with", "this", "that"}:
                result.append(token)
            if len(result) >= 20:
                break
        return result[:20]

    @staticmethod
    def _has_success(events: list[dict[str, Any]], assistants: list[dict[str, Any]]) -> bool:
        text = " ".join(_event_text(e) for e in [*events, *assistants]).lower()
        return bool(
            re.search(
                r"\b(pass|passed|success|successful|ok|exitcode.?0|exit.?code.?0)\b|编译通过|验证通过|测试通过",
                text,
            )
        )

    @staticmethod
    def _decision_text(users: list[dict[str, Any]], assistants: list[dict[str, Any]]) -> str:
        candidates = [_event_text(e) for e in [*users, *assistants]]
        for text in candidates:
            for sentence in re.split(r"(?<=[。！？.!?])\s+|\n+", text):
                sentence = re.sub(r"\s+", " ", sentence).strip(" -*#`")
                if len(sentence) >= 12 and _DECISION_TERMS.search(sentence):
                    return _short(sentence, 1000)
        return ""

    @staticmethod
    def _failure_text(users: list[dict[str, Any]], assistants: list[dict[str, Any]]) -> str:
        for event in [*users, *assistants]:
            text = _event_text(event)
            for sentence in re.split(r"(?<=[。！？.!?])\s+|\n+", text):
                sentence = re.sub(r"\s+", " ", sentence).strip(" -*#`")
                if len(sentence) >= 10 and _FAILURE_TERMS.search(sentence):
                    return _short(sentence, 1100)
        return ""

    # ------------------------------------------------------------------
    # Coding-focused v2 extraction path.  The v1 implementation above is
    # retained as a readable compatibility reference, while this method is
    # the public ``extract`` entry point used by new runs.
    # ------------------------------------------------------------------

    def extract(self, context: AssembledContext) -> ExtractionBatch:
        # Re-sanitize event text here as well as at replay time.  This keeps
        # older canonical stores safe when the importer policy is tightened
        # after they were first ingested.
        events = [
            {**event, "text": _event_text(event)}
            for event in context.events
            if not (
                event.get("event_type") in {"user_message", "assistant_message"}
                and not _event_text(event)
            )
        ]
        user_events = [event for event in events if event.get("event_type") == "user_message"]
        assistant_events = [
            event for event in events if event.get("event_type") == "assistant_message"
        ]
        file_events = [
            event
            for event in events
            if event.get("event_type") in {"file_read", "file_edit", "vcs_change"}
        ]
        edit_events = [
            event for event in events if event.get("event_type") in {"file_edit", "vcs_change"}
        ]
        validation_events = [
            event for event in events if event.get("event_type") in {"validation_run", "command_run"}
        ]
        visible_text = "\n".join(_event_text(event) for event in events)
        intent = self._intent(events, user_events)
        paths = self._paths(events)
        modified_paths = self._paths(edit_events)
        symbols = self._symbols("\n".join([intent, visible_text]))
        evidence = [str(event.get("event_id")) for event in events if event.get("event_id")]
        # The strict batch schema requires at least one source id even when a
        # task contains only a metadata-only message.  Keep the canonical id
        # as an auditable empty run, but do not derive a candidate from it.
        if not evidence:
            evidence = [str(event_id) for event_id in context.event_ids if event_id]
        candidates: list[Candidate] = []

        # Complete local history contains many empty/admin threads.  An empty
        # candidate list is a valid, replayable result and is preferable to a
        # durable “unclassified” memory that can never route a coding task.
        visible_signal = any(_event_text(event).strip() for event in events)
        coding_signal = bool(
            paths
            or file_events
            or validation_events
            or (
                visible_signal
                and bool(intent)
                and (_CODING_TERMS.search(intent) or _CODING_TERMS.search(visible_text))
            )
        ) and not _NOISE_ONLY.match(intent.strip())

        if coding_signal:
            route_evidence = self._ids(
                [*user_events, *file_events, *validation_events], evidence
            ) or evidence[:1]
            bindings = self._bindings(
                events, paths, modified_paths, symbols, route_evidence
            )
            successful = self._has_success(validation_events, assistant_events)
            if successful:
                confidence = 0.84 if edit_events else 0.76
            elif edit_events:
                confidence = 0.70
            elif paths:
                confidence = 0.63
            else:
                confidence = 0.52
            uncertainty = (
                "Heuristic route observation backed by visible validation/success evidence."
                if successful
                else "Heuristic observation; source bindings and validation status require verification."
            )
            candidates.append(
                Candidate(
                    candidate_id=_stable_id(context.task_id, context.input_hash, "route", intent),
                    kind=CandidateKind.ROUTE_OBSERVATION,
                    statement=f"Coding route observed: {intent}",
                    aliases=self._aliases(intent, paths, symbols),
                    bindings=bindings,
                    evidence_event_ids=route_evidence,
                    confidence=confidence,
                    uncertainty=uncertainty,
                    lifecycle_hint=LifecycleHint.CANDIDATE,
                    relation_hints=[
                        {
                            "relation": "observed_in_task",
                            "target_type": "task",
                            "target_id": context.task_id,
                        }
                    ],
                )
            )

            decision_text = self._decision_text(user_events, assistant_events)
            if decision_text:
                decision_evidence = self._ids(
                    [*user_events, *assistant_events], evidence
                ) or evidence[:1]
                candidates.append(
                    Candidate(
                        candidate_id=_stable_id(
                            context.task_id, context.input_hash, "decision", decision_text
                        ),
                        kind=CandidateKind.DECISION,
                        statement=_short(decision_text, 1000),
                        aliases=self._aliases(decision_text, paths, symbols),
                        bindings=[],
                        evidence_event_ids=decision_evidence,
                        confidence=0.54 if context.truncated else 0.66,
                        uncertainty="Natural-language decision proposal; source-code confirmation is pending.",
                        lifecycle_hint=LifecycleHint.CANDIDATE,
                    )
                )

                # Negative constraints are valuable anti-bindings: they keep a
                # future coding agent from repeating a rejected edit path.
                if re.search(r"(?:不要|不能|不应|避免|do not|don't|avoid|never)", decision_text, re.I):
                    candidates.append(
                        Candidate(
                            candidate_id=_stable_id(
                                context.task_id, context.input_hash, "anti", decision_text
                            ),
                            kind=CandidateKind.ANTI_BINDING,
                            statement=_short(
                                f"Rejected or forbidden coding path: {decision_text}", 1200
                            ),
                            aliases=self._aliases(decision_text, paths, symbols),
                            bindings=self._bindings(
                                events, paths, modified_paths, symbols, decision_evidence
                            ),
                            evidence_event_ids=decision_evidence,
                            confidence=0.64,
                            uncertainty="Negative constraint was stated in visible discussion; applicability may be local to this task.",
                            lifecycle_hint=LifecycleHint.OBSERVED,
                        )
                    )

            failure_text = self._failure_text(user_events, assistant_events)
            if failure_text:
                failure_evidence = self._ids(
                    [*user_events, *assistant_events], evidence
                ) or evidence[:1]
                candidates.append(
                    Candidate(
                        candidate_id=_stable_id(
                            context.task_id, context.input_hash, "failure", failure_text
                        ),
                        kind=CandidateKind.FAILURE,
                        statement=_short(
                            f"Observed issue or rejected path: {failure_text}", 1200
                        ),
                        aliases=self._aliases(failure_text, paths, symbols),
                        bindings=[],
                        evidence_event_ids=failure_evidence,
                        confidence=0.63,
                        uncertainty="The conversation reports a problem; resolution status requires a later validation or review signal.",
                        lifecycle_hint=LifecycleHint.OBSERVED,
                    )
                )

            # Only actual command/validation records produce an outcome card;
            # a sentence saying “it should compile” is not execution evidence.
            if validation_events:
                validation_evidence = self._ids(
                    [*validation_events, *assistant_events], evidence
                ) or evidence[:1]
                result = self._validation_summary(validation_events, assistant_events)
                candidates.append(
                    Candidate(
                        candidate_id=_stable_id(
                            context.task_id, context.input_hash, "validation", result
                        ),
                        kind=CandidateKind.VALIDATION,
                        statement=f"Coding outcome: {result}.",
                        aliases=["编译通过", "validation", "compile", "test"],
                        evidence_event_ids=validation_evidence,
                        confidence=0.78
                        if self._has_success(validation_events, assistant_events)
                        else 0.46,
                        uncertainty="Validation is an observed signal, not proof of production behavior or multiplayer/runtime correctness.",
                        lifecycle_hint=LifecycleHint.OBSERVED,
                    )
                )

        return ExtractionBatch(
            extraction_run_id=context.extraction_run_id
            or _stable_id(context.task_id, context.input_hash, "run"),
            project_id=context.project_id,
            task_id=context.task_id,
            session_id=context.session_id,
            source_event_ids=evidence,
            extractor=ExtractorInfo(
                provider=self.provider_name,
                model=self.model_name,
                prompt_version=self.prompt_version,
            ),
            candidates=candidates,
        )

    @classmethod
    def _intent(cls, events: list[dict[str, Any]], users: list[dict[str, Any]]) -> str:
        """Choose a compact user-language intent instead of a whole transcript."""

        fallback: list[str] = []
        for _index, event in enumerate(users):
            text = re.sub(r"\s+", " ", _event_text(event)).strip()
            if len(text) < 8 or _NOISE_ONLY.match(text):
                continue
            # Agent orchestration prompts are visible in some history
            # exports, but they describe the worker role rather than the
            # user's coding intent.  Prefer the first substantive user
            # request and only fall back to these rows when no real request is
            # available.
            if not _ORCHESTRATION_PROMPT_RE.match(text):
                if _LOW_INFORMATION_INTENT_RE.match(text) and not _PATH_RE.search(text):
                    fallback.append(text)
                    continue
                return _short(text, 420)
        for event in events:
            if event.get("event_type") == "session_started":
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                value = _event_text({**event, "text": payload.get("title") or _event_text(event)})
                if value.strip() and not _LOW_INFORMATION_INTENT_RE.match(value.strip()):
                    return _short(re.sub(r"\s+", " ", value).strip(), 420)
        if fallback:
            return _short(fallback[0], 420)
        for event in events:
            value = _event_text(event).strip()
            if value and not _ORCHESTRATION_PROMPT_RE.match(value):
                return _short(re.sub(r"\s+", " ", value), 420)
        return ""

    @classmethod
    def _bindings(
        cls,
        events: list[dict[str, Any]],
        paths: list[str],
        modified_paths: list[str],
        symbols: list[str],
        fallback_evidence: list[str],
    ) -> list[Binding]:
        result: list[Binding] = []
        modified = set(modified_paths)
        for path in paths[:30]:
            path_events = cls._ids(
                [
                    event
                    for event in events
                    if event.get("event_type") != "session_started"
                    and path in cls._paths([event])
                ],
                fallback_evidence,
            )
            if not path_events:
                # A path that exists only in the history index's session-start
                # hints is not a sufficiently grounded code binding.
                continue
            role = BindingRole.MODIFIED_FILE if path in modified else BindingRole.FEATURE_INTEGRATION
            result.append(
                Binding(role=role, path=path, symbol=cls._stem(path), evidence=path_events)
            )
        for symbol in symbols[:20]:
            symbol_events = cls._ids(
                [event for event in events if symbol in _event_text(event)],
                fallback_evidence,
            )
            if symbol_events and not any(item.symbol == symbol for item in result):
                result.append(
                    Binding(
                        role=BindingRole.SUPPORTING_SYMBOL,
                        symbol=symbol,
                        qualified_symbol=symbol,
                        evidence=symbol_events,
                    )
                )
        return result[:50]

    @staticmethod
    def _validation_summary(
        events: list[dict[str, Any]], assistants: list[dict[str, Any]]
    ) -> str:
        commands: list[str] = []
        for event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            command = payload.get("command") or payload.get("text") or payload.get("message")
            if command:
                commands.append(re.sub(r"\s+", " ", str(command)).strip())
        if MockLLMProvider._has_success(events, assistants):
            outcome = "validation or compile success was recorded"
        else:
            outcome = "validation activity was recorded without an explicit success signal"
        if commands:
            return _short(f"{outcome} ({commands[-1]})", 900)
        return outcome


class OpenAICompatibleProvider:
    """Small dependency-free provider for OpenAI-compatible chat endpoints.

    The provider is intentionally a replaceable boundary.  It does not make
    network calls unless explicitly selected by the caller/CLI, and it returns
    only the strict extraction object to the service layer.  A local server
    such as Ollama, LM Studio, or a self-hosted gateway can use the same class
    by changing ``base_url`` and omitting ``api_key``.
    """

    provider_name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        prompt_version: str = "coding-extraction-v1",
        max_tokens: int = 4000,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("CODEMEMORY_LLM_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("CODEMEMORY_LLM_API_KEY")
        self.model_name = model or os.environ.get("CODEMEMORY_LLM_MODEL") or "gpt-4o-mini"
        configured_timeout = timeout_seconds
        if configured_timeout is None:
            configured_timeout = float(os.environ.get("CODEMEMORY_LLM_TIMEOUT_SECONDS", "60"))
        self.timeout_seconds = max(1.0, min(float(configured_timeout), 600.0))
        self.prompt_version = prompt_version
        self.max_tokens = max(256, min(int(max_tokens), 32_000))

    @classmethod
    def from_env(cls) -> "OpenAICompatibleProvider":
        """Construct a provider from CODEMEMORY_LLM_* settings."""

        return cls()

    def extract(self, context: AssembledContext) -> dict[str, Any]:
        request_payload = {
            "model": self.model_name,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": self._user_prompt(context)},
            ],
            # Most OpenAI-compatible servers ignore an unsupported response
            # format; the parser below still enforces the schema locally.
            "response_format": {"type": "json_object"},
        }
        encoded = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=encoded,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read(8_000_000)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4000).decode("utf-8", errors="replace")
            raise ProviderError(f"provider HTTP {exc.code}: {detail[:1000]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"provider transport failure: {type(exc).__name__}: {exc}") from exc
        try:
            response_data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError("provider returned invalid JSON") from exc
        content = self._content(response_data)
        try:
            parsed = json.loads(self._strip_fence(content))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"provider message content is not JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ProviderError("provider JSON root must be an object")
        # Fill only identity metadata that the service already knows.  Source
        # and candidate evidence are intentionally not invented here.
        parsed.setdefault("schema_version", "codememory.extraction_batch.v1")
        parsed.setdefault("extraction_run_id", context.extraction_run_id)
        parsed.setdefault("project_id", context.project_id)
        parsed.setdefault("task_id", context.task_id)
        parsed.setdefault("session_id", context.session_id)
        parsed.setdefault("source_event_ids", list(context.event_ids))
        parsed.setdefault(
            "extractor",
            {
                "provider": self.provider_name,
                "model": self.model_name,
                "prompt_version": self.prompt_version,
            },
        )
        return parsed

    @staticmethod
    def _content(response_data: Any) -> str:
        try:
            choices = response_data["choices"]
            message = choices[0]["message"]
            content = message.get("content")
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("provider response has no choices[0].message.content") from exc
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [str(item.get("text")) for item in content if isinstance(item, dict) and item.get("text")]
            if parts:
                return "\n".join(parts)
        raise ProviderError("provider message content must be text")

    @staticmethod
    def _strip_fence(content: str) -> str:
        text = content.strip()
        if text.startswith("```") and text.endswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        return text.strip()

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You extract durable coding-memory candidates from a visible agent task. "
            "Return one JSON object only. Use schema codememory.extraction_batch.v1. "
            "Every candidate must cite one or more exact source_event_ids and every "
            "binding must cite event ids from that candidate. Do not include hidden "
            "reasoning, credentials, or unsupported facts. Candidate output is a "
            "proposal, never a final verified code fact."
        )

    @staticmethod
    def _user_prompt(context: AssembledContext) -> str:
        payload = {
            "project_id": context.project_id,
            "task_id": context.task_id,
            "session_id": context.session_id,
            "source_event_ids": list(context.event_ids),
            "truncated": context.truncated,
            "events": list(context.events),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def provider_from_name(name: str) -> ExtractionProvider:
    """Resolve a configured provider without making a network call."""

    normalized = name.strip().casefold()
    if normalized in {"mock", "heuristic", "offline"}:
        return MockLLMProvider()
    if normalized in {"openai", "openai-compatible", "http", "local"}:
        provider = OpenAICompatibleProvider.from_env()
        if normalized == "local":
            provider.provider_name = "openai-compatible-local"  # type: ignore[misc]
        return provider
    raise ValueError(f"unsupported extraction provider: {name}")
