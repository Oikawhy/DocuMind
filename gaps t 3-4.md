# Tasks 3–4 gap audit

Scope: Tasks 3 and 4 (including their listed substeps) in `docs/plans/implement.md`, audited against the current `HEAD` worktree. This is a gap report only: it intentionally contains no remediation plan or fix suggestions.

The focused Task 3–4 pytest selection completed successfully. That result does not close the runtime and integration gaps below because several critical paths are only exercised with mocks or are not exercised at all.

## Step status

| Task | Step | Status | Audit result |
| --- | --- | --- | --- |
| 3 | 1. MinIO client/layouts | Partial | Bucket initialization, bounded upload, and key helpers exist; accepted-original promotion is not connected to any runtime path. |
| 3 | 2. Streaming upload | Done | Bounded hashing reader and 500 MiB hard-cap configuration are present. |
| 3 | 3. Document domain/admission/idempotency | Partial | Normal admission is implemented; concurrent `admit_version` idempotent retries do not return the original operation. |
| 3 | 4. Transactional outbox writer | Done | CloudEvents envelope and `FOR UPDATE SKIP LOCKED` claim query are implemented. |
| 3 | 5. Pydantic contracts | Done | Admission, document, version, operation, cursor, and error contracts exist. |
| 3 | 6. REST endpoints | Partial | Required routes exist, but authorized cursor pagination can terminate before later authorized records and most route contracts have no API test. |
| 3 | 7. Operation polling | Done | Endpoint and service method return safe operation/stage metadata. |
| 3 | 8. Required tests | Partial | Named service/domain cases exist; endpoint-level and concurrent-idempotency coverage is incomplete. |
| 4 | 1. Temporal client factory | Done | Namespace-aware factory exists. |
| 4 | 2. Outbox dispatcher | Done | Pending rows are claimed with `SKIP LOCKED`, published to Redis Streams, and record a stream ID. |
| 4 | 3. Redis consumer/start workflow | Partial | Normal deduplication works, but its reservation ordering can permanently lose a workflow. |
| 4 | 4. ClamAV scanner | Done | TCP INSTREAM adapter and safe scanner results are implemented. |
| 4 | 5. Archive inspector | Partial | ZIP ratio, size, nesting, and traversal checks are implemented, but critical limits lack test coverage. |
| 4 | 6. MIME validator | Done | Magic-byte/declared-MIME comparison and the admitted list are implemented. |
| 4 | 7. Docling parser sandbox | Not done | The configured production parser runner is absent and the sandbox container is not actually connected to the worker. |
| 4 | 8. RapidOCR fallback | Partial | The normal low-confidence fallback exists; an unavailable RapidOCR fallback is incorrectly terminal in common cases. |
| 4 | 9. Normalizer | Done | NFC normalization, offset map, page/block records, language evidence, and normalized persistence exist. |
| 4 | 10. DocumentVersionWorkflow | Partial | Inspect → parse → normalize scheduling is present, but the production workflow continues into uninitialized stub services and cannot complete. |
| 4 | 11. Durable stage records/replay checksums | Partial | Records contain keys and checksums, but a crash after execution and before durable success can execute a stage again. |
| 4 | 12. Required tests | Partial | The listed happy-path cases exist; production wiring, failure/retry, archive-limit, and parser-sandbox coverage do not. |

## Gaps

### T3-01 — Safe bytes are never promoted from quarantine to the accepted-original layout

`StorageService.move_to_accepted()` and `accepted_key()` exist, but no production source calls either. `DocumentVersion.accepted_object_key` is therefore never populated, while all worker reads continue to use `quarantine_object_key`.

Impact: a successfully inspected document remains in quarantine indefinitely; the required `originals/{document_uuid}/{version_number}/{sha256}` lifecycle path is not realized.

Evidence: `src/documind/services/storage_service.py:114-123`, `src/documind/services/storage_service.py:176-178`, and `src/documind/workflows/stage_store.py:468-474`; repository-wide references show no call to `move_to_accepted`.

