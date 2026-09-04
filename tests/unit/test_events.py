from __future__ import annotations

import pytest
from pydantic import ValidationError

from codememory.domain.events import EventEnvelope
from codememory.ingest.redaction import redact_event


def test_event_envelope_normalizes_naive_timestamp(event_factory):
    event = event_factory()
    event["occurred_at"] = "2026-09-02T08:00:00"
    parsed = EventEnvelope.model_validate(event)
    assert parsed.occurred_at.tzinfo is not None
    assert parsed.occurred_at.isoformat().endswith("+00:00")


def test_event_envelope_rejects_invalid_version_and_sequence(event_factory):
    event = event_factory()
    event["schema_version"] = "codememory.event.v2"
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event)
    event = event_factory()
    event["seq"] = -1
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event)


def test_session_boundary_requires_session_id(event_factory):
    event = event_factory(event_type="session_started", session_id=None, seq=0)
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event)


def test_redaction_removes_secret_values(event_factory):
    event = EventEnvelope.model_validate(
        event_factory(
            payload={
                "text": "token=abc123 password: hunter2",
                "nested": {"key": "sk-abcdefghijklmnop1234"},
            }
        )
    )
    redacted = redact_event(event)
    assert "abc123" not in str(redacted.payload)
    assert "hunter2" not in str(redacted.payload)
    assert redacted.redaction.applied is True
    assert redacted.redaction.fields


def test_redaction_covers_environment_style_names(event_factory):
    event = EventEnvelope.model_validate(
        event_factory(
            payload={"env": "OPENAI_API_KEY=super-secret AWS_SECRET_ACCESS_KEY=another-secret"}
        )
    )
    redacted = redact_event(event)
    assert "super-secret" not in redacted.payload["env"]
    assert "another-secret" not in redacted.payload["env"]
