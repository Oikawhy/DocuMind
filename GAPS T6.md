# Task 6 gap audit

## Scope and method

This report compares Task 6 in the implementation plan (`.planning/intel/context.md`, the ingested mirror of `docs/plans/implement.md`) with the current Task 6 services, Temporal activities, worker composition, schema, rebuild path, API entry point, and focused tests. It records verified gaps only; it contains no remediation proposals.

The focused Task 6 selection passed during this audit:

```text
79 passed in 0.41s
```

Those tests use in-memory coordinators, payload resolvers, writers, and guards. They do not exercise the production worker's projection construction, a PostgreSQL snapshot, or Qdrant, OpenSearch, and Neo4j.

## PRJ-01 — digest-pinned BGE-M3 embedding

### T6-01 — the production projection pipeline never creates or uses `EmbeddingService`

`EmbeddingService` exists, but no production path constructs it, loads its configured local artifact, or passes it to `EmbeddingChunkPayloadResolver`. `Settings.embedding_model_path` and `Settings.embedding_model_digest` are likewise not read by the worker. `_build_projection_coordinator()` only creates the canonical source, evidence store, guard, completer, and writers ([worker.py:395-441](src/documind/workflows/worker.py#L395)); no embedding dependency appears in that composition. The only `EmbeddingChunkPayloadResolver` references are its own implementation and unit tests.

Consequently, the `project` stage cannot produce the required digest-pinned 1024-dimensional BGE-M3 vectors from canonical chunk text.

### T6-02 — the live worker silently substitutes non-projection writers when the backend clients are unavailable

The installed environment resolves every projection backend to `_PassthroughWriter`, which manufactures a matching manifest without writing a vector, search document, or graph record. The fallback is deliberately returned after an unavailable client import ([worker.py:451-518](src/documind/workflows/worker.py#L451)). The audit executed the worker factory and received `_PassthroughWriter` for `qdrant`, `opensearch`, and `neo4j`.

This path allows the coordinator to verify and complete a version while no BGE-M3 embedding or external projection has occurred.

### T6-03 — embedding results have no durable identity or replay state

`EmbeddingChunkPayloadResolver` calls the embedder for every resolved payload list ([embedding_payload_resolver.py:21-47](src/documind/services/embedding_payload_resolver.py#L21)), but there is no repository, projection payload record, or snapshot field that persists an embedding input hash, vector identity, or model digest for a projection generation. `DocumentChunk.embedding_model_digest` is the chunk-profile value, not a generated-vector record ([models/chunk.py:52-59](src/documind/models/chunk.py#L52)).

Thus a projection retry/rebuild has no durable evidence that it is reusing the same BGE-M3 output for the same canonical input.

## PRJ-02 — deterministic Qdrant and OpenSearch projections

### T6-04 — real writer construction is incompatible with the implemented writer interfaces

If the clients are importable, `_build_projection_writer()` passes `dimension=1024` to `QdrantProjectionWriter` and supplies no `payload_resolver` ([worker.py:462-473](src/documind/workflows/worker.py#L462)). The actual constructor accepts `embedding_dimension` and requires `payload_resolver` ([indexing_service.py:96-107](src/documind/services/indexing_service.py#L96)). The OpenSearch construction omits its required `payload_resolver` ([worker.py:477-486](src/documind/workflows/worker.py#L477); [indexing_service.py:283-292](src/documind/services/indexing_service.py#L283)), and the Neo4j construction omits its required graph payload resolver ([worker.py:490-500](src/documind/workflows/worker.py#L490); [graph_service.py:122-131](src/documind/services/graph_service.py#L122)).

With the declared client packages available, worker startup reaches `TypeError` instead of constructing any real projection writer.

### T6-05 — backend setup is never run before projection writes

The Qdrant payload-index/collection setup, OpenSearch mapping/index setup, and Neo4j constraint/index setup are exposed only by `ensure_collection()`, `ensure_index()`, and `ensure_constraints()` ([indexing_service.py:109-139](src/documind/services/indexing_service.py#L109), [indexing_service.py:294-309](src/documind/services/indexing_service.py#L294), [graph_service.py:133-142](src/documind/services/graph_service.py#L133)). No worker, activity, service bootstrap, or rebuild activity calls any of them.

The required Qdrant payload indexes, OpenSearch BM25/keyword index, and Neo4j schema therefore have no runtime initialization path.

### T6-06 — the canonical source cannot resolve a Task 6 snapshot and does not freeze the required source set

`PostgresCanonicalSource.resolve_snapshot()` imports `GraphFact` from `documind.models.enrichment`, a module absent from this repository; the model is defined in `documind.models.graph` ([projection_source.py:46-47](src/documind/services/projection_source.py#L46); [models/graph.py:43-96](src/documind/models/graph.py#L43)). Invoking the source therefore fails before it reads a projection input.

Independently of that import failure, the method ignores its `snapshot_id` as a lookup key and selects the first 10,000 chunks and first 10,000 facts from the entire database without version, lifecycle, tombstone, scope, ordering, or source-cutoff predicates ([projection_source.py:36-102](src/documind/services/projection_source.py#L36)). It returns a constant `run_id`, an empty `version_id`, generation `1`, and tombstone generation `0` ([projection_source.py:96-102](src/documind/services/projection_source.py#L96)).

This is neither the version-specific ingestion snapshot nor the complete, non-tombstoned canonical-corpus rebuild snapshot required by Task 6. It also causes the normal tombstone guard to parse `uuid.UUID("")` before a real write can begin.

### T6-07 — the snapshot checksum omits projection-defining canonical data

The only chunk input hashed into a snapshot record is chunk UUID plus `content_sha256`; the only fact input is fact UUID plus `predicate_key` ([projection_source.py:59-93](src/documind/services/projection_source.py#L59)). The hashes omit label IDs, lifecycle snapshot, tombstone generation, chunk profile revision, document/version identities, provenance, source links, entities, fact object, extraction revision, confidence, and corroboration data.

Changes to these required Qdrant/OpenSearch/Neo4j payload fields can therefore leave the frozen manifest checksum unchanged.

### T6-08 — Qdrant and OpenSearch manifests claim records that their writers do not write

`ProjectionCoordinator` expects every backend manifest to contain `len(snapshot.records)` and the checksum over every snapshot record ([projection_service.py:312-321](src/documind/services/projection_service.py#L312)). Qdrant and OpenSearch resolve and write only `ChunkProjectionPayload` values, yet each returns that whole-snapshot count/checksum ([indexing_service.py:141-187](src/documind/services/indexing_service.py#L141), [indexing_service.py:311-335](src/documind/services/indexing_service.py#L311)). Neo4j similarly writes graph payloads only but reports the same all-record manifest ([graph_service.py:144-165](src/documind/services/graph_service.py#L144)).

For a snapshot containing both chunks and facts, all three writers can report a matching full manifest while each projection store contains only its own subset. The coordinator treats those manufactured manifests as verification evidence.

### T6-09 — projection verification does not inspect any projection store

The coordinator compares a writer-supplied `ProjectionManifest` with `_expected_manifest()`, whose count and checksum are computed from the in-memory snapshot ([projection_service.py:243-259](src/documind/services/projection_service.py#L243), [projection_service.py:312-321](src/documind/services/projection_service.py#L312)). Each concrete writer creates that same expected count/checksum locally after an upsert rather than querying Qdrant, OpenSearch, or Neo4j ([indexing_service.py:180-187](src/documind/services/indexing_service.py#L180), [indexing_service.py:328-335](src/documind/services/indexing_service.py#L328), [graph_service.py:158-165](src/documind/services/graph_service.py#L158)).

There is no count/checksum comparison between PostgreSQL and any projection store, including no check for partial bulk/upsert success not surfaced by a client response.

### T6-10 — Qdrant and OpenSearch have no generation namespace or active-generation selection

Qdrant upserts only by stable chunk UUID and its payload contains no projection generation ([indexing_service.py:154-168](src/documind/services/indexing_service.py#L154)); OpenSearch does the same by `_id` ([indexing_service.py:338-364](src/documind/services/indexing_service.py#L338)). Both use a fixed collection/index name. The retrieval backends query those fixed stores directly and do not read `active_projection_generation` ([backends/qdrant_backend.py:45-117](src/documind/services/backends/qdrant_backend.py#L45); [backends/opensearch_backend.py:34-104](src/documind/services/backends/opensearch_backend.py#L34)).

A replacement generation therefore overwrites the serving representation before verification; the PostgreSQL active-generation pointer cannot select an isolated verified Qdrant or OpenSearch generation.

## PRJ-03 — constrained, provenance-bearing Neo4j materialization

### T6-11 — no production chunk or graph-fact payload resolver exists

`ChunkPayloadResolver` and `GraphFactPayloadResolver` are protocols only ([indexing_service.py:57-60](src/documind/services/indexing_service.py#L57); [graph_service.py:89-92](src/documind/services/graph_service.py#L89)). `rg` finds no concrete implementation of either resolver under `src/`; the only implementations are test fakes. The production worker supplies neither resolver, as described in T6-04.

There is no production path from canonical PostgreSQL chunks/facts, labels, document metadata, and provenance to a Qdrant/OpenSearch/Neo4j payload.

### T6-12 — Neo4j batch writes have no post-write canonical comparison and an oversized fact bypasses the 5 MiB cap

`Neo4jProjectionWriter._write_batch()` does not query or compare written fact count or source-version values after a batch ([graph_service.py:205-387](src/documind/services/graph_service.py#L205)). `Neo4jGraphRebuilder.verify_generation()` checks only the Fact-node count, not source links, entity identities, tombstone generation, or a checksum ([graph_service.py:444-464](src/documind/services/graph_service.py#L444)).

In `_split_batches()`, the 5 MiB condition splits only when `current_batch` is already non-empty ([graph_service.py:188-197](src/documind/services/graph_service.py#L188)). A single serialized fact larger than 5 MiB is admitted as a one-item transaction, exceeding Task 6's maximum transaction size.

### T6-13 — the Neo4j rebuild implementation is not integrated into the rebuild workflow

`Neo4jGraphRebuilder` is injected into `configure_rebuild_activities()` but is never referenced afterward ([rebuild_projections.py:57-72](src/documind/workflows/maintenance/rebuild_projections.py#L57)). `rebuild_projection()` instead calls the general coordinator, which fans out to all three writers ([rebuild_projections.py:80-98](src/documind/workflows/maintenance/rebuild_projections.py#L80)).

The graph-specific canonical fact replay, verification, and generation behavior in `Neo4jGraphRebuilder` is unreachable from the Task 6 rebuild path.

### T6-14 — emitted Neo4j graph shape cannot satisfy the project's graph-retrieval contract

The projection writer creates `(:Entity)-[:SUBJECT_OF]->(:Fact)`, `(:Fact)-[:OBJECT_ENTITY]->(:Entity)`, and `(:Fact)-[:SUPPORTED_BY]->(:Chunk)` ([graph_service.py:347-387](src/documind/services/graph_service.py#L347)). Both Neo4j retrieval implementations instead traverse `:ABOUT|MENTIONS` or `:ABOUT|MENTIONS|RELATES_TO` and require `(:Fact)-[:SOURCED_FROM]->(:Chunk)` ([backends/neo4j_local_backend.py:80-104](src/documind/services/backends/neo4j_local_backend.py#L80); [backends/neo4j_global_backend.py:78-103](src/documind/services/backends/neo4j_global_backend.py#L78)).

The writer also does not set the `Chunk` node's `version_id`, `document_id`, `content`, or `content_sha256`; it sets `content_hash` only ([graph_service.py:217-247](src/documind/services/graph_service.py#L217)). The graph queries filter and return exactly the missing properties. A graph materialized by Task 6 therefore cannot provide its required source chunks/provenance to its own retrieval paths.

## PRJ-04 — verified completion, generation state, and rebuilds

### T6-15 — the PostgreSQL projection-state adapter cannot persist the coordinator's normal projected state

The coordinator records successful writer outcomes with status `"projected"` and recognizes `"projected"` on replay ([projection_service.py:204-232](src/documind/services/projection_service.py#L204)). The database check constraint permits only `pending`, `writing`, `verified`, `unhealthy`, and `erased` ([models/projection.py:39-59](src/documind/models/projection.py#L39)). `PostgresEvidenceStore.record_outcome()` writes the coordinator status directly to `ProjectionState.state` ([projection_evidence_store.py:79-113](src/documind/services/projection_evidence_store.py#L79)).

The first normal successful writer outcome violates the database constraint rather than creating durable projection evidence.

### T6-16 — durable evidence lookup and persistence use incompatible identities

For Qdrant/OpenSearch, `state_for()` looks up the version UUID scope key (`_scope_key()`), but `record_outcome()` and `record_manifest()` persist the snapshot ID as `scope_key` ([projection_evidence_store.py:41-57](src/documind/services/projection_evidence_store.py#L41), [projection_evidence_store.py:79-124](src/documind/services/projection_evidence_store.py#L79), [projection_evidence_store.py:180-184](src/documind/services/projection_evidence_store.py#L180)). The persistence methods also do not set `version_id`, although the model requires it for Qdrant/OpenSearch scope consistency ([models/projection.py:48-52](src/documind/models/projection.py#L48)).

Stored Qdrant/OpenSearch outcomes cannot be found by the next replay and violate the projection-state scope constraint when persisted.

### T6-17 — verify/complete lose durable state after a worker restart

`ProjectionCoordinator.verify_snapshot()` reads only its process-local `_outcomes` dictionary; it does not load writer outcomes from `ProjectionEvidenceStore` ([projection_service.py:243-259](src/documind/services/projection_service.py#L243)). The `_verified` completion gate is also process-local ([projection_service.py:264-267](src/documind/services/projection_service.py#L264)).

If `project` completes durably and a worker restart occurs before/retry of `verify` or `complete`, the configured coordinator has no outcomes/verified marker and rejects the stage. This contradicts Task 6 projection-state management and durable Temporal replay behavior.

### T6-18 — active generation can point to an unverified or unrelated projection state

The model's active-generation foreign key constrains only `(projection_kind, scope_key, generation)`; it does not require the referenced `ProjectionState.state` to be `verified` ([models/projection.py:63-94](src/documind/models/projection.py#L63)). `ActiveGenerationManager.activate()` performs an unconditional insert/update and has no verification check ([generation_manager.py:49-92](src/documind/services/generation_manager.py#L49)). It runs independently of `PostgresEvidenceStore.record_manifest()`, which uses a separate transaction ([projection_evidence_store.py:117-151](src/documind/services/projection_evidence_store.py#L117)).

An active pointer can therefore be set to a `pending`, `writing`, `unhealthy`, or otherwise unrelated generation, and pointer activation is not atomic with verified manifest evidence.

### T6-19 — graph retrieval bypasses the active-generation registry and selects the highest Neo4j generation

`Neo4jLocalRetrievalBackend` and `Neo4jGlobalRetrievalBackend` determine their generation by `MATCH (f:Fact) ... RETURN max(f.generation)` ([backends/neo4j_local_backend.py:127-147](src/documind/services/backends/neo4j_local_backend.py#L127); [backends/neo4j_global_backend.py:126-142](src/documind/services/backends/neo4j_global_backend.py#L126)). Neither reads `active_projection_generation` or checks the projection-state verification status.

An incomplete or unhealthy replacement generation with a larger number is selected for retrieval instead of the prior active verified generation.

### T6-20 — completion omits required projection revision, domain events, and projection audit evidence

`PostgresLifecycleCompleter.complete_version()` changes only `DocumentVersion.lifecycle` and `completed_at` ([lifecycle_completer.py:31-61](src/documind/services/lifecycle_completer.py#L31)). It does not set `completed_projection_revision`, update the document's current-completed-version state, emit `document-version.processed`/`document-version.indexed`, or write the required projection completion audit record. Neither the coordinator nor the project/verify/complete activities receives `AuditService`.

Task 6's required completion events and the workflow contract's auditable projection completion are absent.

### T6-21 — the rebuild workflow is not registered, invoked, or connected to the reindex endpoint

`RebuildProjectionWorkflow` and its three activities are not imported/registered by the worker. The worker registers only `DocumentVersionWorkflow` and `inspect`, `parse`, `normalize`, `chunk`, `project`, `verify`, and `complete` ([worker.py:252-265](src/documind/workflows/worker.py#L252)). No worker startup call configures `configure_rebuild_activities()`.

`POST /v1/document-versions/{id}/reindex` creates and commits an `Operation`, then immediately returns its accepted response; it does not start or enqueue a rebuild workflow ([api/versions.py:129-156](src/documind/api/versions.py#L129)). Consequently no Task 6 rebuild can run from the exposed reindex operation.

### T6-22 — rebuild input, snapshot selection, backend execution, and activation are disconnected from the requested rebuild

`RebuildProjectionInput.scope`, `scope_id`, `reason`, and `requested_by` are not passed to the rebuild activity. The workflow constructs only `snapshot_id = "rebuild-{backend}-{scope}"` and `backend` ([rebuild_projections.py:164-174](src/documind/workflows/maintenance/rebuild_projections.py#L164)). The canonical source does not interpret that ID or scope (T6-06). `rebuild_projection()` ignores `backend` and calls `ProjectionCoordinator.project_snapshot()`, which fans out to all three backends ([rebuild_projections.py:80-98](src/documind/workflows/maintenance/rebuild_projections.py#L80); [projection_service.py:190-233](src/documind/services/projection_service.py#L190)). `verify_rebuild()` likewise verifies all coordinator writers, not the named backend ([rebuild_projections.py:100-116](src/documind/workflows/maintenance/rebuild_projections.py#L100)).

The requested full-corpus/all-three rebuild and a requested version/backend rebuild both lack the requested scope and backend behavior.

### T6-23 — rebuilds do not allocate or retain replacement generations

The rebuild workflow obtains its `generation` only from coordinator activity output ([rebuild_projections.py:176-178](src/documind/workflows/maintenance/rebuild_projections.py#L176)); the canonical source always returns generation `1` ([projection_source.py:96-102](src/documind/services/projection_source.py#L96)). `ActiveGenerationManager.allocate()` is never called by the rebuild workflow, and the injected generation manager is not configured by the production worker.

There is no distinct replacement generation for a rebuild, no retained previous verified generation, and no inactive partial-generation evidence associated with the requested run.

### T6-24 — Task 6 recovery and tombstone-retraction contracts are absent

The rebuild workflow has only a generic Temporal retry policy followed by optional activation ([rebuild_projections.py:154-214](src/documind/workflows/maintenance/rebuild_projections.py#L154)). It contains no one-fresh-rebuild recovery path after verification exhaustion, no operator/page outcome after a second failure, no tombstone cancellation, no Qdrant/OpenSearch/Neo4j retraction orchestration, and no absence proof across retained generations.

The standalone deletion methods are not called from a rebuild or tombstone path ([indexing_service.py:189-209](src/documind/services/indexing_service.py#L189), [indexing_service.py:366-377](src/documind/services/indexing_service.py#L366), [graph_service.py:466-470](src/documind/services/graph_service.py#L466)).

### T6-25 — projection activities are scheduled and hosted on the ingestion queue instead of Task 6 projection queues

`DocumentVersionWorkflow.stage_configurations()` places `project`, `verify`, and `complete` on `ingest-cpu` ([document_version.py:151-160](src/documind/workflows/document_version.py#L151)), and the live worker registers them there ([worker.py:252-258](src/documind/workflows/worker.py#L252)). The Task 6 contract assigns embedding/index work to `project-gpu` and verification to `project-cpu`.

The BGE-M3/model workload, projection I/O, and verification do not have the required queue isolation.

## Test coverage gaps

### T6-26 — focused Task 6 tests do not cover executable production behavior

`tests/test_services/test_task6_integration.py` builds its coordinator from `InMemorySource`, `InMemoryEvidenceStore`, `PassthroughWriter`, `PassthroughGuard`, and `PassthroughCompleter` ([test_task6_integration.py:51-150](tests/test_services/test_task6_integration.py#L51)). Its worker-adapter assertion only searches module-level names beginning with `_Stub`, while the production worker's nested fallbacks and incompatible writer calls remain unexercised ([test_task6_integration.py:159-170](tests/test_services/test_task6_integration.py#L159)).

There is no production-composition test for real writer construction, model loading, payload resolution, setup routines, PostgreSQL snapshot/state persistence, a worker restart between project/verify/complete, external-store count/checksum verification, active-generation cutover, rebuild dispatch, failed replacement retention, or tombstone retraction/absence proof. `tests/test_services/test_indexing_service.py`, named by the phase validation plan for deterministic Qdrant/OpenSearch behavior, is absent.
