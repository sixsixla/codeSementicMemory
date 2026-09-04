"""Deterministic coding-memory quality classification.

This module is intentionally dependency-free and side-effect free.  It is the
first gate in front of any optional LLM judge: the same event/candidate always
gets the same decision and reasons, which makes replay and regression tests
possible.  A future model-assisted judge can add evidence under the same
dimensions without changing the persistence contract.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .models import (
    QUALITY_CLASSIFIER_VERSION,
    CandidateQualityReview,
    EventQualityEvaluation,
    EventRole,
    QualityDecision,
)
from .project_scope import has_external_project_reference


_CODING_TERMS = re.compile(
    r"(?i)(?:\b(?:code|coding|class|method|function|file|repo|repository|git|p4|unity|wwise|"
    r"compile|build|test|bug|fix|debug|refactor|mcp|vsg|prefab|shader|api|interface|"
    r"implementation|implement|change|edit|patch|source)\b|"
    r"代码|源码|文件|工程|项目|功能|实现|修改|修复|排查|定位|编译|构建|测试|验证|断点|"
    r"配置|流程图|脚本|组件|模块|接口|逻辑|玩法|性能|重构|导出|提交|分支|代码库)"
)
_FAILURE_TERMS = re.compile(
    r"(?i)(?:\b(?:bug|error|exception|failed|failure|broken|regression|crash|issue|"
    r"rejected|forbidden|invalid|not\s+working)\b|"
    r"错误|失败|报错|异常|未生效|不起作用|不工作|崩溃|根因|问题在|问题是|排查|"
    r"拒绝|禁止|禁用|不应|不要|不允许|错误路径)"
)
_SUCCESS_TERMS = re.compile(
    r"(?i)(?:\b(?:pass|passed|success|successful|ok|exit\s*code\s*0|exitcode.?0)\b|"
    r"编译通过|验证通过|测试通过|构建成功|已完成|完成了)"
)
_ACK_RE = re.compile(
    r"(?is)^\s*(?:同意|好的?|可以|收到|明白|确认|继续|执行修改|继续执行|按(?:上面|以上|最终|结论|计划).{0,60}(?:执行|修改|开发|做)|"
    r"continue|proceed|approved?|go ahead|sounds good)\s*[。.!！]?\s*$"
)
_ORCHESTRATION_RE = re.compile(
    r"(?is)^\s*(?:role\s*:|goal\s*:|task\s*:\s*(?:validate|investigate)|"
    r"project[_ ]j\s+primary\s+domain|you are an?\s+(?:investigator|subagent)|"
    r"act as the delivery lead|\{\s*[\"']?(?:assignment_id|agent_run_id|verdict|findings)\b)"
)
_PATH_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/])?[^\s`\"'<>|()\[\]，。；：、]+?\.(?:cs|py|ts|tsx|js|jsx|go|rs|java|cpp|c|h|hpp|json|yaml|yml|sql|prefab|asset|shader|asmdef|md|xlsx)(?=$|[\s`\"'<>|()\[\]),.;:，。；：、])",
    re.IGNORECASE,
)
_CODE_EXTENSIONS = {
    "cs",
    "py",
    "ts",
    "tsx",
    "js",
    "jsx",
    "go",
    "rs",
    "java",
    "cpp",
    "c",
    "h",
    "hpp",
    "json",
    "yaml",
    "yml",
    "sql",
    "prefab",
    "asset",
    "shader",
    "asmdef",
    "xlsx",
}
_AUXILIARY_RE = re.compile(
    r"(?i)(?:^|/)(?:agents\.md|skill\.md)$|(?:^|/)(?:\.codex|codex-skills|recommended_plugins)(?:/|$)"
)
_GLOB_CHARS = set("*?[]{}")
_GENERIC_SYMBOLS = {
    "Code",
    "Memory",
    "CodeMemory",
    "CodebaseMemory",
    "Project",
    "Project_J",
    "Assets",
    "Script",
    "Game",
    "Common",
    "Server",
    "Client",
    "Panel",
    "Current",
    "Result",
    "Response",
    "Task",
    "Event",
    "Message",
    "JSON",
    "SQLite",
}
_EXTERNAL_SYMBOLS = {"codebasememory", "codememory", "supermemory"}
_CANDIDATE_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:coding route observed|observed issue or rejected path|coding outcome)\s*:\s*"
)
_MODIFIED_EVIDENCE_TYPES = {"file_edit", "vcs_change"}


def _str(value: Any) -> str:
    return str(value or "").strip()


def _event_text(event: Mapping[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    values: list[str] = []
    for key in ("text", "message", "summary", "title", "change", "command", "query", "error", "outcome"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    # Some callers pass an already projected text field.
    direct = event.get("text")
    if isinstance(direct, str) and direct.strip():
        values.insert(0, direct.strip())
    return "\n".join(values)


def _event_paths(event: Mapping[str, Any]) -> list[str]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    values: list[Any] = []
    for key in ("path", "file_path", "file", "paths", "files", "modified_files", "changed_files"):
        value = payload.get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value:
            values.append(value)
    artifacts = event.get("artifacts")
    if isinstance(artifacts, list):
        values.extend(item.get("path") for item in artifacts if isinstance(item, Mapping))
    values.extend(_PATH_RE.findall(_event_text(event)))
    result: list[str] = []
    for value in values:
        path = _str(value).strip("`'\"(),.;:，。；：、")
        if path and path not in result:
            result.append(path)
    return result


def _event_symbols(event: Mapping[str, Any]) -> list[str]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    values: list[Any] = []
    for key in ("symbol", "symbols", "qualified_symbol", "qualified_symbols", "class", "classes"):
        value = payload.get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value:
            values.append(value)
    text = _event_text(event)
    values.extend(re.findall(r"\b[A-Z][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?\b", text))
    result: list[str] = []
    for value in values:
        symbol = _str(value)
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def is_concrete_path(value: Any) -> bool:
    """Return whether a value resembles a repository file rather than prose."""

    path = _str(value).replace("\\", "/")
    if not path or "://" in path or any(char in path for char in _GLOB_CHARS):
        return False
    if _AUXILIARY_RE.search(path):
        return False
    basename = path.rsplit("/", 1)[-1]
    if "." not in basename:
        return False
    extension = basename.rsplit(".", 1)[-1].casefold()
    return extension in _CODE_EXTENSIONS and len(path) <= 4000


def is_concrete_symbol(value: Any) -> bool:
    symbol = _str(value)
    if not symbol or "://" in symbol or any(char in symbol for char in _GLOB_CHARS):
        return False
    if symbol in _GENERIC_SYMBOLS or len(symbol) > 1000:
        return False
    # Qualified symbols are allowed; each component must be identifier-like.
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", symbol)) and (
        "_" in symbol or re.search(r"[a-z][A-Z]", symbol) is not None or "." in symbol
    )


def _has_explicit_intent(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) < 8 or _ACK_RE.match(compact) or _ORCHESTRATION_RE.match(compact):
        return False
    return bool(_CODING_TERMS.search(compact) or _PATH_RE.search(compact))


def classify_event(event: Mapping[str, Any], *, classifier_version: str = QUALITY_CLASSIFIER_VERSION) -> EventQualityEvaluation:
    """Classify one canonical event without writing to storage."""

    event_id = _str(event.get("event_id"))
    project_id = _str(event.get("project_id"))
    task_id = _str(event.get("task_id"))
    event_type = _str(event.get("event_type")).casefold()
    text = _event_text(event)
    paths = _event_paths(event)
    symbols = _event_symbols(event)
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    context = event.get("context") if isinstance(event.get("context"), Mapping) else {}
    has_path = any(is_concrete_path(path) for path in paths)
    has_symbol = any(is_concrete_symbol(symbol) for symbol in symbols)
    has_code_signal = has_path or has_symbol or bool(_CODING_TERMS.search(text))
    scope_mismatch = has_external_project_reference(
        context.get("root_path") or context.get("cwd"), text, paths
    )
    reasons: list[str] = []
    dimensions: dict[str, Any] = {
        "visible_text": bool(text.strip()),
        "concrete_paths": sum(is_concrete_path(path) for path in paths),
        "concrete_symbols": sum(is_concrete_symbol(symbol) for symbol in symbols),
        "event_type": event_type,
        "scope_mismatch": scope_mismatch,
    }

    if event_type in {"user_message", "assistant_message"}:
        if not text.strip() or _ACK_RE.match(text) or _ORCHESTRATION_RE.match(text):
            role = EventRole.NOISE.value
            decision = QualityDecision.QUARANTINE.value
            score = 0.05 if text.strip() else 0.0
            reasons.append("acknowledgement or orchestration text has no independent coding intent")
        elif event_type == "user_message" and _has_explicit_intent(text):
            role = EventRole.INTENT.value
            decision = QualityDecision.ACCEPTED.value
            score = 0.86 if has_path else 0.76
            reasons.append("visible user request contains a coding intent")
        elif has_code_signal:
            role = EventRole.CODE_EVIDENCE.value
            decision = QualityDecision.REVIEW.value
            score = 0.64
            reasons.append("visible message contains coding evidence but is not a primary request")
        else:
            role = EventRole.UNKNOWN.value
            decision = QualityDecision.REVIEW.value
            score = 0.24
            reasons.append("visible message is not sufficiently coding-specific")
    elif event_type in {"file_read", "file_edit", "vcs_change", "tool_call", "tool_result"}:
        role = EventRole.CODE_EVIDENCE.value
        if has_path or has_symbol or event_type in {"file_edit", "vcs_change"}:
            decision = QualityDecision.ACCEPTED.value
            score = 0.88 if has_path else 0.72
            reasons.append("structured tool/file/VCS event supplies concrete code evidence")
        else:
            decision = QualityDecision.REVIEW.value
            score = 0.36
            reasons.append("tool event lacks a concrete file or symbol target")
    elif event_type in {"validation_run", "command_run"}:
        role = EventRole.OUTCOME.value
        command = _str(payload.get("command") or payload.get("cmd"))
        status = _str(payload.get("status") or payload.get("outcome") or payload.get("result"))
        dimensions.update({"has_command": bool(command), "status": status.casefold()})
        if command and status:
            decision = QualityDecision.ACCEPTED.value
            score = 0.92 if _SUCCESS_TERMS.search(status) else 0.72
            reasons.append("actual command/validation record contains an outcome")
        elif command:
            decision = QualityDecision.REVIEW.value
            score = 0.58
            reasons.append("actual command is present but its outcome is incomplete")
        else:
            decision = QualityDecision.QUARANTINE.value
            score = 0.18
            reasons.append("textual validation claim without an executable command is not proof")
    elif event_type == "user_feedback":
        role = EventRole.FEEDBACK.value
        if text.strip() and not _ACK_RE.match(text):
            decision = QualityDecision.REVIEW.value
            score = 0.58
            reasons.append("user feedback can refine or reject a prior memory")
        else:
            decision = QualityDecision.QUARANTINE.value
            score = 0.08
            reasons.append("empty or acknowledgement feedback has no durable signal")
    elif event_type in {"session_started", "session_ended"}:
        role = EventRole.LIFECYCLE.value
        decision = QualityDecision.REVIEW.value
        score = 0.22
        reasons.append("session lifecycle is provenance, not an independent memory fact")
    else:
        role = EventRole.UNKNOWN.value
        decision = QualityDecision.REVIEW.value
        score = 0.2 if text.strip() else 0.0
        reasons.append("event type is outside the coding quality vocabulary")

    if scope_mismatch:
        # A conversation in this repository may discuss Project_J as an
        # external subject, but that text must never be silently promoted to a
        # CodeSementicMemory fact.  Keep the evidence and downgrade only an
        # otherwise-accepted event; noise remains quarantine and review/lifecycle
        # states stay available for audit.
        reasons.append(
            "event mentions external Project_J while scoped to CodeSementicMemory (scope_mismatch)"
        )
        if decision == QualityDecision.ACCEPTED.value:
            decision = QualityDecision.REVIEW.value
            score = min(score, 0.54)

    if not reasons:
        reasons.append("classified by deterministic coding quality rules")
    return EventQualityEvaluation(
        event_id=event_id,
        project_id=project_id,
        task_id=task_id,
        role=role,
        decision=decision,
        signal_score=max(0.0, min(1.0, round(float(score), 4))),
        dimensions=dimensions,
        reasons=tuple(dict.fromkeys(reasons)),
        classifier_version=classifier_version,
    )


def _candidate_value(candidate: Any, key: str, default: Any = None) -> Any:
    if isinstance(candidate, Mapping):
        return candidate.get(key, default)
    return getattr(candidate, key, default)


def _binding_role(binding: Any) -> str:
    value = _candidate_value(binding, "role", "")
    # ``BindingRole`` is a StrEnum in the strict extraction model, while
    # replayed/LLM-produced rows may still be plain strings.
    return _str(getattr(value, "value", value)).casefold()


def _normalise_path(value: Any) -> str:
    return _str(value).replace("\\", "/").rstrip("/").casefold()


def _binding_matches_event(binding: Any, event: Mapping[str, Any]) -> bool:
    """Check that a binding target is present in the cited event payload.

    This is deliberately lexical rather than an AST/LSP lookup (that is the
    Phase 4B boundary), but it prevents a provider from attaching an invented
    ``modified_file`` to an unrelated user/assistant sentence.
    """

    path = _candidate_value(binding, "path")
    if path:
        target = _normalise_path(path)
        if target:
            for event_path in _event_paths(event):
                candidate = _normalise_path(event_path)
                if candidate == target or candidate.endswith("/" + target) or target.endswith("/" + candidate):
                    return True
    symbol = _candidate_value(binding, "qualified_symbol") or _candidate_value(binding, "symbol")
    if symbol:
        target_symbol = _str(symbol)
        if target_symbol and any(target_symbol == value for value in _event_symbols(event)):
            return True
        # Tool payloads sometimes provide a symbol in a free-form result that
        # the conservative symbol extractor cannot parse as a standalone token.
        if target_symbol and re.search(rf"(?<![A-Za-z0-9_]){re.escape(target_symbol)}(?![A-Za-z0-9_])", _event_text(event)):
            return True
    return False


def review_candidate(
    candidate: Any,
    events: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    event_evaluations: Mapping[str, EventQualityEvaluation] | None = None,
    classifier_version: str = QUALITY_CLASSIFIER_VERSION,
) -> CandidateQualityReview:
    """Review one extracted candidate against its cited canonical events."""

    event_map = events if isinstance(events, Mapping) else {
        _str(item.get("event_id")): item for item in events if _str(item.get("event_id"))
    }
    candidate_id = _str(_candidate_value(candidate, "candidate_id"))
    project_id = _str(_candidate_value(candidate, "project_id"))
    task_id = _str(_candidate_value(candidate, "task_id"))
    kind = _str(_candidate_value(candidate, "kind")).casefold()
    statement = _str(_candidate_value(candidate, "statement"))
    aliases = [_str(item) for item in (_candidate_value(candidate, "aliases", []) or []) if _str(item)]
    bindings = _candidate_value(candidate, "bindings", []) or []
    evidence_ids = [_str(item) for item in (_candidate_value(candidate, "evidence_event_ids", []) or []) if _str(item)]
    reasons: list[str] = []
    dimensions: dict[str, Any] = {
        "kind": kind,
        "evidence_count": len(evidence_ids),
        "binding_count": len(bindings),
    }
    cited_events = [event_map[event_id] for event_id in evidence_ids if event_id in event_map]
    missing_events = [event_id for event_id in evidence_ids if event_id not in event_map]
    if missing_events:
        reasons.append(f"candidate cites missing events: {', '.join(missing_events[:5])}")
    evaluations: list[EventQualityEvaluation] = []
    for event in cited_events:
        event_id = _str(event.get("event_id"))
        evaluation = (event_evaluations or {}).get(event_id) or classify_event(
            event, classifier_version=classifier_version
        )
        evaluations.append(evaluation)
    scope_mismatch = any(
        bool(item.dimensions.get("scope_mismatch")) for item in evaluations
    )
    if not scope_mismatch:
        # A provider may put the external reference in the synthesized
        # statement/binding rather than in a cited event.  Reuse the root from
        # any cited event, but never infer a mismatch for a Project_J checkout
        # itself.
        for event in cited_events:
            event_context = event.get("context") if isinstance(event.get("context"), Mapping) else {}
            event_paths = _event_paths(event)
            if has_external_project_reference(
                event_context.get("root_path") or event_context.get("cwd"),
                statement + " " + " ".join(aliases),
                event_paths,
            ):
                scope_mismatch = True
                break
    has_intent = any(
        item.role == EventRole.INTENT.value and item.decision != QualityDecision.QUARANTINE.value
        for item in evaluations
    )
    has_code_event = any(
        item.role in {EventRole.CODE_EVIDENCE.value, EventRole.OUTCOME.value}
        and item.decision != QualityDecision.QUARANTINE.value
        for item in evaluations
    )
    statement_intent = _has_explicit_intent(statement) or _has_explicit_intent(" ".join(aliases))
    dimensions.update({"intent": has_intent or statement_intent, "code_evidence": has_code_event})

    valid_bindings = 0
    invalid_bindings = 0
    external_bindings = 0
    modified_bindings = 0
    unverified_modified_bindings = 0
    for binding in bindings:
        path = _candidate_value(binding, "path") if not isinstance(binding, Mapping) else binding.get("path")
        symbol = _candidate_value(binding, "qualified_symbol") if not isinstance(binding, Mapping) else binding.get("qualified_symbol")
        if not symbol:
            symbol = _candidate_value(binding, "symbol") if not isinstance(binding, Mapping) else binding.get("symbol")
        path_ok = bool(path and is_concrete_path(path))
        symbol_ok = bool(symbol and is_concrete_symbol(symbol))
        external_symbol = _str(symbol).casefold() in _EXTERNAL_SYMBOLS
        role = _binding_role(binding)
        binding_evidence_ids = [
            _str(item)
            for item in (_candidate_value(binding, "evidence", []) or [])
            if _str(item)
        ]
        binding_events = [event_map[event_id] for event_id in binding_evidence_ids if event_id in event_map]
        if role == "modified_file":
            modified_bindings += 1
            has_modified_evidence = any(
                _str(event.get("event_type")).casefold() in _MODIFIED_EVIDENCE_TYPES
                and _binding_matches_event(binding, event)
                for event in binding_events
            )
            if not has_modified_evidence:
                unverified_modified_bindings += 1
                invalid_bindings += 1
                reasons.append(
                    "modified_file binding lacks matching file_edit/vcs_change evidence"
                )
                continue
        if path_ok or symbol_ok or external_symbol:
            valid_bindings += 1
            if external_symbol:
                external_bindings += 1
                reasons.append("binding points to an external memory/index service and needs project-scope verification")
        else:
            invalid_bindings += 1
            reasons.append("binding is a URL, glob, auxiliary document, synthetic name, or unsupported target")
    dimensions.update(
        {
            "valid_bindings": valid_bindings,
            "invalid_bindings": invalid_bindings,
            "external_bindings": external_bindings,
            "modified_bindings": modified_bindings,
            "unverified_modified_bindings": unverified_modified_bindings,
        }
    )

    has_actual_validation = any(item.role == EventRole.OUTCOME.value for item in evaluations)
    validation_success = any(
        item.role == EventRole.OUTCOME.value
        and str(item.dimensions.get("status", ""))
        and _SUCCESS_TERMS.search(str(item.dimensions.get("status", "")))
        for item in evaluations
    )
    dimensions.update(
        {
            "actual_validation": has_actual_validation,
            "validation_success": validation_success,
            "scope_mismatch": scope_mismatch,
        }
    )
    signal_statement = _CANDIDATE_PREFIX_RE.sub("", statement)
    generic_noise = bool(
        _ACK_RE.match(signal_statement)
        or _ORCHESTRATION_RE.match(signal_statement)
        or not signal_statement.strip()
    )
    dimensions["generic_noise"] = generic_noise

    # First fail closed on integrity and obvious synthetic output.
    if missing_events or not evidence_ids:
        decision = QualityDecision.QUARANTINE.value
        score = 0.0
        reasons.append("candidate has no complete canonical evidence chain")
    elif generic_noise:
        decision = QualityDecision.QUARANTINE.value
        score = 0.04
        reasons.append("candidate statement is only an acknowledgement or orchestration prompt")
    elif invalid_bindings and not valid_bindings:
        decision = QualityDecision.QUARANTINE.value
        score = 0.12
        reasons.append("candidate has no usable repository binding")
    elif kind == "route_observation":
        if not (has_intent or statement_intent):
            decision = QualityDecision.QUARANTINE.value
            score = 0.2
            reasons.append("route observation lacks a substantive user intent")
        elif valid_bindings == 0:
            decision = QualityDecision.REVIEW.value
            score = 0.48 if has_code_event else 0.42
            reasons.append("route has intent but no concrete repository binding; keep for review only")
        elif invalid_bindings or external_bindings:
            decision = QualityDecision.REVIEW.value
            score = 0.58
            reasons.append("route includes bindings that need source/scope verification")
        else:
            decision = QualityDecision.ACCEPTED.value
            score = 0.82 if has_intent and valid_bindings else 0.68
            reasons.append("route has intent and concrete code evidence")
    elif kind == "validation":
        if not has_actual_validation:
            decision = QualityDecision.QUARANTINE.value
            score = 0.18
            reasons.append("validation candidate is not backed by an actual command event")
        elif validation_success:
            decision = QualityDecision.ACCEPTED.value
            score = 0.86
            reasons.append("validation candidate is backed by an executed successful command")
        else:
            decision = QualityDecision.REVIEW.value
            score = 0.52
            reasons.append("validation command exists but outcome requires review")
    elif kind in {"failure", "anti_binding"}:
        if _FAILURE_TERMS.search(statement) or any(_FAILURE_TERMS.search(_event_text(event)) for event in cited_events):
            decision = QualityDecision.REVIEW.value
            score = 0.62
            reasons.append("negative observation is useful but must be scoped to the affected revision")
        else:
            decision = QualityDecision.QUARANTINE.value
            score = 0.2
            reasons.append("negative candidate has no explicit failure or rejection signal")
    elif kind in {"decision", "convention", "preference"}:
        # A decision can be expressed entirely in domain language (for
        # example, a Chinese gameplay sentence without generic words such as
        # “代码” or “文件”).  Evidence presence and minimum substance are the
        # useful first gate; source verification remains a review obligation.
        if len(statement) < 12:
            decision = QualityDecision.QUARANTINE.value
            score = 0.22
            reasons.append("natural-language candidate is too generic to guide coding")
        else:
            decision = QualityDecision.REVIEW.value
            score = 0.58 if has_intent else 0.46
            reasons.append("decision-like memory is retained for review until source evidence confirms it")
    else:
        decision = QualityDecision.REVIEW.value
        score = 0.4
        reasons.append("candidate kind is useful but has no specialized acceptance policy")

    if scope_mismatch:
        reasons.append(
            "candidate references external Project_J from a CodeSementicMemory task (scope_mismatch)"
        )
        if decision == QualityDecision.ACCEPTED.value:
            decision = QualityDecision.REVIEW.value
            score = min(score, 0.54)

    candidate_confidence = _candidate_value(candidate, "confidence", 0.0)
    try:
        score = min(1.0, max(0.0, (float(score) * 0.75) + (float(candidate_confidence) * 0.25)))
    except (TypeError, ValueError):
        score = min(1.0, max(0.0, float(score)))
    return CandidateQualityReview(
        candidate_id=candidate_id,
        project_id=project_id,
        task_id=task_id,
        decision=decision,
        quality_score=round(score, 4),
        dimensions=dimensions,
        reasons=tuple(dict.fromkeys(reasons)),
        classifier_version=classifier_version,
    )
