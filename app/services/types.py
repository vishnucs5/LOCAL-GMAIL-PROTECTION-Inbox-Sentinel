from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class MessageEnvelope:
    gmail_message_id: str
    thread_id: str | None
    history_id: str | None
    sender: str
    subject: str
    snippet: str
    normalized_text: str
    received_at: datetime | None


@dataclass(slots=True)
class SyncBatch:
    messages: list[MessageEnvelope]
    latest_history_id: str | None
    source: str
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ClassificationDecision:
    verdict: str
    confidence: float
    reasons: list[str]
    provider_request_id: str | None = None
    provider_model: str | None = None


@dataclass(slots=True)
class SyncReport:
    status: str
    source: str
    fetched_count: int = 0
    classified_count: int = 0
    labeled_count: int = 0
    skipped_count: int = 0
    error: str | None = None
    ran_at: datetime | None = None
    notes: list[str] = field(default_factory=list)
