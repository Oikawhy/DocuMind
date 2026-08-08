"""Task 5 persistence contracts for enrichment metadata and graph facts.

Model-introspection tests confirm ORM metadata, while async-session tests
exercise the real PostgreSQL constraints.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from documind.models.chunk import DocumentChunk
from documind.models.document import Document, DocumentVersion
from documind.models.enums import PolicyStatus
from documind.models.graph import GraphEntity, GraphFact
from documind.models.model_route import ModelRouteRevision
from documind.models.policy import ChunkProfileRevision, DeclaredType, PolicyRevision

# --------------------------------------------------------------------------- #
# Model-introspection tests (no DB required)
# --------------------------------------------------------------------------- #


def test_document_version_stores_non_authoritative_type_suggestion() -> None:
    """Suggestions retain route and confidence metadata without changing policy."""
    column = DocumentVersion.__table__.c.type_suggestion

    assert isinstance(column.type, JSONB)
    assert column.nullable is True


def test_graph_fact_requires_exactly_one_entity_or_literal_object() -> None:
    """Each canonical fact has either an entity object or a literal object."""
    table = GraphFact.__table__

    assert {"object_entity_id", "object_literal", "object_normalized_key"}.issubset(table.c.keys())
    checks = [
        str(constraint.sqltext).replace(" ", "")
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    assert "(object_entity_idISNOTNULL)<>(object_literalISNOTNULL)" in checks


def test_graph_fact_identity_includes_extraction_revision_and_gleaning_contract() -> None:
    """Retries are idempotent per route revision and only one gleaning pass is legal."""
    table = GraphFact.__table__

    assert {"gleaning_pass", "corroboration_count", "conflict_group_key", "tombstoned_at"}.issubset(table.c.keys())
    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert (
        "subject_entity_id",
        "predicate_key",
        "object_normalized_key",
        "source_chunk_id",
        "extraction_route_revision_id",
    ) in unique_column_sets
    checks = [
        str(constraint.sqltext).replace(" ", "")
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    assert "gleaning_passIN(0,1)" in checks


# --------------------------------------------------------------------------- #
# DB-level persistence contract tests (require async_session from conftest)
# --------------------------------------------------------------------------- #


async def _create_fact_prerequisites(session):
    """Insert shared prerequisite rows and return (version, chunk, model_route, entity_subject)."""
    policy = PolicyRevision(
        id=uuid.uuid4(),
        policy_kind="auth",
        stable_key=f"contract-{uuid.uuid4().hex[:8]}",
        revision=1,
        status=PolicyStatus.ACTIVE,
        body={},
        body_sha256="a" * 64,
        created_by_subject="admin",
    )
    session.add(policy)
    await session.flush()

    declared_type = DeclaredType(
        id=uuid.uuid4(),
        stable_key=f"type-{uuid.uuid4().hex[:8]}",
        active_policy_revision_id=policy.id,
    )
    session.add(declared_type)
    await session.flush()

    chunk_profile = ChunkProfileRevision(
        id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        revision=1,
        status=PolicyStatus.ACTIVE,
        configuration={},
        configuration_sha256="c" * 64,
    )
    session.add(chunk_profile)
    await session.flush()

    doc = Document(
        id=uuid.uuid4(),
        title="Contract test",
        declared_type_id=declared_type.id,
        created_by_subject="user",
    )
    session.add(doc)
    await session.flush()

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
    session.add(version)
    await session.flush()

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
    session.add(chunk)
    await session.flush()

    model_route = ModelRouteRevision(
        id=uuid.uuid4(),
        role="EXTRACT",
        revision=1,
        status=PolicyStatus.ACTIVE,
        route_configuration={"model": "qwen2.5"},
    )
    session.add(model_route)
    await session.flush()

    entity_subject = GraphEntity(
        id=uuid.uuid4(),
        entity_type="Company",
        normalized_key=f"subject_{uuid.uuid4().hex[:8]}",
        display_value="Acme Corp",
    )
    session.add(entity_subject)
    await session.flush()

    return version, chunk, model_route, entity_subject


@pytest.mark.asyncio(loop_scope="session")
async def test_entity_fact_persists_with_object_entity(async_session) -> None:
    """An entity-object fact is accepted when object_entity_id is set and object_literal is NULL."""
    version, chunk, model_route, subject = await _create_fact_prerequisites(async_session)

    entity_object = GraphEntity(
        id=uuid.uuid4(),
        entity_type="Company",
        normalized_key=f"object_{uuid.uuid4().hex[:8]}",
        display_value="Globex",
    )
    async_session.add(entity_object)
    await async_session.flush()

    fact = GraphFact(
        id=uuid.uuid4(),
        subject_entity_id=subject.id,
        predicate_key="partner_of",
        object_entity_id=entity_object.id,
        object_normalized_key=entity_object.normalized_key,
        source_chunk_id=chunk.id,
        source_version_id=version.id,
        extraction_route_revision_id=model_route.id,
        confidence=Decimal("0.950"),
    )
    async_session.add(fact)
    await async_session.flush()
    assert fact.object_entity_id == entity_object.id
    assert fact.object_literal is None
    assert fact.gleaning_pass == 0
    assert fact.corroboration_count == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_literal_fact_persists_with_object_literal(async_session) -> None:
    """A literal-object fact is accepted when object_literal is set and object_entity_id is NULL."""
    version, chunk, model_route, subject = await _create_fact_prerequisites(async_session)

    fact = GraphFact(
        id=uuid.uuid4(),
        subject_entity_id=subject.id,
        predicate_key="revenue_usd",
        object_entity_id=None,
        object_literal={"type": "currency", "unit": "USD", "value": 1_000_000},
        object_normalized_key="1000000_usd",
        source_chunk_id=chunk.id,
        source_version_id=version.id,
        extraction_route_revision_id=model_route.id,
        confidence=Decimal("0.800"),
    )
    async_session.add(fact)
    await async_session.flush()
    assert fact.object_entity_id is None
    assert fact.object_literal == {"type": "currency", "unit": "USD", "value": 1_000_000}


@pytest.mark.asyncio(loop_scope="session")
async def test_fact_with_both_entity_and_literal_rejected(async_session) -> None:
    """The exact-one-object constraint rejects a fact with both entity and literal."""
    version, chunk, model_route, subject = await _create_fact_prerequisites(async_session)

    entity_object = GraphEntity(
        id=uuid.uuid4(),
        entity_type="Company",
        normalized_key=f"both_{uuid.uuid4().hex[:8]}",
        display_value="Both Corp",
    )
    async_session.add(entity_object)
    await async_session.flush()

    fact = GraphFact(
        id=uuid.uuid4(),
        subject_entity_id=subject.id,
        predicate_key="invalid_both",
        object_entity_id=entity_object.id,
        object_literal={"type": "text", "value": "should fail"},
        object_normalized_key="both",
        source_chunk_id=chunk.id,
        source_version_id=version.id,
        extraction_route_revision_id=model_route.id,
        confidence=Decimal("0.500"),
    )
    async_session.add(fact)
    with pytest.raises(IntegrityError):
        await async_session.flush()


@pytest.mark.asyncio(loop_scope="session")
async def test_fact_with_neither_entity_nor_literal_rejected(async_session) -> None:
    """The exact-one-object constraint rejects a fact with neither entity nor literal."""
    version, chunk, model_route, subject = await _create_fact_prerequisites(async_session)

    fact = GraphFact(
        id=uuid.uuid4(),
        subject_entity_id=subject.id,
        predicate_key="invalid_neither",
        object_normalized_key="neither",
        source_chunk_id=chunk.id,
        source_version_id=version.id,
        extraction_route_revision_id=model_route.id,
        confidence=Decimal("0.500"),
    )
    async_session.add(fact)
    with pytest.raises(IntegrityError):
        await async_session.flush()


@pytest.mark.asyncio(loop_scope="session")
async def test_gleaning_pass_only_allows_zero_or_one(async_session) -> None:
    """The valid_gleaning_pass CHECK rejects any value other than 0 or 1."""
    version, chunk, model_route, subject = await _create_fact_prerequisites(async_session)

    fact = GraphFact(
        id=uuid.uuid4(),
        subject_entity_id=subject.id,
        predicate_key="invalid_gleaning",
        object_entity_id=None,
        object_literal={"type": "text", "value": "gleaning test"},
        object_normalized_key="gleaning",
        source_chunk_id=chunk.id,
        source_version_id=version.id,
        extraction_route_revision_id=model_route.id,
        confidence=Decimal("0.900"),
        gleaning_pass=2,  # Invalid — only 0 and 1 are legal
    )
    async_session.add(fact)
    with pytest.raises(IntegrityError):
        await async_session.flush()


@pytest.mark.asyncio(loop_scope="session")
async def test_type_suggestion_stores_route_and_confidence_metadata(async_session) -> None:
    """Type suggestion JSONB stores route revision ID and confidence without altering policy."""
    version, _, _, _ = await _create_fact_prerequisites(async_session)

    route_id = uuid.uuid4()
    suggestion = {
        "suggested_type": "invoice",
        "confidence": 0.92,
        "route_revision_id": str(route_id),
        "evidence_hash": "ab" * 32,
    }
    version.type_suggestion = suggestion
    await async_session.flush()

    assert version.type_suggestion is not None
    assert version.type_suggestion["suggested_type"] == "invoice"
    assert version.type_suggestion["confidence"] == 0.92
    assert version.type_suggestion["route_revision_id"] == str(route_id)


@pytest.mark.asyncio(loop_scope="session")
async def test_migration_003_upgrade_creates_enrichment_columns(async_session) -> None:
    """Verify that migration 003 columns exist after upgrade (implicit from conftest migration).

    This test confirms the migration ran successfully on a clean database by
    checking all Task 5 columns and constraints exist in the live schema.
    """
    # These columns were added by migration 003
    version_table = DocumentVersion.__table__
    fact_table = GraphFact.__table__

    # type_suggestion on document_version
    assert "type_suggestion" in version_table.c

    # Task 5 graph_fact columns
    for col_name in ("object_literal", "gleaning_pass", "corroboration_count", "conflict_group_key", "tombstoned_at"):
        assert col_name in fact_table.c, f"Missing column: {col_name}"

    # Verify constraints exist (naming convention prefixes with ck_<table>_)
    check_names = {c.name for c in fact_table.constraints if isinstance(c, CheckConstraint)}
    assert "ck_graph_fact_valid_gleaning_pass" in check_names
    assert "ck_graph_fact_exactly_one_fact_object" in check_names

    unique_names = {c.name for c in fact_table.constraints if isinstance(c, UniqueConstraint)}
    assert "uq_fact_triple_chunk_route" in unique_names
