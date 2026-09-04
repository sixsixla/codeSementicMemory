"""Versioned candidate-to-card consolidation for coding memory."""

from .models import CardStatus, ConsolidationAction, ConsolidationResult
from .service import ConsolidationService
from .store import CardStore

__all__ = [
    "CardStatus",
    "ConsolidationAction",
    "ConsolidationResult",
    "ConsolidationService",
    "CardStore",
]
