"""PostgreSQL ENUM types matching §8.1 DDL."""

import enum


class DocumentLifecycle(enum.StrEnum):
    """Immutable version lifecycle states.

    Transitions: accepted → processing/quarantined/failed/erased;
    processing → completed/failed/quarantined/erased;
    completed → erased; failed → processing (authorized replay);
    quarantined → processing (approved remediation).
    No row may transition out of erased.
    """

    ACCEPTED = "accepted"
    QUARANTINED = "quarantined"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    ERASED = "erased"


class StageStatus(enum.StrEnum):
    """Processing stage lifecycle."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class ExtractionStatus(enum.StrEnum):
    """Structured extraction lifecycle."""

    NOT_REQUESTED = "not_requested"
    PENDING_TEMPLATE = "pending_template"
    QUEUED = "queued"
    COMPLETED = "completed"
    FAILED = "failed"


class PolicyStatus(enum.StrEnum):
    """Versioned policy lifecycle."""

    DRAFT = "draft"
    REVIEW = "review"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETIRED = "retired"
    REJECTED = "rejected"


class OperationStatus(enum.StrEnum):
    """Asynchronous operation lifecycle."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
