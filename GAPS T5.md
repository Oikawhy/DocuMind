# Task 5 gap audit

## Scope and method

This report compares every requirement in Task 5.1–5.7 of
docs/plans/implement.md with the current source, worker composition, and tests.
It records verified gaps only. It deliberately contains no remediation or
design proposals.

The focused Task 5 pytest selection completed without test failures during this
audit. Those tests do not establish the production worker paths described below;
several use fakes or inspect configuration rather than exercising live adapters
and durable repositories.

## Task 5.1 — persistence and admission contracts

### T5.1-01 — policy selections are resolved outside the admission transaction

DocumentService.admit_document() resolves the declared type, chunk profile, and
template before its transaction opens
([document_service.py:171](/home/test/documind/src/documind/domain/document_service.py:171),
[document_service.py:201](/home/test/documind/src/documind/domain/document_service.py:201)).
The selected IDs are persisted later in that transaction. A policy/template
change between those reads and persistence can produce a version whose
supposedly immutable selections were not resolved in its admission transaction.

### T5.1-02 — subsequent-version admission inherits prior selections instead of resolving current declared-type policy

admit_version() copies the most recent version's selected chunk-profile and
template revision IDs
([document_service.py:289](/home/test/documind/src/documind/domain/document_service.py:289)).
It does not resolve the document's declared-type policy mapping as first
admission does. A later version can retain a stale profile/template selection
after that mapping changes.

### T5.1-03 — admission audit metadata omits the pinned chunk-profile revision

The admit-stage policy JSON contains both profile and template IDs
([document_service.py:728](/home/test/documind/src/documind/domain/document_service.py:728)),
but the admission audit event contains only the template ID
([document_service.py:762](/home/test/documind/src/documind/domain/document_service.py:762)).
The required immutable profile selection is absent from audit metadata.

### T5.1-04 — the required prior-head migration/legacy-fact upgrade test is absent

The migration test only inspects ORM metadata after the test database is
already at head ([test_schema_contract.py:325](/home/test/documind/tests/test_models/test_schema_contract.py:325)).
It does not upgrade a database containing pre-Task-5 graph facts from the
previous head or verify those facts are preserved.

### T5.1-05 — required admission stability coverage is absent

The admission tests cover active, missing, inactive, and mismatched template
mappings ([test_document_service.py:659](/home/test/documind/tests/test_domain/test_document_service.py:659)),
but do not cover a later template activation or mapping change leaving an
already-admitted version unchanged.

## Task 5.2 — validated normalized-content input

### T5.2-01 — normalization revision is not verified against persisted version metadata

NormalizedDocumentSource accepts any non-empty artifact normalization revision
([processing_service.py:173](/home/test/documind/src/documind/services/processing_service.py:173)).
PostgresNormalizedDocumentSource loads the version but passes neither
DocumentVersion.normalization_revision nor another expected revision to the
reader ([stage_store.py:507](/home/test/documind/src/documind/workflows/stage_store.py:507),
[stage_store.py:539](/home/test/documind/src/documind/workflows/stage_store.py:539)).
An artifact with a different non-empty revision is accepted.

### T5.2-02 — missing canonical artifact fields are silently substituted rather than rejected

The reader defaults a missing text to an empty string, missing blocks to an
empty list, and the other canonical outputs to empty lists
([processing_service.py:164](/home/test/documind/src/documind/services/processing_service.py:164),
[processing_service.py:178](/home/test/documind/src/documind/services/processing_service.py:178),
[processing_service.py:185](/home/test/documind/src/documind/services/processing_service.py:185)).
For example, an artifact with no text and a SHA-256 for the empty string can
pass. Missing or malformed canonical normalization input is required to be an
integrity error.

### T5.2-03 — pages, offset map, language evidence, and parser attempts are not structurally validated

Those fields are returned directly from decoded JSON without type or content
validation ([processing_service.py:185](/home/test/documind/src/documind/services/processing_service.py:185)).
Malformed values can therefore become a canonical chunking input.

## Task 5.3 — chunk algorithms and durable writer

### T5.3-01 — production chunking uses a placeholder tokenizer, not the configured pinned BGE-M3 tokenizer

The worker constructs _WhitespaceTokenizer, whose own docstring states that
production must replace it with a BGE-M3 adapter
([worker.py:284](/home/test/documind/src/documind/workflows/worker.py:284)).
Its whitespace-tokenizer-v1 digest cannot satisfy a profile pinned to a
BGE-M3 tokenizer digest.

