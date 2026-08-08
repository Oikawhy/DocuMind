"""DocumentChunk ORM model matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base


class DocumentChunk(Base):
    """Immutable text chunk with profile revision and projection references."""

    __tablename__ = "document_chunk"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_version.id"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_path: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    block_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chunk_profile_revision.id"),
        nullable=False,
    )
    embedding_model_digest: Mapped[str] = mapped_column(Text, nullable=False)
    tombstone_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="non_negative_chunk_index"),
        CheckConstraint("start_offset >= 0", name="non_negative_start_offset"),
        CheckConstraint("end_offset > start_offset", name="end_after_start"),
        CheckConstraint("token_count > 0", name="positive_token_count"),
        UniqueConstraint("version_id", "chunk_index", name="uq_chunk_version_index"),
        UniqueConstraint(
            "version_id",
            "content_sha256",
            "start_offset",
            "end_offset",
            name="uq_chunk_content_span",
        ),
    )
