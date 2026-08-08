"""ORM models for the DocuMind PostgreSQL schema.

Re-exports Base, all ENUMs, and all model classes for convenient import.
"""

from documind.models.audit import AuditAnchor, AuditEvent, AuditEventIdentity
from documind.models.base import Base
from documind.models.chat import AgentRun, ChatMessage, ChatSession
from documind.models.chunk import DocumentChunk
from documind.models.document import Document, DocumentVersion
from documind.models.enums import (
    DocumentLifecycle,
    ExtractionStatus,
    OperationStatus,
    PolicyStatus,
    StageStatus,
)
from documind.models.extraction import StructuredExtraction
from documind.models.graph import GraphEntity, GraphFact
from documind.models.identity import IdentityGroupMembership, IdentitySubject
from documind.models.label import DeletionTombstone, DocumentLabel, Label, LegalHold
from documind.models.model_route import ModelRouteRevision
from documind.models.outbox import DeadLetter, OutboxEvent
from documind.models.policy import ChunkProfileRevision, DeclaredType, PolicyRevision
from documind.models.processing import Operation, ProcessingRun, ProcessingStage
from documind.models.projection import ActiveProjectionGeneration, ProjectionState
from documind.models.template import ExtractionTemplateRevision, TemplateProposal
from documind.models.webhook import Webhook, WebhookDelivery

__all__ = [
    # Base
    "Base",
    # Enums
    "DocumentLifecycle",
    "ExtractionStatus",
    "OperationStatus",
    "PolicyStatus",
    "StageStatus",
    # Document
    "Document",
    "DocumentVersion",
    # Label
    "Label",
    "DocumentLabel",
    "LegalHold",
    "DeletionTombstone",
    # Processing
    "Operation",
    "ProcessingRun",
    "ProcessingStage",
    # Outbox
    "OutboxEvent",
    "DeadLetter",
    # Policy
    "PolicyRevision",
    "DeclaredType",
    "ChunkProfileRevision",
    # Template
    "ExtractionTemplateRevision",
    "TemplateProposal",
    # Model Route
    "ModelRouteRevision",
    # Chunk
    "DocumentChunk",
    # Extraction
    "StructuredExtraction",
    # Graph
    "GraphEntity",
    "GraphFact",
    # Projection
    "ProjectionState",
    "ActiveProjectionGeneration",
    # Identity
    "IdentitySubject",
    "IdentityGroupMembership",
    # Chat
    "ChatSession",
    "ChatMessage",
    "AgentRun",
    # Webhook
    "Webhook",
    "WebhookDelivery",
    # Audit
    "AuditEvent",
    "AuditEventIdentity",
    "AuditAnchor",
]