### T5.3-02 — production worker cannot execute vector-semantic chunking or its configured fallback

The worker injects no sentence segmenter, sentence embedder, or fallback
profile mapping ([worker.py:316](/home/test/documind/src/documind/workflows/worker.py:316)).
Vector chunking requires both dependencies
([chunking_service.py:282](/home/test/documind/src/documind/services/chunking_service.py:282)),
and fallback resolution requires a loaded fallback profile
([chunking_service.py:155](/home/test/documind/src/documind/services/chunking_service.py:155)).
Thus the live worker cannot perform the required BGE-M3 semantic split or
restart under an explicitly configured recursive fallback.

### T5.3-03 — default fixed overlap is zero instead of 50 tokens

Both profile construction paths default overlap_tokens to 0
([chunk.py:159](/home/test/documind/src/documind/workflows/activities/chunk.py:159),
[stage_store.py:580](/home/test/documind/src/documind/workflows/stage_store.py:580)).
This conflicts with the required default fixed strategy of 512-token windows
with a 50-token overlap.

### T5.3-04 — recursive splitting omits the required character fallback and coverage assertion

The recursive separator sequence ends at a space and the terminal case simply
partitions tokenizer tokens
([chunking_service.py:191](/home/test/documind/src/documind/services/chunking_service.py:191),
[chunking_service.py:325](/home/test/documind/src/documind/services/chunking_service.py:325)).
There is no character-level split and no assertion that all source units are
covered before the result is returned.

### T5.3-05 — vector and paragraph modes do not enforce the required 100–1,024-token bounds

Profile validation accepts arbitrary positive min_tokens and a maximum no
smaller than that minimum
([chunking_service.py:445](/home/test/documind/src/documind/services/chunking_service.py:445)).
The PostgreSQL profile source defaults min_tokens to 1 and does not provide a
default maximum ([stage_store.py:583](/home/test/documind/src/documind/workflows/stage_store.py:583)).
Vector mode uses those values directly
([chunking_service.py:312](/home/test/documind/src/documind/services/chunking_service.py:312));
paragraph mode likewise derives its maximum from the profile
([chunking_service.py:207](/home/test/documind/src/documind/services/chunking_service.py:207)).

### T5.3-06 — paragraph short-span merging can produce chunks over the configured maximum

After an oversize block is split, paragraph mode merges a short final span into
the preceding span without rechecking the configured maximum
([chunking_service.py:258](/home/test/documind/src/documind/services/chunking_service.py:258)).
An oversize unbroken paragraph can consequently return a merged chunk larger
than the maximum.

### T5.3-07 — fallback chunks and stage metadata disagree about the effective profile

When vector fallback succeeds, ChunkingService builds rows with the fallback
profile ([chunking_service.py:125](/home/test/documind/src/documind/services/chunking_service.py:125),
[chunking_service.py:143](/home/test/documind/src/documind/services/chunking_service.py:143)).
The activity nevertheless writes and reports them under the requested profile
ID, and reports only an effective strategy—not an effective profile ID or
fallback reason ([chunk.py:108](/home/test/documind/src/documind/workflows/activities/chunk.py:108)).
This breaks requested/effective-profile provenance and can make replay look up
the wrong rows.

### T5.3-08 — multi-section paragraph chunks retain only the first section path

Chunk construction records all covered block IDs, but writes only the first
covered block's section path
([chunking_service.py:509](/home/test/documind/src/documind/services/chunking_service.py:509)).
A chunk spanning blocks from multiple sections loses the remaining section
provenance.

### T5.3-09 — durable chunk replay accepts partially divergent immutable rows

_PostgresChunkWriter compares only ordinal, offsets, and content hash on replay
([worker.py:563](/home/test/documind/src/documind/workflows/worker.py:563)).
It does not compare deterministic ID, content, page spans, section path, block
IDs, token count, profile revision, or embedding digest. A mismatch in those
immutable fields can be accepted as an identical retry.

### T5.3-10 — required durable writer scenarios are untested

The sole writer-specific test manually raises ChunkWriterConflictError
([test_chunking_service.py:538](/home/test/documind/tests/test_services/test_chunking_service.py:538)).
There is no database-backed coverage for identical persisted retries,
same-ordinal/same-span conflicts, or a crash between the domain write and
stage completion.

## Task 5.4 — logical-role LiteLLM boundary

