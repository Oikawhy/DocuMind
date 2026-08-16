# Task 3–4 implementation gap audit

Scope: Task 3 and Task 4 in `docs/plans/implement.md`, checked against the current working tree on 2026-08-16. This report records confirmed implementation defects, missing required behavior, and plan-required test gaps only. It intentionally contains no remediation proposals.

The focused Task 3–4 test selection passes, but several findings below are unexercised production paths or crash/safety cases.

## Task 3 — MinIO storage, admission, and outbox

### T3-01 — The accepted-original lifecycle is not implemented

`StorageService.move_to_accepted()` and `accepted_key()` exist, but nothing calls them. `DocumentVersion.accepted_object_key` is never assigned. The worker content source always reads `quarantine_object_key`, including after a successful inspection.

Impact: documents which pass inspection remain in quarantine indefinitely; the required `originals/{document_uuid}/{version_number}/{sha256}` object layout is never realized.

Evidence: `src/documind/services/storage_service.py:114-123,176-178`; `src/documind/models/document.py:76`; `src/documind/workflows/stage_store.py:461-474`. Repository references to `move_to_accepted`, `accepted_key`, and `accepted_object_key` contain only their definitions/model field and the storage unit test.

### T3-02 — The 100 MiB default admission limit is enforced only after the entire upload reaches MinIO

The API composition passes `upload_default_max_bytes` (100 MiB) as `DocumentService.max_upload_bytes`, but storage receives the 500 MiB hard cap. `admit_document()` and `admit_version()` first stream the whole object and only then compare `byte_size` to the 100 MiB maximum and delete it.

Impact: uploads between 100 MiB and 500 MiB consume transfer, object-storage, and scanner-adjacent capacity before the default admission limit rejects them, rather than enforcing the default limit in the API streaming reader as required by §5.3.

Evidence: `src/documind/main.py:114-131`; `src/documind/config.py:83-85`; `src/documind/domain/document_service.py:181-189,306-313`; `src/documind/services/storage_service.py:94-112`.

### T3-03 — Object Lock is enabled on the bucket but sealed objects have no WORM retention behavior

`initialize()` creates an object-lock-capable bucket, but `write_sealed()` is an ordinary `put_object()` call with no retention or legal-hold settings. The same generic `remove_object()` method can remove any deterministic object key, including `sealed/` keys.

Impact: the sealed audit/tombstone layout is writable and deletable by the storage credential; bucket object-lock capability alone does not make these objects WORM.

Evidence: `src/documind/services/storage_service.py:84-92,137-147,160-170,205-214`.

### T3-04 — Version-admission idempotency does not preserve the idempotency-conflict contract after a uniqueness race

`admit_document()` re-reads the winning operation after an `IntegrityError`, which distinguishes an exact replay from a reused key. `admit_version()` instead maps every `IntegrityError` directly to `VERSION_CONFLICT`. Concurrent exact version admissions cannot return the winning operation, and a simultaneous reuse of the key for another target/body cannot surface the required `IDEMPOTENCY_CONFLICT` result.

Impact: the required 24-hour idempotency replay and subject + target + request-body binding are not consistently implemented by the version-admission endpoint under concurrency.

Evidence: `src/documind/domain/document_service.py:323-376` compared with `:234-253`; `src/documind/models/processing.py:38-64`. Existing version-idempotency coverage tests sequential duplicate content, not this uniqueness race: `tests/test_domain/test_document_service.py:406-427`.

### T3-05 — Authorized cursor pagination can terminate before later eligible documents

`list_documents()` applies SQL ordering and `limit + 1` before requested-label, lifecycle, and authorization filtering. If that database window contains no eligible documents, `page_documents` is empty and no `next_cursor` is issued even when older eligible documents exist.

Impact: `GET /v1/documents` can return an empty terminal page while documents matching the caller’s filters and authorization exist beyond the first unfiltered window.

Evidence: `src/documind/domain/document_service.py:409-479`, especially the pre-filter limit at `:423-424`, filters at `:426-462`, and cursor condition at `:475-479`.

