"""Pydantic contracts for chat session management and messaging per §7.1 / §9.3."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Incoming chat message.

    If ``session_id`` is omitted the server creates a new session.
    The ``locale``, ``document_ids``, and ``mode`` fields pass through
    to the retrieval subsystem.
    """

    message: str = Field(..., min_length=1, max_length=8192)
    session_id: uuid.UUID | None = None
    locale: str = "en"
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    mode: str | None = None


class CitationOut(BaseModel):
    """Compact citation reference embedded in a chat response."""

    citation_id: str
    document_id: uuid.UUID
    version_id: uuid.UUID
    version_number: int
    chunk_id: uuid.UUID
    excerpt: str = ""
    content_sha256: str


class ChatResponse(BaseModel):
    """Response envelope for ``POST /v1/chat`` per §9.3."""

    session_id: uuid.UUID
    message_id: uuid.UUID
    answer: str
    abstained: bool = False
    citations: list[CitationOut] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] | None = None
    route: str | None = None
    agent_path: list[str] = Field(default_factory=list)
    policy_revisions: list[str] = Field(default_factory=list)
    model_route_revisions: dict[str, int] = Field(default_factory=dict)
    limitation_code: str | None = None
    trace_id: uuid.UUID


class MessageOut(BaseModel):
    """Single chat message in a session history response."""

    id: uuid.UUID
    role: str
    content: str
    token_count: int | None = None
    created_at: datetime
    # T9-08: Optional metadata from AgentRun for assistant messages.
    citations: list[str] | None = None
    confidence: str | None = None
    route: str | None = None


class SessionSummary(BaseModel):
    """Summary row for session listing."""

    id: uuid.UUID
    created_at: datetime
    retention_expires_at: datetime
    message_count: int = 0


class SessionDetail(BaseModel):
    """Full session with message history."""

    id: uuid.UUID
    created_at: datetime
    retention_expires_at: datetime
    messages: list[MessageOut] = Field(default_factory=list)
    summary: str | None = None


class FeedbackRequest(BaseModel):
    """User feedback on an assistant message."""

    score: int = Field(..., ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=1024)