### T5.4-01 — external-route consent and secret authorization are not validated

LLMService checks only whether a route with a secret reference has a non-null
consent ID ([llm_service.py:204](/home/test/documind/src/documind/services/llm_service.py:204)).
PostgresRouteResolver copies the consent ID and secret reference without
loading the consent policy or checking that it is active and permits that
reference ([llm_adapters.py:55](/home/test/documind/src/documind/services/llm_adapters.py:55)).
An external provider route with no secret reference also bypasses this guard,
because provider kind is not validated as part of route validation
([llm_service.py:371](/home/test/documind/src/documind/services/llm_service.py:371)).

### T5.4-02 — structured output is not strict Draft 2020-12 validation and returns no validation evidence

The implementation only checks JSON-object shape, top-level required keys, and
top-level primitive property types
([llm_service.py:386](/home/test/documind/src/documind/services/llm_service.py:386)).
It does not evaluate nested schemas, arrays/items, additionalProperties,
numeric/string constraints, references, formats, or Draft 2020-12 validation
evidence.

### T5.4-03 — credential clearing is ineffective and repair loses the external credential

The first immutable LLMRequest retains the credential
([llm_service.py:216](/home/test/documind/src/documind/services/llm_service.py:216)).
Setting only the local credential variable to None
([llm_service.py:247](/home/test/documind/src/documind/services/llm_service.py:247))
does not clear it from that request. The repair request then receives the
cleared local value ([llm_service.py:291](/home/test/documind/src/documind/services/llm_service.py:291)),
so an external route cannot complete the permitted bounded repair.

### T5.4-04 — provider exceptions cross the logical-role boundary unchanged

After error auditing, LLMService.invoke() re-raises the original adapter
exception ([llm_service.py:252](/home/test/documind/src/documind/services/llm_service.py:252)).
Provider exception text can escape instead of producing a safe model-route
error.

### T5.4-05 — required model audit records are absent in the live application and on structured-output exhaustion

Application startup configures the live LLMService with audit_sink=None
([main.py:250](/home/test/documind/src/documind/main.py:250)).
Additionally, structured-output exhaustion raises before reaching the normal
audit block ([llm_service.py:332](/home/test/documind/src/documind/services/llm_service.py:332)).
The required content-free audit data is not emitted for either path.

## Task 5.5 — enrichment records, templates, and graph facts

### T5.5-01 — configured production enrichment is non-functional

_build_enrichment_service() supplies a resolver that always returns None, an
LLM stub that always raises, an empty graph-fact stub, and a version loader
whose chunk/template reads and writes are placeholders
([worker.py:341](/home/test/documind/src/documind/workflows/worker.py:341)).
EnrichmentService.enrich() returns as soon as that resolver supplies no route
([enrichment_service.py:166](/home/test/documind/src/documind/services/enrichment_service.py:166)).
The live worker therefore cannot persist type suggestions, structured
extractions, proposals, or graph facts.

### T5.5-02 — enrichment neither loads canonical normalized content nor uses a version/tombstone lock

The VersionLoader protocol lacks operations for canonical normalized content and
locking ([enrichment_service.py:48](/home/test/documind/src/documind/services/enrichment_service.py:48)),
and the coordinator loads only version metadata and chunks
([enrichment_service.py:178](/home/test/documind/src/documind/services/enrichment_service.py:178)).
This does not satisfy the required locked canonical-content input.

### T5.5-03 — stored model-route provenance can identify a route different from the one invoked

The coordinator reads an EXTRACT route before invoking the LLM
([enrichment_service.py:166](/home/test/documind/src/documind/services/enrichment_service.py:166)),
while each LLMService.invoke() resolves a route again. It persists the
preliminary ID rather than LLMResult.route_revision_id for suggestions
([enrichment_service.py:243](/home/test/documind/src/documind/services/enrichment_service.py:243)),
extractions ([enrichment_service.py:309](/home/test/documind/src/documind/services/enrichment_service.py:309)),
proposals ([enrichment_service.py:382](/home/test/documind/src/documind/services/enrichment_service.py:382)),
and graph facts ([enrichment_service.py:446](/home/test/documind/src/documind/services/enrichment_service.py:446)).

### T5.5-04 — template extraction does not enforce the active template, full schema, field dictionary, or field source spans