### T3-02 — Concurrent idempotent version admission does not replay the winning operation

`admit_document()` has a second lookup after an `IntegrityError`, but `admit_version()` converts every `IntegrityError` directly into `VERSION_CONFLICT`. Two simultaneous retries with the same subject, target, body hash, and idempotency key can therefore return a conflict instead of the original operation.

Impact: Task 3's 24-hour idempotency contract is not reliable for version-admission races.

Evidence: `src/documind/domain/document_service.py:323-369`, especially the unconditional `IntegrityError` handler following the initial replay lookup; contrast with the recovery path in the new-document admission method. No concurrent version-idempotency test exists.

### T3-03 — Authorized document cursor pagination can hide all later authorized documents

`list_documents()` applies SQL ordering and `limit + 1` before applying requested-label, lifecycle, and authorization filters. If that database window contains only inaccessible/non-matching documents, `page_documents` is empty and the method returns no `next_cursor`, even if older authorized documents exist.

Impact: `GET /v1/documents` is not a complete cursor-paginated view of authorized documents; clients can stop at an empty page and never see later eligible records.

Evidence: `src/documind/domain/document_service.py:409-424` limits the unfiltered query, filters happen at `:426-462`, and the cursor is only emitted when a returned page document exists. There is no pagination test with an inaccessible first window.

### T3-04 — Most Task 3 API contracts have no route-level verification

The API suite covers only initial document admission, inaccessible document read, missing idempotency header, and operation polling. It does not exercise the implemented version-admission, document-list, document-version-read, or deletion endpoints.

Impact: four required REST surfaces and their public error/authorization/multipart behavior have no API-level regression coverage.

Evidence: all test functions in `tests/test_api/test_documents.py:37-165`; the implemented routes are in `src/documind/api/documents.py:43-159`.

### T4-01 — Docling sandbox is neither buildable nor runnable from the production worker

The sandbox Dockerfile copies `src/documind/parsers/`, but that directory and the `documind.parsers.docling_runner` module do not exist. The worker's configured command runs that missing module locally (`python -m documind.parsers.docling_runner`), while the separately declared `docling-sandbox` container has no worker-to-container transport or shared input path.

Impact: the Dockerfile cannot copy its required source and the production Docling parser cannot execute. Every document depends on RapidOCR or fails parsing; the required least-privilege Docling execution path is absent.

Evidence: `Dockerfile.docling:23-31`, `docker-compose.dev.yml:62` and `:76-95`, `src/documind/workflows/worker.py:162-190`, and the absence of any `src/documind/parsers` file in the repository.

### T4-02 — Redis deduplication can acknowledge an event without ever starting its workflow

The consumer writes its Redis `SET NX` deduplication key before calling Temporal. If the process stops after that reservation but before `start_workflow`, Redis redelivers the pending message; `consume()` returns `False` because the key already exists, and the stream runner still acknowledges that message.

Impact: a valid accepted-version CloudEvent can be permanently dropped, leaving its document version without a Temporal workflow.

Evidence: reservation occurs in `src/documind/workflows/maintenance/outbox_dispatcher.py:159-169`; workflow start is later at `:178-191`; `RedisStreamWorkflowRunner.run_once()` acknowledges regardless of the returned boolean at `:251-259`. Existing tests cover only ordinary duplicate delivery (`tests/test_workflows/test_outbox_dispatcher.py:116-127`).

### T4-03 — RapidOCR unavailability is terminal after a Docling content fallback

When Docling produces empty or low-confidence content, `docling_unavailable` remains `False`. If RapidOCR then raises `ParserUnavailableError`, `OCRService.parse()` returns an unsuccessful `ParseResult` instead of raising. The stage store treats every unsuccessful parse result as a terminal failure, so the configured two parser attempts are skipped.

Impact: temporary RapidOCR outages become immediate terminal parser failures rather than using the Task 4 retry policy.