### T3-06 — Required REST surfaces lack route-level regression coverage

`tests/test_api/test_documents.py` covers new-document admission, inaccessible document read, missing idempotency header, and operation polling only. It does not execute version admission, list pagination/filtering, version read, or document deletion through FastAPI.

Impact: most Task 3 REST contracts—including multipart, public error-envelope, authorization, and cursor behavior—have no route-level regression detection.

Evidence: `tests/test_api/test_documents.py:37-165`; implemented routes in `src/documind/api/documents.py:43-171`.

## Task 4 — workflow, inspection, and parser

### T4-01 — The configured Docling sandbox cannot be built or executed by the production worker

`Dockerfile.docling` copies `src/documind/parsers/` and starts `documind.parsers.docling_runner`, but neither the directory nor the module exists. The worker service is configured to execute that same nonexistent module locally. The separate `docling-sandbox` container has `network_mode: none`, no input mount, and no worker-to-container transport, so it is not used by `DoclingSandboxParser`.

Additionally, `Dockerfile.worker` installs only the base dependency set; Docling and RapidOCR are optional `ml` dependencies and are not installed in that image.

Impact: neither primary Docling parsing nor the local RapidOCR fallback is runnable in the configured worker deployment. The required least-privilege parser path is absent.

Evidence: `Dockerfile.docling:20-31`; `docker-compose.dev.yml:48-88`; `src/documind/workflows/worker.py:165-194`; `src/documind/services/ocr_service.py:68-110`; `Dockerfile.worker:18-29`; `pyproject.toml:37-53`. There is no `src/documind/parsers/` source file and no repository implementation of `docling_runner`.

### T4-02 — A crash between Redis dedupe reservation and Temporal start permanently loses an accepted event

The consumer first writes a seven-day Redis `SET NX` dedupe key and then starts the workflow. A process crash in between does not execute the exception handler that removes the reservation. On Redis redelivery the consumer returns `False` because the reservation exists, and `RedisStreamWorkflowRunner` acknowledges the entry regardless of that false result.

Impact: a valid accepted-version event can be acknowledged without ever starting `document-version/{version_uuid}`, leaving the admitted version without its processing workflow for up to—and after—the dedupe TTL.

Evidence: `src/documind/workflows/maintenance/outbox_dispatcher.py:156-190,243-260,263-289`. Existing tests cover ordinary duplicate delivery, not this crash window: `tests/test_workflows/test_outbox_dispatcher.py:116-143`.

### T4-03 — A crash after Temporal starts but before workflow-run recording leaves durable processing state stale

The Temporal workflow is started before `record_workflow_start()`. If the process stops after a successful start and before that database write, the Redis dedupe reservation prevents a later consumer from retrying the recorder. `ProcessingRun.temporal_run_id` remains `pending` and its state remains `accepted` even though the Temporal workflow exists or runs.

Impact: the durable workflow/run record required for stage tracking can permanently disagree with Temporal execution.

Evidence: start/record ordering in `src/documind/workflows/maintenance/outbox_dispatcher.py:173-190`; durable state update in `src/documind/workflows/stage_store.py:95-104`; initial `pending` values in `src/documind/domain/document_service.py:707-714`.

### T4-04 — The ClamAV verdict check is not fail-closed

`ScannerService` accepts any verdict that contains the substring `OK` and does not contain `FOUND`. A malformed or negative response such as `stream: NOT OK` meets those conditions and is treated as safe.

Impact: an unusable scanner response can pass inspection instead of producing the required safe scanner failure outcome.

Evidence: `src/documind/services/scanner_service.py:114-117,171-192`.

### T4-05 — PDF encryption checks can be bypassed by valid PDFs whose `/Encrypt` entry is not in the first kilobyte

The PDF preflight only searches `payload[:1024]` for `/Encrypt`. In valid PDFs, the encryption dictionary reference normally occurs in the trailer near the end of the file, so an encrypted PDF with a sufficiently large prefix is not rejected by this check.

Impact: the required rejection of encrypted PDFs is not reliably enforced before parsing.

