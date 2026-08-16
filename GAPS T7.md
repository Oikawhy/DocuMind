# Task 7 gap audit

## Scope and method

This report compares Task 7, **Retrieval Service + Reranker + Authorization Filters**, in `docs/plans/implement.md` with the current retrieval service, backend adapters, projection writers, API routes, startup composition, and focused tests. It records verified implementation and coverage gaps only; it does not propose changes.

Focused Task 7 schema/service/API tests passed during this audit:

```text
44 passed in 0.14s
```

Those tests mostly construct schemas, mocks, or the standalone RRF/reranker logic. They do not exercise the live retrieval pipeline against the implemented projection payloads or graph schema.

## Retrieval backends and authorization filters

### T7-01 — Qdrant does not perform the required dense BGE-M3 search

`QdrantRetrievalBackend.search()` calls `AsyncQdrantClient.search()` with a payload filter, limit, and payload request, but no `query_vector` or other query embedding ([qdrant_backend.py:107-116](src/documind/services/backends/qdrant_backend.py#L107)). The constructor has no embedding dependency and the retrieval service passes the plain query text unchanged ([retrieval_service.py:505-510](src/documind/services/retrieval_service.py#L505)).

Task 7 requires Qdrant dense search, while Task 7's `naive` and `hybrid` modes rely on that branch. The implemented branch has no dense-query input.

### T7-02 — Qdrant retrieval rejects its own projected records

The Qdrant projection payload stores `content_hash`, but not `content` or `content_sha256` ([indexing_service.py:155-169](src/documind/services/indexing_service.py#L155)). The retrieval adapter reads `content` and `content_sha256`, then discards every result lacking the latter ([qdrant_backend.py:132-153](src/documind/services/backends/qdrant_backend.py#L132)). Thus a record created by the repository's Qdrant writer cannot be converted into a `ScoredChunk` by its Qdrant reader.

### T7-03 — OpenSearch retrieval rejects its own projected records

The OpenSearch writer uses the chunk UUID only as the document `_id` and stores `content_hash`, not `chunk_id` or `content_sha256` in `_source` ([indexing_service.py:338-365](src/documind/services/indexing_service.py#L338)). The reader requests and requires `_source.chunk_id` and `_source.content_sha256`, then discards a hit with no hash ([opensearch_backend.py:80-142](src/documind/services/backends/opensearch_backend.py#L80)).

Consequently, a normal OpenSearch projection produced by this repository cannot yield retrievable `ScoredChunk` values through the implemented reader.

### T7-04 — server-built projection filters omit the allowed-label constraint

`build_authorization_context()` resolves `allowed_label_ids` from the caller's role mappings ([retrieval_service.py:400-458](src/documind/services/retrieval_service.py#L400)), but neither Qdrant nor OpenSearch adds it to its backend filter. Their filters contain only version ID, lifecycle, and tombstone generation ([qdrant_backend.py:88-105](src/documind/services/backends/qdrant_backend.py#L88); [opensearch_backend.py:59-79](src/documind/services/backends/opensearch_backend.py#L59)).

This leaves the required server-built allowed-label filter absent from both projection queries.

### T7-05 — Permission Guard does not validate the candidate’s version/document identity or actual content

`PermissionGuard.check()` fetches the canonical chunk's version and document IDs, but never compares them with `candidate.version_id` or `candidate.document_id` ([retrieval_service.py:156-195](src/documind/services/retrieval_service.py#L156)). It compares the canonical hash only with the candidate-supplied `content_sha256`; it does not hash or otherwise compare `candidate.content` ([retrieval_service.py:170-173](src/documind/services/retrieval_service.py#L170)).

A projection result with a valid chunk UUID and copied canonical hash, but a different version/document identifier or different text, passes the Permission Guard and is sent to the reranker. This contradicts the required canonical candidate identity/hash validation before reranking.

### T7-06 — RRF records conflicting duplicate metadata but still adds the conflicting branch score

When two branches return the same chunk UUID with conflicting version, document, or content-hash metadata, `rrf_fuse()` only logs the conflict ([retrieval_service.py:242-261](src/documind/services/retrieval_service.py#L242)). It retains the first `ScoredChunk` but still adds reciprocal-rank contributions from every conflicting result ([retrieval_service.py:237-274](src/documind/services/retrieval_service.py#L237)).

The resulting fused rank can be increased by a projection result whose identity metadata conflicts with the retained candidate, despite the function's documented conflict rejection behavior.

### T7-07 — retrieval ignores the request’s `version_selector`

`RetrievalRequest` accepts `version_selector` ([retrieval.py:18-29](src/documind/schemas/retrieval.py#L18)), but `RetrievalService.retrieve()` passes only `document_ids` into the authorization-context builder ([retrieval_service.py:473-481](src/documind/services/retrieval_service.py#L473)). That builder selects every completed, non-tombstoned version of the requested documents ([retrieval_service.py:407-422](src/documind/services/retrieval_service.py#L407)).

The retrieval API therefore does not enforce the caller's permitted version constraint, including its default `latest_completed` selector.

### T7-08 — the Neo4j queries do not match the graph projection schema

Both graph retrieval backends traverse `:ABOUT`, `:MENTIONS`, and (for global) `:RELATES_TO`, then require `(:Fact)-[:SOURCED_FROM]->(:Chunk)` ([neo4j_local_backend.py:80-104](src/documind/services/backends/neo4j_local_backend.py#L80); [neo4j_global_backend.py:78-103](src/documind/services/backends/neo4j_global_backend.py#L78)). The graph writer creates `(:Entity)-[:SUBJECT_OF]->(:Fact)`, optional `(:Fact)-[:OBJECT_ENTITY]->(:Entity)`, and `(:Fact)-[:SUPPORTED_BY]->(:Chunk)` instead ([graph_service.py:347-387](src/documind/services/graph_service.py#L347)).

The writer also stores the source chunk's `content_hash` and page/section fields, while the readers return `c.version_id`, `c.document_id`, `c.content`, and `c.content_sha256`, which the writer does not set on the `Chunk` node ([graph_service.py:217-247](src/documind/services/graph_service.py#L217)). A graph materialized by the current projection writer cannot satisfy either graph retrieval query.

### T7-09 — graph retrieval reads the highest Neo4j generation, not the verified active generation

`Neo4jLocalRetrievalBackend._get_active_generation()` and `Neo4jGlobalRetrievalBackend._get_active_generation()` execute `max(f.generation)` directly against Neo4j ([neo4j_local_backend.py:127-147](src/documind/services/backends/neo4j_local_backend.py#L127); [neo4j_global_backend.py:126-142](src/documind/services/backends/neo4j_global_backend.py#L126)). Neither reads PostgreSQL's `active_projection_generation` registry or checks that the selected generation is verified.

Task 7 requires local/global modes to use only the active, verified generation. A newer incomplete or unhealthy graph generation can instead be served.

### T7-10 — graph traversal does not enforce the required two-hop maximum

`Neo4jGlobalRetrievalBackend` defaults `max_path_length` to `4` ([neo4j_global_backend.py:30-41](src/documind/services/backends/neo4j_global_backend.py#L30)). Startup supplies only `max_sources`, so the default remains in use ([main.py:208-211](src/documind/main.py#L208)). The generated Cypher uses that value in its variable-length path ([neo4j_global_backend.py:78-103](src/documind/services/backends/neo4j_global_backend.py#L78)). The local backend likewise accepts any `max_hops` value and startup passes the unconstrained setting directly ([neo4j_local_backend.py:39-48](src/documind/services/backends/neo4j_local_backend.py#L39); [main.py:203-207](src/documind/main.py#L203)).

The global default exceeds Task 7's maximum depth of two hops, and the local configuration has no enforcement of that maximum.

### T7-11 — local/global modes have no required naive fallback when graph evidence is unavailable

`_select_backends()` selects only `neo4j_local` for `local` and only `neo4j_global` for `global` ([retrieval_service.py:607-616](src/documind/services/retrieval_service.py#L607)). An unavailable graph backend is simply recorded as degraded, while an unhealthy graph generation or an empty traversal is returned as an ordinary empty result ([retrieval_service.py:518-530](src/documind/services/retrieval_service.py#L518); [neo4j_local_backend.py:66-76](src/documind/services/backends/neo4j_local_backend.py#L66); [neo4j_global_backend.py:62-71](src/documind/services/backends/neo4j_global_backend.py#L62)). No naive branch is added in either case.

Task 7 requires local/global graph requests to fall back to naive retrieval when the graph is unhealthy or has no authorized source chunks. The implementation returns empty evidence instead.

### T7-12 — configured Neo4j authentication disables graph retrieval at startup

The lifespan closes `secret_client` before retrieval adapters are built ([main.py:96-140](src/documind/main.py#L96)). When `neo4j_auth_ref` is configured, the following Neo4j setup calls `secret_client.get_secret()` after that close ([main.py:195-203](src/documind/main.py#L195)); its broad exception handler then omits both graph backends ([main.py:212-213](src/documind/main.py#L212)).

In addition, the declared example reference addresses the `auth` secret key ([.env.example:9](.env.example#L9)), while startup attempts to read `user` and `password`. The configured authenticated Neo4j path is therefore not wired as a working retrieval dependency.

### T7-13 — graph-specific provenance is discarded before citation construction

The Neo4j query returns `fact_id`, `generation`, and `hop_count` ([neo4j_local_backend.py:89-103](src/documind/services/backends/neo4j_local_backend.py#L89); [neo4j_global_backend.py:86-103](src/documind/services/backends/neo4j_global_backend.py#L86)), but both `_convert_results()` implementations discard those fields when creating `ScoredChunk` ([neo4j_local_backend.py:171-202](src/documind/services/backends/neo4j_local_backend.py#L171); [neo4j_global_backend.py:163-194](src/documind/services/backends/neo4j_global_backend.py#L163)). `RetrievalService._build_evidence()` never supplies `graph_path` to `build_citation()` ([retrieval_service.py:691-710](src/documind/services/retrieval_service.py#L691)).

Graph-derived evidence therefore cannot retain the returned fact/generation/hop provenance in the citation's optional graph path.

## Pipeline, citations, and degraded behavior

### T7-14 — citation provenance is not rebuilt from canonical page and section metadata

During final evidence construction, the service loads only canonical chunk ID, content, and content hash ([retrieval_service.py:644-655](src/documind/services/retrieval_service.py#L644)). It then calls `build_citation()` with the original projected `ScoredChunk`; page range and section path remain that candidate's values ([retrieval_service.py:691-710](src/documind/services/retrieval_service.py#L691); [retrieval_service.py:284-324](src/documind/services/retrieval_service.py#L284)).

The citation's page and section provenance is consequently not canonicalized after a projection result is returned, despite Task 7 requiring provenance-bearing canonical citations.

### T7-15 — the final citation pass does not recheck the cited version’s lifecycle or tombstone state

The pre-rerank `PermissionGuard` checks lifecycle, version tombstone generation, document erasure, and deletion tombstones ([retrieval_service.py:163-206](src/documind/services/retrieval_service.py#L163)). The later `_build_evidence()` queries chunks and versions by ID without those predicates ([retrieval_service.py:644-664](src/documind/services/retrieval_service.py#L644)); its only final check is document-level `AuthorizationService.authorize()` ([retrieval_service.py:666-689](src/documind/services/retrieval_service.py#L666)).

If lifecycle or tombstone state changes after the Permission Guard and before response serialization, the final citation path does not independently reject that version/chunk.

### T7-16 — empty/degraded responses discard required retrieval measurements

On all-backend failure or a budget exit, `_empty_response()` emits zero candidate counts and only `mode="none"`, omitting the already-collected PostgreSQL time, failed-backend timings, fusion time, permission-filter count, and reranker counts ([retrieval_service.py:529-530](src/documind/services/retrieval_service.py#L529); [retrieval_service.py:549-551](src/documind/services/retrieval_service.py#L549); [retrieval_service.py:714-733](src/documind/services/retrieval_service.py#L714)).

Task 7 requires the API to record individual backend latency, fusion time, permission-filter count, degraded branches, and candidate/reranker/evidence counts. The degraded/empty return path does not retain the values it has already observed.

### T7-17 — the 2.5-second end-to-end deadline does not cover Permission Guard, final citation work, or final authorization

Backend searches have 750 ms wrappers and reranking has a 1,000 ms wrapper ([retrieval_service.py:505-516](src/documind/services/retrieval_service.py#L505); [retrieval_service.py:553-567](src/documind/services/retrieval_service.py#L553)). The only overall-budget checks occur before search and after the Permission Guard ([retrieval_service.py:498-500](src/documind/services/retrieval_service.py#L498); [retrieval_service.py:549-551](src/documind/services/retrieval_service.py#L549)).

`PermissionGuard.check()`, canonical evidence queries, and the sequential final authorization checks have no deadline and no subsequent overall-budget check ([retrieval_service.py:541-547](src/documind/services/retrieval_service.py#L541); [retrieval_service.py:623-712](src/documind/services/retrieval_service.py#L623)). The service can return after the Task 7 end-to-end retrieval budget is exhausted.

## Comparison and reindex endpoints

### T7-18 — comparison returns no deterministic comparison data

After resolving versions, `compare()` runs two generic retrieval calls with the literal query `"content of version"`, collects citations, and creates a `deterministic_diff` containing only IDs, citation counts, and requested field names ([retrieval_service.py:735-810](src/documind/services/retrieval_service.py#L735)). It does not load/compare version content or return changed, added, removed, or field-level values.

The `/v1/comparisons` endpoint therefore does not provide the required cited comparison data/deterministic diff.

### T7-19 — comparison declares success without evidence for either resolved version

`compare()` assigns a `deterministic_diff` whenever both version IDs resolve, irrespective of whether either scoped retrieval produced an authorized citation ([retrieval_service.py:762-804](src/documind/services/retrieval_service.py#L762)).

Task 7's cited comparison contract requires evidence for the referenced versions; the implementation returns a comparison-shaped result with zero citations rather than a safe missing-evidence result.

### T7-20 — an invalid explicit comparison selector silently resolves to the latest version

For a non-`latest_completed` selector, `_resolve_version_ref()` tries a number and then a UUID. If both parsing attempts fail, it retains the original latest-version statement and returns that result ([retrieval_service.py:850-888](src/documind/services/retrieval_service.py#L850)).

An invalid explicit version selector consequently resolves to `latest_completed` rather than failing resolution, so a comparison can silently use a different version than the caller named.

### T7-21 — the reindex endpoint does not start a projection revision

`POST /v1/document-versions/{version_id}/reindex` validates and persists an `Operation` with status `accepted`, then returns it ([versions.py:129-156](src/documind/api/versions.py#L129)). No workflow, activity, queue dispatch, or active-generation operation is invoked by the route. The worker registers only the document-version workflow and its ingestion activities, not a reindex/rebuild workflow ([worker.py:252-265](src/documind/workflows/worker.py#L252)).

The endpoint does not create the new projection revision required by Task 7.

## Reranker supply-chain contract

### T7-22 — the BGE reranker model is not digest-pinned

The reranker image downloads `BAAI/bge-reranker-v2-m3` by mutable model name during the Docker build ([Dockerfile.reranker:10-16](Dockerfile.reranker#L10)). The sidecar only verifies a digest when `RERANKER_MODEL_DIGEST` is non-empty ([reranker_app.py:22-62](scripts/reranker_app.py#L22)), but Compose supplies no such variable ([docker-compose.yml:186-199](docker-compose.yml#L186)).

The Task 7 requirement for a digest-pinned BGE-reranker-v2-m3 model is not enforced in the deployed sidecar configuration.

## Required Task 7 test coverage

### T7-23 — required pipeline and backend acceptance coverage is absent

`tests/test_services/test_retrieval_service.py` covers request schemas, RRF arithmetic/tie-breaking, an empty Permission Guard input, and locally built citations ([test_retrieval_service.py:1-339](tests/test_services/test_retrieval_service.py#L1)). `tests/test_api/test_retrieval.py` replaces the retrieval service with mocks and verifies only HTTP serialization ([test_retrieval.py:32-271](tests/test_api/test_retrieval.py#L32)). No tests instantiate the Qdrant, OpenSearch, Neo4j-local, or Neo4j-global retrieval adapters; no focused backend test files exist.

As a result, the Task 7-required authorization-filtering pipeline, Neo4j bounded-depth behavior, backend degraded-mode behavior, canonical citation validity, comparison evidence requirement, reindex dispatch, and the contract between projection writers and retrieval readers are untested. The passing focused suite does not execute those production paths.