Evidence: `src/documind/services/ocr_service.py:181-213`; terminal parse outputs are finalized by `src/documind/workflows/stage_store.py` in `_terminal_failure`. `tests/test_services/test_ocr_service.py` covers only the successful low-confidence fallback.

### T4-04 — The registered production workflow cannot complete because it enters stubbed services

After normalize, `DocumentVersionWorkflow` runs chunk, enrich, project, verify, and complete. The actual worker builds `ChunkingService` and `EnrichmentService` with `__new__` instead of initialized dependencies, and builds the projection coordinator from no-op stub adapters. Those activities are registered on the production queues.

Impact: an accepted version can reach Task 4's inspect/parse/normalize stages but cannot reliably complete the registered workflow; the worker is not an operational end-to-end ingestion runtime.

Evidence: stage sequence in `src/documind/workflows/document_version.py:122-209`; stub construction and registration in `src/documind/workflows/worker.py:204-270` and `:273-370`. The bootstrap tests only inspect configuration constants and imports, not a constructed runtime (`tests/test_worker_bootstrap.py:35-120`).

### T4-05 — Stage replay is not crash-safe between executing work and recording success

`PostgresStageStore.run()` commits a stage as `running`, executes the activity outside that transaction, and only then stores output/checksum in a later transaction. A process failure in that interval leaves a `running` stage with no durable output; a Temporal retry claims it and executes the scanner/parser/normalizer again.

Impact: the implementation provides replay only after a success record exists, not durable exactly-once/idempotent execution across the crash window required by the stage-replay contract.

Evidence: `src/documind/workflows/stage_store.py:58-87` and `:96-155`. Existing replay tests use an in-memory store or already-persisted output and do not simulate this interval.

### T4-06 — Activity heartbeats are not dependable for the maximum admitted input size

The heartbeat task shares the event loop with synchronous scanner/archive work and the normalizer's multi-pass Unicode processing. `ProcessingService.normalize()` performs its large text transformations without yielding after the initial source read; scanner archive checks and the ClamAV write loop also execute substantial synchronous work. The 10-second heartbeat coroutine cannot run while those operations block the loop.

Impact: on large documents the 30-second heartbeat contract can time out although the activity is still executing, causing duplicate Temporal attempts and undermining the configured retry behavior.

Evidence: heartbeat loop in `src/documind/workflows/activities/inspect.py:77-97`; synchronous normalization path in `src/documind/services/processing_service.py:86-117`; synchronous archive/magic processing in `src/documind/services/scanner_service.py`.

### T4-07 — Archive and parser safety behaviors lack coverage beyond the minimal examples

Scanner tests cover malware, one expansion-ratio ZIP bomb, and unsupported MIME only. They do not cover path traversal (zip-slip), nested archive rejection, the decompressed-size cap, duplicate archive entries, ClamAV TCP failure, or format-specific preflight outcomes. OCR tests cover one successful low-confidence fallback only; they do not cover empty Docling output, Docling failure, unavailable RapidOCR, both parsers unavailable, sandbox command failure, or parser provenance on failure.

Impact: several Task 4 archive-defense and parser/fallback substep behaviors can regress without detection, including the retry defect documented above.

Evidence: the complete scanner test file ends at `tests/test_services/test_scanner_service.py:101`; the complete OCR test file ends at `tests/test_services/test_ocr_service.py:60`.

### T4-08 — No integration test detects the broken worker, sandbox, or accepted-object lifecycle

The focused suites use fake Redis, fake Temporal starter, fake scanner/parser, or only inspect static workflow configuration. No test builds `Dockerfile.docling`, starts the worker runtime, verifies the configured Docling command, runs an accepted event through Redis/Temporal/stage storage, or verifies quarantine-to-accepted promotion.

Impact: the current test suite passes while the production parser is missing, the accepted-original transition is absent, and worker completion is blocked by stubs.

Evidence: `tests/test_workflows/test_outbox_dispatcher.py:103-144`, `tests/test_worker_bootstrap.py:12-120`, and the unit-only storage test at `tests/test_services/test_storage_service.py:63-72`.
