"""Operation, ProcessingRun, and ProcessingStage ORM models matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Enum,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base, _enum_values
from documind.models.enums import OperationStatus, StageStatus


class Operation(Base):
    """Asynchronous operation record with idempotency key."""

    __tablename__ = "operation"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    operation_type: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document.id"),
        nullable=True,
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_version.id"),
        nullable=True,
    )
    requested_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[OperationStatus] = mapped_column(
        Enum(OperationStatus, name="operation_status", create_type=False, values_callable=_enum_values),
        nullable=False,
        server_default=text("'accepted'"),
    )
    temporal_workflow_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    result_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    safe_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "requested_by_subject",
            "idempotency_key_hash",
            name="uq_operation_idempotency",
        ),
    )


class ProcessingRun(Base):
    """Temporal workflow execution record."""

    __tablename__ = "processing_run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_version.id"),
        nullable=False,
    )
    temporal_workflow_id: Mapped[str] = mapped_column(Text, nullable=False)
    temporal_run_id: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_event_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "temporal_workflow_id",
            "temporal_run_id",
            name="uq_processing_run_temporal",
        ),
    )


class ProcessingStage(Base):
    """Individual processing stage within a run."""

    __tablename__ = "processing_stage"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    processing_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_run.id"),
        nullable=False,
    )
    stage_name: Mapped[str] = mapped_column(Text, nullable=False)
    stage_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[StageStatus] = mapped_column(
        Enum(StageStatus, name="stage_status", create_type=False, values_callable=_enum_values),
        nullable=False,
        server_default=text("'queued'"),
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    policy_revision_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    trace_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    safe_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("processing_run_id", "stage_name", name="uq_stage_name"),
        UniqueConstraint("processing_run_id", "idempotency_key", name="uq_stage_idempotency"),
    )