Any non-null loaded template is used without checking its active state
([enrichment_service.py:192](/home/test/documind/src/documind/services/enrichment_service.py:192)).
Its JSON Schema is reduced to top-level required and properties
([enrichment_service.py:272](/home/test/documind/src/documind/services/enrichment_service.py:272))
and then shallowly checked
([enrichment_service.py:491](/home/test/documind/src/documind/services/enrichment_service.py:491)).
field_dictionary is never read. Persisted source spans are generated as complete
ranges of every chunk rather than validated model-provided field spans
([enrichment_service.py:303](/home/test/documind/src/documind/services/enrichment_service.py:303),
[enrichment_service.py:509](/home/test/documind/src/documind/services/enrichment_service.py:509)).

### T5.5-05 — invalid structured extraction creates no validation_failed evidence row

If the structured LLM invocation raises or exhausts repair, extraction sets
state to failed and returns
([enrichment_service.py:291](/home/test/documind/src/documind/services/enrichment_service.py:291)).
save_extraction() is reached only after a result was already marked
structured-valid ([enrichment_service.py:303](/home/test/documind/src/documind/services/enrichment_service.py:303)).
The required auditable validation-failed record is absent for invalid output.

### T5.5-06 — template proposals are not independently schema- or span-validated

Proposal persistence accepts the same limited LLM-boundary result and writes the
candidate JSON Schema and sample spans directly
([enrichment_service.py:373](/home/test/documind/src/documind/services/enrichment_service.py:373)).
Candidate Draft 2020-12 validity and authorization of the sample spans are not
checked.

### T5.5-07 — graph-fact evidence spans are not validated against source chunk offsets

RawFact.evidence_span is an unconstrained string
([graph_fact_service.py:29](/home/test/documind/src/documind/services/graph_fact_service.py:29)).
Validation only rejects blank strings
([graph_fact_service.py:233](/home/test/documind/src/documind/services/graph_fact_service.py:233)),
and the persistence API receives only chunk IDs—not their offset ranges
([graph_fact_service.py:101](/home/test/documind/src/documind/services/graph_fact_service.py:101)).

### T5.5-08 — entity and literal normalization is incomplete

The entity normalized key case-folds the type but the lookup and stored
entity_type keep its raw model-provided spelling
([graph_fact_service.py:51](/home/test/documind/src/documind/services/graph_fact_service.py:51),
[graph_fact_service.py:256](/home/test/documind/src/documind/services/graph_fact_service.py:256)).
Literal type and unit are only case-folded, without the required NFC and
collapsed-whitespace canonicalization
([graph_fact_service.py:63](/home/test/documind/src/documind/services/graph_fact_service.py:63)).
Equivalent facts can therefore obtain different stored keys/entities.

### T5.5-09 — corroboration and conflict handling discard independent provenance

The physical identity includes source_chunk_id, but _find_existing_fact()
intentionally omits it
([graph_fact_service.py:286](/home/test/documind/src/documind/services/graph_fact_service.py:286)).
A matching fact from a different chunk increments the first row's corroboration
count instead of retaining the second source row
([graph_fact_service.py:199](/home/test/documind/src/documind/services/graph_fact_service.py:199)).
For the same source, a conflict group only increments a result counter; no
separate conflicting fact is retained
([graph_fact_service.py:204](/home/test/documind/src/documind/services/graph_fact_service.py:204)).

### T5.5-10 — gleaning-pass enforcement is neither chunk-set-specific nor concurrency-safe

The pass-one lookup is scoped only by version and route revision
([graph_fact_service.py:123](/home/test/documind/src/documind/services/graph_fact_service.py:123)),
not the authorized chunk set. It has no row lock or database constraint, so
concurrent pass-one transactions can both observe no result and persist.

### T5.5-11 — invalid graph-fact output creates no validation evidence and can partially persist a mixed response

Malformed fact UUIDs are silently dropped during parsing
([enrichment_service.py:461](/home/test/documind/src/documind/services/enrichment_service.py:461)).
Other invalid facts are silently counted as skipped while the remaining facts
continue to persistence
([graph_fact_service.py:139](/home/test/documind/src/documind/services/graph_fact_service.py:139)).
Neither path records validation evidence, and a mixed invalid response can still
produce graph facts.

### T5.5-12 — required graph-fact persistence-path tests are absent

