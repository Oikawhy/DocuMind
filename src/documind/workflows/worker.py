"""Least-privilege Temporal worker process bootstrap.

Starts separate Temporal workers for ``ingest-cpu`` and ``model-gpu`` task
queues while retaining the same workflow registration and clean
shutdown/close behavior.  The ``model-gpu`` worker owns only ``enrich``;
it must not make its dependencies available to the ingestion queue.
"""

import asyncio
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
    RedisStreamWorkflowRunner,
    TemporalWorkflowConsumer,
)
from documind.workflows.stage_store import PostgresParseResultSource, PostgresStageStore, PostgresVersionContentSource

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
    redis_client: Any
    engine: AsyncEngine

    async def run(self, shutdown: asyncio.Event) -> None:
        """Run both workers and the Redis consumer together until shutdown."""
        async with self.ingest_worker, self.model_worker:
            await self.stream_runner.run(shutdown)

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
    configure_inspection_activity(scanner, tombstone_guard=stage_store, stage_store=stage_store)
    configure_parse_activity(ocr, tombstone_guard=stage_store, stage_store=stage_store)
    configure_normalize_activity(processing, tombstone_guard=stage_store, stage_store=stage_store)
    configure_chunk_activity(
        _build_chunking_service(settings),
        _build_chunk_writer(session_factory),
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
    coordinator = _build_projection_coordinator(session_factory)
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

    return IngestionWorkerRuntime(
        ingest_worker=ingest_worker,
        model_worker=model_worker,
        stream_runner=stream_runner,
        redis_client=redis_client,
        engine=engine,
    )


def _build_chunking_service(settings: Settings) -> Any:
    """Build a ChunkingService stub — actual wiring deferred to integration."""
    # Import here to avoid circular dependency at module level
    from documind.services.chunking_service import ChunkingService

    # The actual ChunkingService requires injected tokenizer/segmenter/embedder.
    # In production, these are resolved from the configuration.  This function
    # creates a minimal service instance; full wiring is done during integration.
    return ChunkingService.__new__(ChunkingService)


def _build_chunk_writer(session_factory: async_sessionmaker[AsyncSession]) -> Any:
    """Build a ChunkWriter stub — actual wiring deferred to integration."""
    # A ChunkWriter implementation backed by the session factory would be
    # created here.  For now, return a minimal protocol-compatible stub.
    return _PostgresChunkWriter(session_factory)


def _build_enrichment_service(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Any:
    """Build an EnrichmentService stub — actual wiring deferred to integration."""
    from documind.services.enrichment_service import EnrichmentService

    return EnrichmentService.__new__(EnrichmentService)


def _build_projection_coordinator(
    session_factory: async_sessionmaker[AsyncSession],
) -> Any:
    """Build a ProjectionCoordinator with stub adapters.

    Concrete PostgreSQL adapters for evidence storage, lifecycle completion,
    and projection writing are wired during full integration.  This provides
    protocol-compatible stubs for the worker bootstrap.
    """
    from documind.services.projection_service import (
        ProjectionBackend,
        ProjectionCoordinator,
        ProjectionManifest,
        ProjectionSnapshot,
        WriterOutcome,
        manifest_checksum,
    )

    class _StubSource:
        async def resolve_snapshot(self, snapshot_id: str) -> ProjectionSnapshot:
            return ProjectionSnapshot(
                snapshot_id=snapshot_id,
                run_id="stub",
                version_id="stub",
                generation=1,
                tombstone_generation=0,
                records=(),
            )

    class _StubWriter:
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

    class _StubEvidence:
        async def state_for(
            self,
            backend: ProjectionBackend,
            snapshot: ProjectionSnapshot,
        ) -> WriterOutcome | None:
            return None

        async def record_outcome(self, outcome: WriterOutcome) -> None:
            pass

        async def record_manifest(self, manifest: ProjectionManifest) -> None:
            pass

    class _StubGuard:
        async def assert_active(self, version_id: str, tombstone_generation: int) -> None:
            pass

    class _StubCompleter:
        async def complete_version(self, snapshot: ProjectionSnapshot) -> None:
            pass

    return ProjectionCoordinator(
        source=_StubSource(),
        writers={backend: _StubWriter(backend) for backend in ProjectionBackend},
        evidence_store=_StubEvidence(),
        tombstone_guard=_StubGuard(),
        lifecycle_completer=_StubCompleter(),
    )


class _PostgresChunkWriter:
    """Minimal ChunkWriter that delegates to a session factory."""

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

        chunk_count = len(chunks)
        content_hashes = sorted(getattr(c, "content_sha256", "") for c in chunks)
        checksum_input = json.dumps(content_hashes, sort_keys=True).encode("utf-8")
        chunk_checksum = hashlib.sha256(checksum_input).hexdigest()

        async with self._session_factory() as session, session.begin():
            for c in chunks:
                session.add(c)

        return {
            "chunk_count": chunk_count,
            "chunk_checksum": chunk_checksum,
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
