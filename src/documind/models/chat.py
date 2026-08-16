"""ChatSession, ChatMessage, and AgentRun ORM models matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base


class ChatSession(Base):
    """Retention-scoped chat session."""

    __tablename__ = "chat_session"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    retention_expires_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class ChatMessage(Base):
    """Individual message within a chat session."""

    __tablename__ = "chat_message"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_session.id"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="valid_chat_role",
        ),
    )


class AgentRun(Base):
    """LangGraph agent execution record within a chat session."""

    __tablename__ = "agent_run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_session.id"),
        nullable=False,
    )
    trigger_message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_message.id"),
        nullable=False,
    )
    graph_state_checkpoint: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_message.id"),
        nullable=True,
    )
    trace_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # --- RAG pipeline persisted fields (§7.3 / §8.1) ---
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    principal_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_revisions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    plan: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    rewritten_query_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    retrieval_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    filtered_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    reranked_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    model_route_revisions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    prompt_revisions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    schema_validation_outcomes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    retry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    citation_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    timing: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    abstention_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