test_graph_fact_service.py tests normalizers and input validation, but not
database-backed retry idempotency, independent corroboration, conflict
retention, invalid spans, or gleaning concurrency
([test_graph_fact_service.py:37](/home/test/documind/tests/test_services/test_graph_fact_service.py:37)).
The enrichment tests use fake loaders and graph-fact services, so they do not
exercise the production persistence path.

## Task 5.6 — workflow and worker composition

### T5.6-01 — Task 5 crosses its stated lifecycle boundary into Tasks 6 completion

Task 5 is specified to stop while the version remains processing. The workflow
instead invokes project, verify, and complete after enrichment
([document_version.py:217](/home/test/documind/src/documind/workflows/document_version.py:217))
and returns state="completed"
([document_version.py:235](/home/test/documind/src/documind/workflows/document_version.py:235)).

### T5.6-02 — chunk and enrich lack periodic heartbeats and write-adjacent tombstone checks

Both activities heartbeat once before execution and once after it
([chunk.py:83](/home/test/documind/src/documind/workflows/activities/chunk.py:83),
[enrich.py:58](/home/test/documind/src/documind/workflows/activities/enrich.py:58))
instead of using the periodic heartbeat loop used by inspection
([inspect.py:77](/home/test/documind/src/documind/workflows/activities/inspect.py:77)).
Chunking performs its domain write after the initial guard and before its final
guard ([chunk.py:89](/home/test/documind/src/documind/workflows/activities/chunk.py:89)).
A tombstone can therefore be committed between the guard and write, and work
lasting over the 30-second heartbeat timeout has no periodic heartbeat.

### T5.6-03 — enrichment stage output never records resolved model-route revisions

EnrichmentResult has no route_revision_ids field
([enrichment_service.py:31](/home/test/documind/src/documind/services/enrichment_service.py:31)).
The activity reads it with getattr(..., []), yielding an empty list in every
stage output ([enrich.py:75](/home/test/documind/src/documind/workflows/activities/enrich.py:75)).
The durable stage store persists only suggestion and extraction-status metadata
([stage_store.py:635](/home/test/documind/src/documind/workflows/stage_store.py:635)).

### T5.6-04 — parse-stage document text is placed in Temporal workflow history

The parse activity returns asdict(ParseResult)
([parse.py:48](/home/test/documind/src/documind/workflows/activities/parse.py:48)).
ParseResult includes text and pages
([ocr_service.py:53](/home/test/documind/src/documind/services/ocr_service.py:53)),
and the workflow receives that content-bearing result before hashing it
([document_version.py:185](/home/test/documind/src/documind/workflows/document_version.py:185)).
This violates the checksum-only workflow-history requirement.

### T5.6-05 — required workflow and bootstrap tests do not exercise the durable production path

Replay tests use InMemoryStageReplayStore and synthetic dictionaries
([test_document_version.py:239](/home/test/documind/tests/test_workflows/test_document_version.py:239)).
Queue tests only inspect stage_configurations()
([test_worker_bootstrap.py:35](/home/test/documind/tests/test_worker_bootstrap.py:35)).
There is no tombstone-interruption test despite the module's stated coverage.
The required production replay, chunk-before-enrich provenance, actual worker
queue, and tombstone scenarios remain untested.

## Task 5.7 — verification gate

### T5.7-01 — checked lint verification is currently false

The exact ruff check src tests command currently reports 51 errors. These
include Task 5 code in processing_service.py (two unused normalization
variables), as well as errors elsewhere within the command's required scope.
The checked Task 5.7 verification claim is therefore not true for the current
worktree.

### T5.7-02 — checked formatting verification is currently false

The exact ruff format --check src tests command reports 56 files that would be
reformatted, including Task 5 files stage_store.py, worker.py, and
tests/test_workflows/test_document_version.py.

### T5.7-03 — checked whitespace verification is currently false

git diff --check reports trailing blank-line errors in the current shared
worktree (src/documind/database.py, src/documind/services/audit_service.py, and
tests/test_services/test_webhook_service.py). The exact Task 5.7 command
therefore does not pass.

### T5.7-04 — the passing focused tests do not prove the claimed production vertical slice

The focused tests use fake LLM, route, credential, enrichment-loader,
graph-fact, and replay-store dependencies. They do not execute the configured
worker composition, which is non-functional for enrichment (T5.5-01) and
cannot execute pinned BGE-M3/vector chunking (T5.3-01 and T5.3-02). They also
do not cover the required durable crash boundary, legacy migration upgrade,
corroboration/conflict persistence, or tombstone interruption paths.

