"""Conservative local secret redaction at the ingest boundary."""

from __future__ import annotations

import re
from typing import Any

from ..domain.events import ArtifactRef, EventContext, EventEnvelope, RedactionInfo


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b[\w.-]*(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|passwd|secret|token)[\w.-]*\b\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]{12,})")
_OPENAI_STYLE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_SENSITIVE_KEY = re.compile(
    r"^(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|passwd|"
    r"secret|token|private[_-]?key|client[_-]?secret)$|"
    r"(?:[_-](?:api[_-]?key|access[_-]?token|password|passwd|secret|token|private[_-]?key|client[_-]?secret))$",
    re.IGNORECASE,
)


def redact_text(value: str) -> tuple[str, bool]:
    changed = False

    def replace_assignment(match: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        return f"{match.group(1)}[REDACTED]"

    value = _SECRET_ASSIGNMENT.sub(replace_assignment, value)

    def replace_bearer(match: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        return f"{match.group(1)}[REDACTED]"

    value = _BEARER.sub(replace_bearer, value)

    def replace_key(match: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        return "[REDACTED]"

    value = _OPENAI_STYLE.sub(replace_key, value)
    return value, changed


def redact_value(value: Any, path: str = "") -> tuple[Any, bool, list[str]]:
    if isinstance(value, str):
        redacted, changed = redact_text(value)
        return redacted, changed, [path] if changed and path else []
    if isinstance(value, list):
        output: list[Any] = []
        changed = False
        fields: list[str] = []
        for index, item in enumerate(value):
            item_value, item_changed, item_fields = redact_value(item, f"{path}[{index}]")
            output.append(item_value)
            changed = changed or item_changed
            fields.extend(item_fields)
        return output, changed, fields
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        changed = False
        fields: list[str] = []
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            # Pattern redaction is not enough for structured tool arguments:
            # a value such as ``{"api_key": "opaque-value"}`` contains no
            # assignment syntax and may not have a recognizable provider
            # prefix.  Redact sensitive-key values before recursively walking
            # ordinary metadata.  Numeric counters (for example
            # ``token_count``) are retained because they are not credentials.
            if _SENSITIVE_KEY.search(str(key)):
                if isinstance(item, (dict, list)):
                    output[key] = "[REDACTED]"
                    changed = True
                    fields.append(child_path)
                    continue
                if isinstance(item, str):
                    redacted, item_changed = redact_text(item)
                    output[key] = redacted if item_changed else "[REDACTED]"
                    changed = True
                    fields.append(child_path)
                    continue
            item_value, item_changed, item_fields = redact_value(item, child_path)
            output[key] = item_value
            changed = changed or item_changed
            fields.extend(item_fields)
        return output, changed, fields
    return value, False, []


def redact_event(event: EventEnvelope) -> EventEnvelope:
    """Return a copy with secrets removed from context, payload, and artifacts."""

    context, context_changed, context_fields = redact_value(
        event.context.model_dump(mode="python"), "context"
    )
    payload, payload_changed, payload_fields = redact_value(event.payload, "payload")
    artifacts = []
    artifacts_changed = False
    artifact_fields: list[str] = []
    for index, artifact in enumerate(event.artifacts):
        data = artifact.model_dump(mode="python")
        content, content_changed, content_fields = redact_value(
            data.get("content"), f"artifacts[{index}].content"
        )
        data["content"] = content
        metadata, changed, fields = redact_value(
            data.get("metadata", {}), f"artifacts[{index}].metadata"
        )
        data["metadata"] = metadata
        artifacts.append(data)
        artifacts_changed = artifacts_changed or changed or content_changed
        artifact_fields.extend(fields)
        artifact_fields.extend(content_fields)
    changed = context_changed or payload_changed or artifacts_changed
    existing = event.redaction.model_dump(mode="python")
    existing_fields = list(existing.get("fields", []))
    all_fields = list(
        dict.fromkeys(existing_fields + context_fields + payload_fields + artifact_fields)
    )
    redaction = RedactionInfo(
        **{
            **existing,
            "applied": bool(existing.get("applied") or changed),
            "fields": all_fields,
        }
    )
    if not changed and redaction == event.redaction:
        return event
    # ``model_copy(update=...)`` intentionally skips validation in Pydantic;
    # rebuild the artifact refs so downstream repository code always receives
    # typed values even after redaction.
    typed_context = EventContext.model_validate(context)
    typed_artifacts = [ArtifactRef.model_validate(item) for item in artifacts]
    return event.model_copy(
        update={
            "context": typed_context,
            "payload": payload,
            "artifacts": typed_artifacts,
            "redaction": redaction,
        }
    )
