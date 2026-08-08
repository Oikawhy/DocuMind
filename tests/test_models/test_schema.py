"""Schema validation tests for ORM models against PostgreSQL."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from documind.models import Base
from documind.models.audit import AuditEvent, AuditEventIdentity
from documind.models.chat import ChatMessage, ChatSession
from documind.models.chunk import DocumentChunk
from documind.models.document import Document, DocumentVersion
from documind.models.enums import (
    DocumentLifecycle,
    ExtractionStatus,
    OperationStatus,
    PolicyStatus,
    StageStatus,
)
from documind.models.graph import GraphEntity, GraphFact
from documind.models.identity import IdentitySubject
from documind.models.label import DeletionTombstone, Label
from documind.models.model_route import ModelRouteRevision
from documind.models.policy import ChunkProfileRevision, DeclaredType, PolicyRevision

EXPECTED_TABLES = {
    "document",
    "document_version",
    "label",
    "document_label",
    "legal_hold",
    "deletion_tombstone",
    "operation",
    "processing_run",
    "processing_stage",
    "outbox_event",
    "dead_letter",
    "policy_revision",
    "declared_type",
    "chunk_profile_revision",
    "extraction_template_revision",
    "template_proposal",
    "model_route_revision",
    "document_chunk",
    "structured_extraction",
    "graph_entity",
    "graph_fact",
    "projection_state",
    "active_projection_generation",
    "identity_subject",
    "identity_group_membership",
    "chat_session",
    "chat_message",
    "agent_run",
    "webhook",
    "webhook_delivery",
    "audit_event",
    "audit_event_identity",
    "audit_anchor",
}


class TestMetadataCompleteness:
    """Verify all spec tables are registered in ORM metadata."""

    def test_all_tables_in_metadata(self):
        """Every table from §8.1 must appear in the Base metadata."""
        actual_tables = set(Base.metadata.tables.keys())
        missing = EXPECTED_TABLES - actual_tables
        assert not missing, f"Missing tables: {missing}"


class TestDocumentLifecycleEnum:
    """Verify lifecycle ENUM values match §8.1."""

    def test_lifecycle_values(self):
        expected = {"accepted", "quarantined", "processing", "completed", "failed", "erased"}
        actual = {e.value for e in DocumentLifecycle}
        assert actual == expected

    def test_stage_status_values(self):
        expected = {"queued", "running", "succeeded", "retrying", "failed", "cancelled", "skipped"}
        actual = {e.value for e in StageStatus}
        assert actual == expected

    def test_extraction_status_values(self):
        expected = {"not_requested", "pending_template", "queued", "completed", "failed"}
        actual = {e.value for e in ExtractionStatus}
        assert actual == expected

    def test_policy_status_values(self):
        expected = {"draft", "review", "active", "superseded", "retired", "rejected"}
        actual = {e.value for e in PolicyStatus}
        assert actual == expected

    def test_operation_status_values(self):
        expected = {"accepted", "running", "succeeded", "failed", "cancelled"}
        actual = {e.value for e in OperationStatus}
        assert actual == expected


async def _guarded_version(async_session) -> DocumentVersion:
    """Create the minimum graph required for direct lifecycle trigger checks."""
    policy = PolicyRevision(
        id=uuid.uuid4(),
        policy_kind="authorization",
        stable_key=f"guard-policy-{uuid.uuid4().hex}",
        revision=1,
        status=PolicyStatus.ACTIVE,
        body={},
        body_sha256="a" * 64,
        created_by_subject="admin",
    )
    profile = ChunkProfileRevision(
        id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        revision=1,
        status=PolicyStatus.ACTIVE,
        configuration={},
        configuration_sha256="b" * 64,
    )
    async_session.add_all([policy, profile])
    await async_session.flush()
    declared = DeclaredType(
        id=uuid.uuid4(),
        stable_key=f"guard-type-{uuid.uuid4().hex}",
        active_policy_revision_id=policy.id,
    )
    async_session.add(declared)
    await async_session.flush()
    document = Document(
        id=uuid.uuid4(),
        title="Guarded document",
        declared_type_id=declared.id,
        created_by_subject="admin",
    )
    async_session.add(document)
    await async_session.flush()
    version = DocumentVersion(
        id=uuid.uuid4(),
        document_id=document.id,
        version_number=1,
        original_filename="guarded.txt",
        declared_mime_family="text/plain",
        byte_size=1,
        content_sha256="c" * 64,
        quarantine_object_key="quarantine/guarded/original",
        selected_chunk_profile_revision_id=profile.id,
        created_by_subject="admin",
    )
    async_session.add(version)
    await async_session.flush()
    return version


@pytest.mark.asyncio
class TestDatabaseImmutabilityGuards:
    """Exercise trigger enforcement rather than only ORM enum validation."""

    async def test_lifecycle_trigger_rejects_invalid_transition(self, async_session):
        version = await _guarded_version(async_session)
        version.lifecycle = DocumentLifecycle.PROCESSING
        await async_session.flush()
        version.lifecycle = DocumentLifecycle.COMPLETED
        await async_session.flush()

        with pytest.raises(DBAPIError, match="invalid document version lifecycle transition"):
            async with async_session.begin_nested():
                await async_session.execute(
                    text("UPDATE document_version SET lifecycle = 'accepted' WHERE id = :id"),
                    {"id": version.id},
                )

    async def test_tombstone_trigger_rejects_update_and_delete(self, async_session):
        version = await _guarded_version(async_session)
        tombstone = DeletionTombstone(
            id=uuid.uuid4(),
            document_id=version.document_id,
            version_id=version.id,
            scope="version",
            tombstone_generation=1,
            request_id=uuid.uuid4(),
            sealed_object_key="sealed/tombstones/guarded.json",
            sealed_hash="d" * 64,
            created_by_subject="admin",
        )
        async_session.add(tombstone)
        await async_session.flush()

        with pytest.raises(DBAPIError, match="deletion tombstones are immutable"):
            async with async_session.begin_nested():
                await async_session.execute(
                    text("UPDATE deletion_tombstone SET scope = 'document' WHERE id = :id"),
                    {"id": tombstone.id},
                )
        with pytest.raises(DBAPIError, match="deletion tombstones are immutable"):
            async with async_session.begin_nested():
                await async_session.execute(
                    text("DELETE FROM deletion_tombstone WHERE id = :id"),
                    {"id": tombstone.id},
                )


@pytest.mark.asyncio
class TestModelInstantiation:
    """Verify models can be created and persisted to PostgreSQL."""

    async def test_create_policy_and_declared_type(self, async_session):
        """PolicyRevision and DeclaredType can be persisted."""
        policy = PolicyRevision(
            id=uuid.uuid4(),
            policy_kind="authorization",
            stable_key="default",
            revision=1,
            status=PolicyStatus.ACTIVE,
            body={"rules": []},
            body_sha256="a" * 64,
            created_by_subject="admin",
        )
        async_session.add(policy)
        await async_session.flush()

        declared = DeclaredType(
            id=uuid.uuid4(),
            stable_key="contract",
            active_policy_revision_id=policy.id,
        )
        async_session.add(declared)
        await async_session.flush()
        assert declared.active is True

    async def test_create_chunk_profile(self, async_session):
        """ChunkProfileRevision can be persisted."""
        profile = ChunkProfileRevision(
            id=uuid.uuid4(),
            profile_id=uuid.uuid4(),
            revision=1,
            status=PolicyStatus.ACTIVE,
            configuration={"algorithm": "recursive"},
            configuration_sha256="b" * 64,
        )
        async_session.add(profile)
        await async_session.flush()
        assert profile.revision == 1

    async def test_create_document_with_version(self, async_session):
        """Document and DocumentVersion can be persisted together."""
        policy = PolicyRevision(
            id=uuid.uuid4(),
            policy_kind="authorization",
            stable_key=f"default-{uuid.uuid4().hex[:8]}",
            revision=1,
            status=PolicyStatus.ACTIVE,
            body={},
            body_sha256="a" * 64,
            created_by_subject="admin",
        )
        async_session.add(policy)
        await async_session.flush()

        declared_type = DeclaredType(
            id=uuid.uuid4(),
            stable_key=f"contract-{uuid.uuid4().hex[:8]}",
            active_policy_revision_id=policy.id,
        )
        async_session.add(declared_type)
        await async_session.flush()

        chunk_profile = ChunkProfileRevision(
            id=uuid.uuid4(),
            profile_id=uuid.uuid4(),
            revision=1,
            status=PolicyStatus.ACTIVE,
            configuration={},
            configuration_sha256="c" * 64,
        )
        async_session.add(chunk_profile)
        await async_session.flush()

        doc = Document(
            id=uuid.uuid4(),
            title="Test Contract",
            declared_type_id=declared_type.id,
            created_by_subject="user@example.com",
        )
        async_session.add(doc)
        await async_session.flush()

        version = DocumentVersion(
            id=uuid.uuid4(),
            document_id=doc.id,
            version_number=1,
            original_filename="contract.pdf",
            declared_mime_family="application/pdf",
            byte_size=1024,
            content_sha256="d" * 64,
            quarantine_object_key="quarantine/test",
            created_by_subject="user@example.com",
            selected_chunk_profile_revision_id=chunk_profile.id,
        )
        async_session.add(version)
        await async_session.flush()
        assert version.version_number == 1

    async def test_create_label(self, async_session):
        """Label can be created with hierarchy."""
        parent = Label(
            id=uuid.uuid4(),
            stable_key=f"department-{uuid.uuid4().hex[:8]}",
            retention_class="standard",
        )
        async_session.add(parent)
        await async_session.flush()

        child = Label(
            id=uuid.uuid4(),
            stable_key=f"department/legal-{uuid.uuid4().hex[:8]}",
            parent_id=parent.id,
            retention_class="extended",
        )
        async_session.add(child)
        await async_session.flush()
        assert child.parent_id == parent.id

    async def test_create_identity_and_chat(self, async_session):
        """Identity, ChatSession, and ChatMessage can be created."""
        subject_key = f"user-{uuid.uuid4().hex[:8]}@example.com"
        subject = IdentitySubject(
            subject=subject_key,
            display_name="Test User",
            email=subject_key,
            active=True,
            scim_version="1",
            reconciled_at=datetime.now(UTC),
        )
        async_session.add(subject)
        await async_session.flush()

        session = ChatSession(
            id=uuid.uuid4(),
            subject=subject_key,
            retention_expires_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
        async_session.add(session)
        await async_session.flush()

        message = ChatMessage(
            id=uuid.uuid4(),
            session_id=session.id,
            role="user",
            content="What changed in the renewal clause?",
        )
        async_session.add(message)
        await async_session.flush()
        assert message.role == "user"

    async def test_create_audit_event(self, async_session):
        """AuditEvent and AuditEventIdentity can be created."""
        event_id = uuid.uuid4()
        now = datetime.now(UTC)

        identity = AuditEventIdentity(
            id=event_id,
            event_hash="g" * 64,
            event_time=now,
        )
        async_session.add(identity)
        await async_session.flush()

        event = AuditEvent(
            id=event_id,
            event_time=now,
            actor_subject="admin@example.com",
            action="document.created",
            resource_type="document",
            resource_id=str(uuid.uuid4()),
            details={"title": "Test"},
            event_hash="g" * 64,
        )
        async_session.add(event)
        await async_session.flush()
        assert event.action == "document.created"

    async def test_create_graph_entity_and_fact(self, async_session):
        """GraphEntity and GraphFact can be created with proper FKs."""
        # Prerequisites
        policy = PolicyRevision(
            id=uuid.uuid4(),
            policy_kind="auth",
            stable_key=f"default-{uuid.uuid4().hex[:8]}",
            revision=1,
            status=PolicyStatus.ACTIVE,
            body={},
            body_sha256="a" * 64,
            created_by_subject="admin",
        )
        async_session.add(policy)
        await async_session.flush()

        declared_type = DeclaredType(
            id=uuid.uuid4(),
            stable_key=f"contract-{uuid.uuid4().hex[:8]}",
            active_policy_revision_id=policy.id,
        )
        async_session.add(declared_type)
        await async_session.flush()

        chunk_profile = ChunkProfileRevision(
            id=uuid.uuid4(),
            profile_id=uuid.uuid4(),
            revision=1,
            status=PolicyStatus.ACTIVE,
            configuration={},
            configuration_sha256="c" * 64,
        )
        async_session.add(chunk_profile)
        await async_session.flush()

        doc = Document(
            id=uuid.uuid4(),
            title="Test",
            declared_type_id=declared_type.id,
            created_by_subject="user",
        )
        async_session.add(doc)
        await async_session.flush()

        version = DocumentVersion(
            id=uuid.uuid4(),
            document_id=doc.id,
            version_number=1,
            original_filename="test.pdf",
            declared_mime_family="application/pdf",
            byte_size=100,
            content_sha256="e" * 64,
            quarantine_object_key="q/test",
            created_by_subject="user",
            selected_chunk_profile_revision_id=chunk_profile.id,
        )
        async_session.add(version)
        await async_session.flush()

        chunk = DocumentChunk(
            id=uuid.uuid4(),
            version_id=version.id,
            chunk_index=0,
            content="Test content",
            content_sha256="f" * 64,
            start_offset=0,
            end_offset=12,
            token_count=3,
            profile_revision_id=chunk_profile.id,
            embedding_model_digest="bge-m3-v1",
        )
        async_session.add(chunk)
        await async_session.flush()

        model_route = ModelRouteRevision(
            id=uuid.uuid4(),
            role="EXTRACT",
            revision=1,
            status=PolicyStatus.ACTIVE,
            route_configuration={"model": "qwen2.5"},
        )
        async_session.add(model_route)
        await async_session.flush()

        entity_a = GraphEntity(
            id=uuid.uuid4(),
            entity_type="Company",
            normalized_key=f"acme_corp_{uuid.uuid4().hex[:8]}",
            display_value="Acme Corp",
        )
        entity_b = GraphEntity(
            id=uuid.uuid4(),
            entity_type="Company",
            normalized_key=f"globex_{uuid.uuid4().hex[:8]}",
            display_value="Globex",
        )
        async_session.add_all([entity_a, entity_b])
        await async_session.flush()

        fact = GraphFact(
            id=uuid.uuid4(),
            subject_entity_id=entity_a.id,
            predicate_key="partner_of",
            object_entity_id=entity_b.id,
            object_normalized_key=entity_b.normalized_key,
            source_chunk_id=chunk.id,
            source_version_id=version.id,
            extraction_route_revision_id=model_route.id,
            confidence=Decimal("0.950"),
        )
        async_session.add(fact)
        await async_session.flush()
        assert fact.confidence == Decimal("0.950")


@pytest.mark.asyncio
class TestUniqueConstraints:
    """Verify unique constraints are enforced on PostgreSQL."""

    async def test_duplicate_label_stable_key_rejected(self, async_session):
        """Two labels with the same stable_key should fail."""
        key = f"department-{uuid.uuid4().hex[:8]}"
        label1 = Label(
            id=uuid.uuid4(),
            stable_key=key,
            retention_class="standard",
        )
        label2 = Label(
            id=uuid.uuid4(),
            stable_key=key,
            retention_class="extended",
        )
        async_session.add(label1)
        await async_session.flush()
        async_session.add(label2)
        with pytest.raises(IntegrityError):
            await async_session.flush()

    async def test_duplicate_entity_type_key_rejected(self, async_session):
        """Two graph entities with the same (type, normalized_key) should fail."""
        key = f"acme_{uuid.uuid4().hex[:8]}"
        e1 = GraphEntity(
            id=uuid.uuid4(),
            entity_type="Company",
            normalized_key=key,
            display_value="Acme",
        )
        e2 = GraphEntity(
            id=uuid.uuid4(),
            entity_type="Company",
            normalized_key=key,
            display_value="ACME Corp",
        )
        async_session.add(e1)
        await async_session.flush()
        async_session.add(e2)
        with pytest.raises(IntegrityError):
            await async_session.flush()

    async def test_duplicate_policy_revision_rejected(self, async_session):
        """Two policy revisions with same (kind, key, revision) should fail."""
        key = f"default-{uuid.uuid4().hex[:8]}"
        pr1 = PolicyRevision(
            id=uuid.uuid4(),
            policy_kind="auth",
            stable_key=key,
            revision=1,
            status=PolicyStatus.ACTIVE,
            body={},
            body_sha256="a" * 64,
            created_by_subject="admin",
        )
        pr2 = PolicyRevision(
            id=uuid.uuid4(),
            policy_kind="auth",
            stable_key=key,
            revision=1,
            status=PolicyStatus.DRAFT,
            body={},
            body_sha256="b" * 64,
            created_by_subject="admin",
        )
        async_session.add(pr1)
        await async_session.flush()
        async_session.add(pr2)
        with pytest.raises(IntegrityError):
            await async_session.flush()


@pytest.mark.asyncio
class TestCheckConstraints:
    """Verify PostgreSQL CHECK constraints are enforced."""

    async def test_empty_title_rejected(self, async_session):
        """Document with empty title should fail title_length CHECK."""
        policy = PolicyRevision(
            id=uuid.uuid4(),
            policy_kind="auth",
            stable_key=f"default-{uuid.uuid4().hex[:8]}",
            revision=1,
            status=PolicyStatus.ACTIVE,
            body={},
            body_sha256="a" * 64,
            created_by_subject="admin",
        )
        async_session.add(policy)
        await async_session.flush()

        dt = DeclaredType(
            id=uuid.uuid4(),
            stable_key=f"type-{uuid.uuid4().hex[:8]}",
            active_policy_revision_id=policy.id,
        )
        async_session.add(dt)
        await async_session.flush()

        doc = Document(
            id=uuid.uuid4(),
            title="",  # Empty — violates CHECK
            declared_type_id=dt.id,
            created_by_subject="user",
        )
        async_session.add(doc)
        with pytest.raises(IntegrityError):
            await async_session.flush()

    async def test_negative_byte_size_rejected(self, async_session):
        """DocumentVersion with negative byte_size should fail CHECK."""
        policy = PolicyRevision(
            id=uuid.uuid4(),
            policy_kind="auth",
            stable_key=f"default-{uuid.uuid4().hex[:8]}",
            revision=1,
            status=PolicyStatus.ACTIVE,
            body={},
            body_sha256="a" * 64,
            created_by_subject="admin",
        )
        async_session.add(policy)
        await async_session.flush()

        dt = DeclaredType(
            id=uuid.uuid4(),
            stable_key=f"type-{uuid.uuid4().hex[:8]}",
            active_policy_revision_id=policy.id,
        )
        async_session.add(dt)
        await async_session.flush()

        cp = ChunkProfileRevision(
            id=uuid.uuid4(),
            profile_id=uuid.uuid4(),
            revision=1,
            status=PolicyStatus.ACTIVE,
            configuration={},
            configuration_sha256="x" * 64,
        )
        async_session.add(cp)
        await async_session.flush()

        doc = Document(
            id=uuid.uuid4(),
            title="Test",
            declared_type_id=dt.id,
            created_by_subject="user",
        )
        async_session.add(doc)
        await async_session.flush()

        version = DocumentVersion(
            id=uuid.uuid4(),
            document_id=doc.id,
            version_number=1,
            original_filename="test.pdf",
            declared_mime_family="application/pdf",
            byte_size=-1,  # Negative — violates CHECK
            content_sha256="z" * 64,
            quarantine_object_key="q/test",
            created_by_subject="user",
            selected_chunk_profile_revision_id=cp.id,
        )
        async_session.add(version)
        with pytest.raises(IntegrityError):
            await async_session.flush()

    async def test_invalid_confidence_rejected(self, async_session):
        """GraphFact with confidence > 1 should fail CHECK."""
        entity = GraphEntity(
            id=uuid.uuid4(),
            entity_type="Company",
            normalized_key=f"test-{uuid.uuid4().hex[:8]}",
            display_value="Test",
        )
        async_session.add(entity)
        await async_session.flush()

        # Need prerequisites for the fact
        policy = PolicyRevision(
            id=uuid.uuid4(),
            policy_kind="auth",
            stable_key=f"default-{uuid.uuid4().hex[:8]}",
            revision=1,
            status=PolicyStatus.ACTIVE,
            body={},
            body_sha256="a" * 64,
            created_by_subject="admin",
        )
        async_session.add(policy)
        await async_session.flush()

        dt = DeclaredType(
            id=uuid.uuid4(),
            stable_key=f"type-{uuid.uuid4().hex[:8]}",
            active_policy_revision_id=policy.id,
        )
        async_session.add(dt)
        await async_session.flush()

        cp = ChunkProfileRevision(
            id=uuid.uuid4(),
            profile_id=uuid.uuid4(),
            revision=1,
            status=PolicyStatus.ACTIVE,
            configuration={},
            configuration_sha256="c" * 64,
        )
        async_session.add(cp)
        await async_session.flush()

        doc = Document(
            id=uuid.uuid4(),
            title="Test",
            declared_type_id=dt.id,
            created_by_subject="user",
        )
        async_session.add(doc)
        await async_session.flush()

        version = DocumentVersion(
            id=uuid.uuid4(),
            document_id=doc.id,
            version_number=1,
            original_filename="test.pdf",
            declared_mime_family="application/pdf",
            byte_size=100,
            content_sha256="e" * 64,
            quarantine_object_key="q/test",
            created_by_subject="user",
            selected_chunk_profile_revision_id=cp.id,
        )
        async_session.add(version)
        await async_session.flush()

        chunk = DocumentChunk(
            id=uuid.uuid4(),
            version_id=version.id,
            chunk_index=0,
            content="Test content",
            content_sha256="f" * 64,
            start_offset=0,
            end_offset=12,
            token_count=3,
            profile_revision_id=cp.id,
            embedding_model_digest="bge-m3-v1",
        )
        async_session.add(chunk)
        await async_session.flush()

        mr = ModelRouteRevision(
            id=uuid.uuid4(),
            role="EXTRACT",
            revision=1,
            status=PolicyStatus.ACTIVE,
            route_configuration={},
        )
        async_session.add(mr)
        await async_session.flush()

        fact = GraphFact(
            id=uuid.uuid4(),
            subject_entity_id=entity.id,
            predicate_key="related_to",
            object_entity_id=None,
            object_normalized_key="other",
            source_chunk_id=chunk.id,
            source_version_id=version.id,
            extraction_route_revision_id=mr.id,
            confidence=Decimal("1.500"),  # > 1 — violates CHECK
        )
        async_session.add(fact)
        with pytest.raises(IntegrityError):
            await async_session.flush()
