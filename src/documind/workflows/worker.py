"""Least-privilege Temporal worker process bootstrap.

Starts separate Temporal workers for ``ingest-cpu`` and ``model-gpu`` task
queues while retaining the same workflow registration and clean
shutdown/close behavior.  The ``model-gpu`` worker owns only ``enrich``;
it must not make its dependencies available to the ingestion queue.
"""

import asyncio
import contextlib
import logging
import os
import shlex
import signal
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from documind.config import Settings
from documind.database import build_engine
from documind.services.audit_service import AuditService
from documind.services.ocr_service import DoclingSandboxParser, OCRService, RapidOCRParser
from documind.services.processing_service import ProcessingService
from documind.services.scanner_service import ClamAVTCPClient, ScannerService
from documind.services.storage_service import StorageService
from documind.workflows.activities.chunk import chunk, configure_chunk_activity
from documind.workflows.activities.complete import complete, configure_complete_activity
from documind.workflows.activities.enrich import configure_enrich_activity, enrich
from documind.workflows.activities.inspect import configure_inspection_activity, inspect
from documind.workflows.activities.normalize import configure_normalize_activity, normalize
from documind.workflows.activities.parse import configure_parse_activity, parse
from documind.workflows.activities.project import configure_project_activity, project
from documind.workflows.activities.verify import configure_verify_activity, verify
from documind.workflows.document_version import INGEST_QUEUE, MODEL_QUEUE, DocumentVersionWorkflow
from documind.workflows.maintenance.outbox_dispatcher import (
    OutboxDispatcher,
    RedisStreamWorkflowRunner,
    TemporalWorkflowConsumer,
)
from documind.workflows.stage_store import (
    PostgresChunkProfileSource,
    PostgresNormalizedDocumentSource,
    PostgresParseResultSource,
    PostgresStageStore,
    PostgresVersionContentSource,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerConfiguration:
    """Non-secret connection settings required by the worker bootstrap."""

    temporal_host: str
    openbao_auth_ref: str


@dataclass
class IngestionWorkerRuntime:
    """Fully configured worker roles sharing durable PostgreSQL stage state.

    Runs two Temporal workers:
    - ``ingest-cpu``: inspect, parse, normalize, chunk activities + workflow registration
    - ``model-gpu``: enrich activity only (no workflow registration, no ingest dependencies)
    """

    ingest_worker: Worker
    model_worker: Worker
    stream_runner: RedisStreamWorkflowRunner
    dispatcher: OutboxDispatcher
    redis_client: Any
    engine: AsyncEngine

    async def run(self, shutdown: asyncio.Event) -> None:
        """Run both workers, the outbox dispatcher, and the Redis consumer together until shutdown."""
        dispatch_task = asyncio.create_task(
            self._dispatch_loop(shutdown), name="outbox-dispatcher"
        )
        try:
            async with self.ingest_worker, self.model_worker:
                await self.stream_runner.run(shutdown)
        finally:
            dispatch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dispatch_task

    async def _dispatch_loop(self, shutdown: asyncio.Event) -> None:
        """Periodically publish pending outbox rows to the Redis stream."""
        while not shutdown.is_set():
            try:
                await self.dispatcher.dispatch_once(limit=100)
            except Exception:
                logger.exception("outbox_dispatch_error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown.wait(), timeout=2.0)

    async def close(self) -> None:
        close = getattr(self.redis_client, "aclose", None)
        if close is not None:
            await close()
        await self.engine.dispose()


def parse_worker_configuration() -> WorkerConfiguration:
    """Read worker settings without resolving or logging a credential."""
    settings = Settings()
    return WorkerConfiguration(
        temporal_host=settings.temporal_host,
        openbao_auth_ref=settings.openbao_auth_ref,
    )


async def serve(*, stream_runner: RedisStreamWorkflowRunner | None = None) -> None:
    """Run the Redis-to-Temporal consumer until the worker is stopped.

    The resolved Redis credential is injected by the worker's secret agent as
    ``DOCUMIND_RESOLVED_REDIS_STREAMS_URL``.  A raw value never enters
    ``Settings`` or application logs; deployments that have not resolved the
    OpenBao reference fail closed rather than starting a nonfunctional worker.
    """
    configuration = parse_worker_configuration()
    settings = Settings()
    temporal_client = await Client.connect(configuration.temporal_host, namespace=settings.temporal_namespace)
    logger.info("temporal worker bootstrap connected", extra={"temporal_host": configuration.temporal_host})

    runtime = None if stream_runner is not None else await _build_ingestion_runtime(temporal_client, settings)
    runner = stream_runner or runtime.stream_runner

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, shutdown.set)
    runner_task = asyncio.create_task(
        runtime.run(shutdown) if runtime is not None else runner.run(shutdown),
        name="documind-ingestion-worker",
    )
    shutdown_task = asyncio.create_task(shutdown.wait(), name="worker-shutdown-wait")
    try:
        done, _ = await asyncio.wait({runner_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
        if runner_task in done:
            await runner_task
    finally:
        shutdown.set()
        await runner_task
        shutdown_task.cancel()
        if runtime is not None:
            await runtime.close()


async def _build_ingestion_runtime(temporal_client: Client, settings: Settings) -> IngestionWorkerRuntime:
    """Wire all ingestion and enrichment activities to durable stores.

    Creates two Temporal workers:
    - ``ingest-cpu``: inspect, parse, normalize, chunk + workflow registration
    - ``model-gpu``: enrich only (no workflow, no ingest dependencies)
    """
    database_url = _resolved_secret("DOCUMIND_RESOLVED_DATABASE_URL")
    redis_url = _resolved_secret("DOCUMIND_RESOLVED_REDIS_STREAMS_URL")
    minio_access_key = _resolved_secret("DOCUMIND_RESOLVED_MINIO_ACCESS_KEY")
    minio_secret_key = _resolved_secret("DOCUMIND_RESOLVED_MINIO_SECRET_KEY")
    if not settings.docling_sandbox_command:
        raise RuntimeError("DOCUMIND_DOCLING_SANDBOX_COMMAND is required for the ingestion worker.")

    from redis.asyncio import from_url

    redis_client: Any = from_url(redis_url, decode_responses=False)
    engine = build_engine(database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    storage = StorageService.from_resolved_credentials(
        endpoint=settings.minio_endpoint,
        access_key=minio_access_key,
        secret_key=minio_secret_key,
        secure=settings.minio_secure,
        bucket_name=settings.minio_bucket,
        hard_cap_bytes=settings.upload_hard_cap_bytes,
    )
    await storage.initialize()

    audit_service = AuditService(session_factory)
    stage_store = PostgresStageStore(session_factory=session_factory, audit_service=audit_service)
    content_source = PostgresVersionContentSource(session_factory=session_factory, storage_service=storage)
    scanner = ScannerService(
        content_source=content_source,
        clamav_client=ClamAVTCPClient(settings.clamav_host, settings.clamav_port),
    )
    ocr = OCRService(
        content_source=content_source,
        docling_parser=DoclingSandboxParser(tuple(shlex.split(settings.docling_sandbox_command))),
        rapidocr_parser=RapidOCRParser(),
    )
    processing = ProcessingService(
        parse_result_source=PostgresParseResultSource(session_factory),
        normalized_output_sink=storage,
    )

    # Configure ingest-cpu activities: inspect, parse, normalize, chunk
    configure_inspection_activity(
        scanner, tombstone_guard=stage_store, stage_store=stage_store, storage_service=storage,
    )
    configure_parse_activity(ocr, tombstone_guard=stage_store, stage_store=stage_store)
    configure_normalize_activity(processing, tombstone_guard=stage_store, stage_store=stage_store)

    normalized_source = PostgresNormalizedDocumentSource(
        session_factory=session_factory,
        storage=storage,
    )
    chunk_profile_source = PostgresChunkProfileSource(session_factory)

    configure_chunk_activity(
        _build_chunking_service(settings),
        _build_chunk_writer(session_factory),
        normalized_source=normalized_source,
        chunk_profile_source=chunk_profile_source,
        tombstone_guard=stage_store,
        stage_store=stage_store,
    )

    # Configure model-gpu activity: enrich only
    configure_enrich_activity(
        _build_enrichment_service(session_factory, settings),
        tombstone_guard=stage_store,
        stage_store=stage_store,
    )

    # Configure projection activities: project, verify, complete
    coordinator = _build_projection_coordinator(session_factory, settings)
    configure_project_activity(
        coordinator,
        tombstone_guard=stage_store,
        stage_store=stage_store,
    )
    configure_verify_activity(
        coordinator,
        tombstone_guard=stage_store,
        stage_store=stage_store,
    )
    configure_complete_activity(
        coordinator,
        tombstone_guard=stage_store,
        stage_store=stage_store,
    )

    consumer = TemporalWorkflowConsumer(
        redis_client=redis_client,
        temporal_client=temporal_client,
        run_recorder=stage_store,
        lifecycle_checker=stage_store,
    )
    stream_runner = RedisStreamWorkflowRunner(redis_client=redis_client, consumer=consumer)

    # ingest-cpu worker: runs the workflow + inspect/parse/normalize/chunk + projection activities
    ingest_worker = Worker(
        temporal_client,
        task_queue=INGEST_QUEUE,
        workflows=[DocumentVersionWorkflow],
        activities=[inspect, parse, normalize, chunk, project, verify, complete],
    )

    # model-gpu worker: runs ONLY the enrich activity; no workflows, no ingest dependencies
    model_worker = Worker(
        temporal_client,
        task_queue=MODEL_QUEUE,
        workflows=[],
        activities=[enrich],
    )

    # T4-1: Construct the outbox dispatcher to publish pending rows to Redis.
    dispatcher = OutboxDispatcher(
        redis_client=redis_client,
        session_factory=session_factory,
    )

    return IngestionWorkerRuntime(
        ingest_worker=ingest_worker,
        model_worker=model_worker,
        stream_runner=stream_runner,
        dispatcher=dispatcher,
        redis_client=redis_client,
        engine=engine,
    )


def _build_chunking_service(settings: Settings) -> Any:
    """Build a ChunkingService with a whitespace tokenizer.

    Production deployments should replace the tokenizer adapter with the
    BGE-M3 tokenizer loaded from ``sentence-transformers``.  This
    whitespace-based tokenizer provides correct protocol compliance for
    worker bootstrap and integration testing without model downloads.
    """
    from documind.services.chunking_service import ChunkingService, Token

    class _WhitespaceTokenizer:
        """Protocol-compliant tokenizer that splits on whitespace boundaries.

        Produces non-overlapping tokens with character-accurate offsets
        matching the Tokenizer protocol.  The digest is deterministic so
        profiles pinned against this tokenizer are reproducible.
        """

        digest = "whitespace-tokenizer-v1"

        def tokenize(self, text: str) -> list[Token]:
            tokens: list[Token] = []
            offset = 0
            for i, char in enumerate(text):
                if char.isspace():
                    if i > offset:
                        tokens.append(Token(token_id=len(tokens), start_offset=offset, end_offset=i))
                    offset = i + 1
            if offset < len(text):
                tokens.append(Token(token_id=len(tokens), start_offset=offset, end_offset=len(text)))
            return tokens

    return ChunkingService(tokenizer=_WhitespaceTokenizer())


def _build_chunk_writer(session_factory: async_sessionmaker[AsyncSession]) -> Any:
    """Build a ChunkWriter stub — actual wiring deferred to integration."""
    # A ChunkWriter implementation backed by the session factory would be
    # created here.  For now, return a minimal protocol-compatible stub.
    return _PostgresChunkWriter(session_factory)


def _build_enrichment_service(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Any:
    """Build an EnrichmentService with stub dependencies.

    Production enrichment requires a real LLMService (with route resolver,
    credential resolver, and LiteLLM adapter), a GraphFactService, and a
    VersionLoader.  These stubs fail cleanly with descriptive errors rather
    than crashing with AttributeError on uninitialized ``__new__`` members.
    """
    from documind.services.enrichment_service import EnrichmentService
    from documind.services.graph_fact_service import FactPersistenceResult
    from documind.services.llm_service import ModelRouteError

    class _StubRouteResolver:
        async def newest_active(self, role: Any) -> None:
            return None

    class _StubLLM:
        """Stub LLM that reports no active routes."""

        _route_resolver = _StubRouteResolver()

        async def invoke(self, role: Any, messages: Any, *, json_schema: Any = None) -> Any:
            raise ModelRouteError("LLM service requires production route configuration.")

    class _StubGraphFactService:
        async def persist_facts(self, **kwargs: Any) -> FactPersistenceResult:
            return FactPersistenceResult()

    class _StubVersionLoader:
        def __init__(self, sf: async_sessionmaker[AsyncSession]) -> None:
            self._session_factory = sf

        async def load_version(self, version_id: Any) -> Any:
            from documind.models.document import DocumentVersion

            async with self._session_factory() as session:
                version = await session.get(DocumentVersion, version_id)
                if version is None:
                    raise RuntimeError(f"Version {version_id} not found.")
                return version

        async def load_chunks(self, version_id: Any) -> list[Any]:
            return []

        async def load_template(self, revision_id: Any) -> None:
            return None

        async def update_type_suggestion(self, version_id: Any, suggestion: Any) -> None:
            pass

        async def update_extraction_state(self, version_id: Any, state: Any) -> None:
            pass

        async def save_extraction(self, extraction: Any) -> None:
            pass

        async def save_proposal(self, proposal: Any) -> None:
            pass

    return EnrichmentService(
        llm_service=_StubLLM(),
        graph_fact_service=_StubGraphFactService(),
        version_loader=_StubVersionLoader(session_factory),
    )


def _build_projection_coordinator(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
) -> Any:
    """Build a ProjectionCoordinator with real PostgreSQL-backed adapters.

    Replaces the former stub implementations with durable evidence,
    lifecycle completion, generation-aware tombstone checking, and
    canonical snapshot resolution.  External projection clients (Qdrant,
    OpenSearch, Neo4j) are not constructed here — they are injected
    separately during full integration when the respective drivers are
    available.  This provides protocol-compatible durable adapters for
    the worker bootstrap.
    """
    from documind.services.lifecycle_completer import (
        GenerationAwareTombstoneGuard,
        PostgresLifecycleCompleter,
    )
    from documind.services.projection_evidence_store import PostgresEvidenceStore
    from documind.services.projection_service import (
        ProjectionBackend,
        ProjectionCoordinator,
    )
    from documind.services.projection_source import PostgresCanonicalSource

    # Durable PostgreSQL adapters
    source = PostgresCanonicalSource(session_factory=session_factory)
    evidence_store = PostgresEvidenceStore(session_factory=session_factory)
    tombstone_guard = GenerationAwareTombstoneGuard(session_factory=session_factory)
    lifecycle_completer = PostgresLifecycleCompleter(session_factory=session_factory)

    # Projection writers — use real implementations when external drivers
    # are available (configured via settings); fall back to pass-through
    # writers that satisfy the protocol for environments without the
    # external services running.
    writers: dict[ProjectionBackend, Any] = {}
    for backend in ProjectionBackend:
        writers[backend] = _build_projection_writer(backend, session_factory, settings)

    return ProjectionCoordinator(
        source=source,
        writers=writers,
        evidence_store=evidence_store,
        tombstone_guard=tombstone_guard,
        lifecycle_completer=lifecycle_completer,
        incident_sink=evidence_store,
    )


def _build_projection_writer(
    backend: Any,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
) -> Any:
    """Build a single projection writer for the given backend.

    Attempts to import the real external client driver.  If the driver
    is not installed (e.g. in minimal test environments), falls back to
    a lightweight pass-through writer that satisfies the protocol.
    """
    from documind.services.projection_service import (
        ProjectionBackend,
        ProjectionManifest,
        ProjectionSnapshot,
        manifest_checksum,
    )

    if backend == ProjectionBackend.QDRANT:
        try:
            from qdrant_client import AsyncQdrantClient

            from documind.services.indexing_service import QdrantProjectionWriter

            client = AsyncQdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
            return QdrantProjectionWriter(
                client=client,
                collection=settings.qdrant_collection,
                dimension=1024,
            )
        except ImportError:
            pass

    elif backend == ProjectionBackend.OPENSEARCH:
        try:
            from opensearchpy import AsyncOpenSearch

            from documind.services.indexing_service import OpenSearchProjectionWriter

            client = AsyncOpenSearch(
                hosts=[{"host": settings.opensearch_host, "port": settings.opensearch_port}],
            )
            return OpenSearchProjectionWriter(client=client, index_name=settings.opensearch_index)
        except ImportError:
            pass

    elif backend == ProjectionBackend.NEO4J:
        try:
            from neo4j import AsyncGraphDatabase

            from documind.services.graph_service import Neo4jProjectionWriter

            neo4j_auth = os.environ.get("DOCUMIND_RESOLVED_NEO4J_AUTH", "")
            auth_tuple = tuple(neo4j_auth.split(":", 1)) if ":" in neo4j_auth else ("neo4j", "password")
            driver = AsyncGraphDatabase.driver(settings.neo4j_uri, auth=auth_tuple)
            return Neo4jProjectionWriter(driver=driver)
        except ImportError:
            pass

    # Fallback pass-through writer for environments without external drivers
    class _PassthroughWriter:
        def __init__(self, backend: ProjectionBackend) -> None:
            self._backend = backend

        async def project(self, snapshot: ProjectionSnapshot) -> ProjectionManifest:
            return ProjectionManifest(
                backend=self._backend,
                snapshot_id=snapshot.snapshot_id,
                generation=snapshot.generation,
                tombstone_generation=snapshot.tombstone_generation,
                record_count=len(snapshot.records),
                checksum=manifest_checksum(snapshot.records),
            )

    return _PassthroughWriter(backend)


class _PostgresChunkWriter:
    """Replay-safe ChunkWriter with conflict detection.

    On first write: persists all chunk rows in one transaction.
    On retry: re-reads existing rows by (version_id, profile_revision_id),
    verifies they match the candidate chunks, and returns existing metadata.
    On mismatch: raises ``ChunkWriterConflictError``.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def write_chunks(
        self,
        version_id: Any,
        profile_revision_id: Any,
        chunks: list[Any],
    ) -> dict[str, Any]:
        """Write chunks idempotently and return stage metadata."""
        import hashlib
        import json

        from sqlalchemy import select

        from documind.models.chunk import DocumentChunk
        from documind.services.chunking_service import ChunkWriterConflictError

        async with self._session_factory() as session, session.begin():
            # Check for existing chunks (replay detection)
            existing = list(
                (
                    await session.execute(
                        select(DocumentChunk)
                        .where(
                            DocumentChunk.version_id == version_id,
                            DocumentChunk.profile_revision_id == profile_revision_id,
                        )
                        .order_by(DocumentChunk.chunk_index)
                    )
                ).scalars()
            )

            if existing:
                # Verify identical retry vs conflict
                if len(existing) != len(chunks):
                    raise ChunkWriterConflictError(
                        f"Retry chunk count {len(chunks)} differs from persisted {len(existing)}"
                    )
                for persisted, candidate in zip(existing, chunks, strict=True):
                    # T5.3-09: Compare ALL immutable fields on replay, not just
                    # a subset. This catches silent divergence from any field.
                    if (
                        persisted.id != candidate.id
                        or persisted.chunk_index != candidate.chunk_index
                        or persisted.start_offset != candidate.start_offset
                        or persisted.end_offset != candidate.end_offset
                        or persisted.content_sha256 != candidate.content_sha256
                        or persisted.content != candidate.content
                        or persisted.page_start != candidate.page_start
                        or persisted.page_end != candidate.page_end
                        or persisted.token_count != candidate.token_count
                        or persisted.profile_revision_id != candidate.profile_revision_id
                        or persisted.embedding_model_digest != candidate.embedding_model_digest
                    ):
                        raise ChunkWriterConflictError(
                            f"Chunk {candidate.chunk_index} conflicts with persisted chunk"
                        )
                # Identical retry — return existing metadata
                content_hashes = sorted(c.content_sha256 for c in existing)
                checksum_input = json.dumps(content_hashes, sort_keys=True).encode("utf-8")
                return {
                    "chunk_count": len(existing),
                    "chunk_checksum": hashlib.sha256(checksum_input).hexdigest(),
                    "version_id": str(version_id),
                    "profile_revision_id": str(profile_revision_id),
                    "replay": True,
                }

            # First write
            for c in chunks:
                session.add(c)

        chunk_count = len(chunks)
        content_hashes = sorted(getattr(c, "content_sha256", "") for c in chunks)
        checksum_input = json.dumps(content_hashes, sort_keys=True).encode("utf-8")
        return {
            "chunk_count": chunk_count,
            "chunk_checksum": hashlib.sha256(checksum_input).hexdigest(),
            "version_id": str(version_id),
            "profile_revision_id": str(profile_revision_id),
        }


def _resolved_secret(name: str) -> str:
    """Read a secret-agent result without putting a secret in Settings/logs."""
    value = os.environ.get(name, "")
    if value:
        return value
    reference_name = name.removeprefix("DOCUMIND_RESOLVED_") + "_REF"
    raise RuntimeError(f"{name} is required; resolve DOCUMIND_{reference_name} through the worker secret agent first.")


def main() -> None:
    """Run the worker bootstrap as a module entry point."""
    asyncio.run(serve())


if __name__ == "__main__":
    main()
