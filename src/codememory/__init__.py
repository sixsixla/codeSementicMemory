"""CodeSementicMemory: a local, SQLite-first coding-memory core."""

from .domain.events import EVENT_SCHEMA_VERSION, EventEnvelope, EventType

__all__ = ["EVENT_SCHEMA_VERSION", "EventEnvelope", "EventType"]

__version__ = "0.1.0"