Evidence: `src/documind/services/scanner_service.py:336-371`; required encrypted-PDF rejection in `docs/specs/2026-07-29-documind-self-hosted-architecture-design.md:463`.

### T4-06 — Image admission limits do not meet the required safe-file policy

The implemented pixel limit is 200 million rather than the specified 100 million. The preflight inspects only PNG and JPEG dimensions; TIFF—an admitted format—has no dimension, frame-count, or decode-memory check.

Impact: image inputs beyond the required pixel budget, and unbounded TIFF frames/dimensions, can proceed to parsing.

Evidence: `src/documind/services/scanner_service.py:313-317,423-482`; admitted MIME list at `:19-38`; required image limits in `docs/specs/2026-07-29-documind-self-hosted-architecture-design.md:462-478`.

### T4-07 — Markup active-content and remote-fetch defenses are incomplete and bypassable

The XML/HTML preflight scans only the first 8 KiB and detects only a narrow set of `DOCTYPE`/`ENTITY`/`SYSTEM` strings. It does not sanitize HTML active content, inspect remote URL directives, or detect equivalent directives later in a payload. OOXML external-relationship detection also accepts valid single-quoted XML attributes because it matches only the literal `TargetMode="External"`.

Impact: the required markup active-content, entity-expansion, and remote-fetch defenses are not fully implemented for admitted HTML/XML/Office documents.

Evidence: `src/documind/services/scanner_service.py:384-421`; the single-literal OOXML match at `:396-407`; required controls in `docs/specs/2026-07-29-documind-self-hosted-architecture-design.md:462-465`.

### T4-08 — RapidOCR operational unavailability is incorrectly terminal after a Docling content fallback

When Docling returns empty or low-confidence content, `docling_unavailable` remains false. If RapidOCR then raises `ParserUnavailableError`, `OCRService.parse()` returns an unsuccessful result rather than raising. `PostgresStageStore` turns every unsuccessful parse result into a non-retryable terminal failure, so the configured two parser attempts are skipped.

Impact: a temporary RapidOCR outage becomes a terminal parser failure whenever Docling completed but needed an OCR fallback.

Evidence: `src/documind/services/ocr_service.py:181-242`; `src/documind/workflows/stage_store.py:608-620`; parser retry configuration at `src/documind/workflows/document_version.py:151-160`.

### T4-09 — The normalization MinIO write is not protected by a tombstone check immediately before that write

The normalize activity checks lifecycle state before calling `ProcessingService.normalize()`, then the service writes the normalized object to MinIO. A tombstone can be created while normalization is running; the next tombstone check occurs only after the object write and then again when the stage record is persisted.

Impact: a deleted/tombstoned version can receive a newly written normalized artifact, contrary to the requirement for a tombstone check before every write.

Evidence: `src/documind/workflows/activities/normalize.py:43-62`; `src/documind/services/processing_service.py:86-117`; `src/documind/workflows/stage_store.py:362-383`.

### T4-10 — Activity heartbeats are not dependable while large synchronous work holds the event loop

The heartbeat task shares an event loop with synchronous archive preflight, ClamAV write-loop preparation, and the normalizer’s multi-pass Unicode/offset processing. These paths operate over the complete in-memory object/text without yielding. The ten-second heartbeat coroutine cannot execute while such work blocks the event loop.

Impact: large admitted documents can exceed the 30-second heartbeat timeout while work is still progressing, causing Temporal retries and duplicate stage execution.

Evidence: heartbeat implementation at `src/documind/workflows/activities/inspect.py:77-97`; synchronous scanner paths at `src/documind/services/scanner_service.py:155-169,219-291,319-482`; synchronous normalization at `src/documind/services/processing_service.py:86-106,299-390`.

### T4-11 — Unsafe inspection does not persist the required processing evidence or complete inspection audit metadata

`StorageService.write_evidence()` and `ProcessingStage.evidence_object_key` exist but have no callers. On a terminal inspect failure, the stage store records only a generic stage audit entry; it does not write an evidence object, assign `evidence_object_key`, retain the detected MIME on the version, or include detected MIME in the inspection audit details.

