"""Phase 2A candidate-memory extraction primitives."""

from .context import AssembledContext, ContextAssembler
from .models import (
    Binding,
    BindingRole,
    Candidate,
    CandidateKind,
    ExtractionBatch,
    ExtractorInfo,
    LifecycleHint,
)
from .providers import (
    ExtractionProvider,
    MockLLMProvider,
    OpenAICompatibleProvider,
    ProviderError,
    provider_from_name,
)
from .service import ExtractionResult, ExtractionService

__all__ = [
    "AssembledContext",
    "Binding",
    "BindingRole",
    "Candidate",
    "CandidateKind",
    "ContextAssembler",
    "ExtractionBatch",
    "ExtractionProvider",
    "ExtractionResult",
    "ExtractionService",
    "ExtractorInfo",
    "LifecycleHint",
    "MockLLMProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "provider_from_name",
]