Impact: unsafe/unsupported input does not meet the required evidence-retention and inspection-audit contract.

Evidence: uncalled evidence support in `src/documind/services/storage_service.py:193-203` and `src/documind/models/processing.py:111-113`; terminal failure path at `src/documind/workflows/stage_store.py:191-243`; success-only MIME assignment at `:624-631`; admission-stage input construction at `src/documind/domain/document_service.py:707-736`. The required evidence/audit fields are specified in `docs/specs/2026-07-29-documind-self-hosted-architecture-design.md:480-481,519-523`.

### T4-12 — The Redis consumer does not re-check version lifecycle or tombstone state before starting Temporal

The consumer validates only the CloudEvents envelope and UUID, reserves its dedupe key, and starts the workflow from event data. It never loads the version from PostgreSQL or performs a lifecycle/tombstone check. The first lifecycle check is deferred until the inspect activity has already been scheduled.

Impact: a version deleted or tombstoned after publication can still be given a Temporal workflow, contrary to the consumer contract. The ensuing activity failure occurs after workflow creation rather than suppressing the stale event.

Evidence: `src/documind/workflows/maintenance/outbox_dispatcher.py:139-190`; the first injected lifecycle guard is invoked by `src/documind/workflows/activities/inspect.py:43-57`; the PostgreSQL tombstone check is `src/documind/workflows/stage_store.py:362-383`. The required consumer re-check is in `docs/specs/2026-07-29-documind-self-hosted-architecture-design.md:511-512`.

### T4-13 — The Task 4 safety and production-runtime cases are largely untested

Scanner tests cover malware, one expansion-ratio ZIP bomb, and unsupported MIME. OCR tests cover one successful low-confidence fallback. There is no test for the absent sandbox runner/container path, encrypted PDF handling, image/TIFF budgets, markup remote directives, malformed ClamAV verdicts, RapidOCR unavailability after a Docling content fallback, Redis crash windows, the tombstone/write race, or heartbeat behavior under a large input.

Impact: the listed Task 4 production and safe-file contract defects have no regression detection; the passing focused suite does not exercise them.

Evidence: `tests/test_services/test_scanner_service.py:46-101`; `tests/test_services/test_ocr_service.py:25-60`; `tests/test_workflows/test_outbox_dispatcher.py:103-143`; `tests/test_worker_bootstrap.py:1-120`.

### T4-14 — The worker’s projection stages run on the wrong Task 4 queue

Task 4 requires `ingest-cpu` only for scanner/parser work. The workflow instead schedules `project`, `verify`, and `complete` on `ingest-cpu`; the production worker registers all three on that queue. This also omits the specification’s `project-gpu` and `project-cpu` queues.

Impact: CPU ingestion work shares a queue with embedding/projection and lifecycle completion, so the required workload isolation and queue contract are not implemented.

Evidence: `src/documind/workflows/document_version.py:151-160`; `src/documind/workflows/worker.py:252-265`; required queues in `docs/specs/2026-07-29-documind-self-hosted-architecture-design.md:526-531`.

### T4-15 — Completion does not emit the required processed and indexed domain events

The complete stage only invokes `ProjectionCoordinator.complete_snapshot()` and updates lifecycle metadata through the stage store. No code publishes `document-version.processed` or `document-version.indexed` CloudEvents after completion.

Impact: downstream consumers never receive two required terminal document-version events.

Evidence: `src/documind/workflows/activities/complete.py:32-49`; `src/documind/workflows/stage_store.py:624-656`; repository references contain no `document-version.processed` or `document-version.indexed` event publisher. Required events: `docs/specs/2026-07-29-documind-self-hosted-architecture-design.md:368-379,1708`.

## Verification performed

The focused test selection completed successfully:

`pytest -q tests/test_api/test_documents.py tests/test_domain/test_document_service.py tests/test_workflows/test_document_version.py tests/test_workflows/test_outbox_dispatcher.py tests/test_services/test_storage_service.py tests/test_services/test_scanner_service.py tests/test_services/test_ocr_service.py tests/test_services/test_processing_service.py tests/test_worker_bootstrap.py`
