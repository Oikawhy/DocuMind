# Task 8 gap audit

## Scope and method

This audit compares **Task 8: LangGraph Agent RAG Engine** in `docs/plans/implement.md` and its referenced §7 RAG specification with the current RAG graph, nodes, tools, prompt registry, service entry point, startup wiring, chat integration, and Task 8 tests. It records verified gaps only; it intentionally contains no remediation guidance.

## Production graph and service integration

### T8-01 — The chat endpoint cannot invoke the implemented RAG service

`post_chat()` calls `rag_service.run_rag_query()` with `query`, `principal`, `document_ids`, `mode`, and `session_history` keyword arguments ([chat.py](src/documind/api/chat.py#L345-L355)). `RAGService.run_rag_query()` instead accepts `question`, `principal_subject`, `session_id`, and `chat_history`, and accepts none of the former keyword names ([service.py](src/documind/rag/service.py#L60-L73)). The call therefore raises `TypeError`, which the endpoint converts to `RAG_QUERY_FAILED` ([chat.py](src/documind/api/chat.py#L401-L407)).

This also differs from Task 8's declared public interface, which requires `run_rag_query(question, principal, session_id?, locale)`.

### T8-02 — The chat endpoint and RAG service disagree on the response type and shape

The RAG service returns a `RAGResponse` dataclass ([service.py](src/documind/rag/service.py#L20-L35), [service.py](src/documind/rag/service.py#L115-L115)). The chat endpoint treats that result as a dictionary and repeatedly calls `.get()` on it ([chat.py](src/documind/api/chat.py#L356-L400)). Even if the call signature matched, this raises `AttributeError` and enters the same `RAG_QUERY_FAILED` fallback.

Further, `RAGResponse` has no claims, plan, retrieval IDs, prompt revisions, schema-validation outcomes, retry count, revision count, or timing fields, although the chat endpoint attempts to read all of those values into `AgentRun` ([chat.py](src/documind/api/chat.py#L452-L466)). Task 8 requires those run-record values to be retained.

### T8-03 — The graph invokes a retrieval method that does not exist on the production retrieval service

The typed retrieval tool calls `retrieval_service.search()` ([retrieve_evidence.py](src/documind/rag/tools/retrieve_evidence.py#L82-L85)). The production `RetrievalService` exposes `retrieve()` and `compare()`; it has no `search()` implementation ([retrieval_service.py](src/documind/services/retrieval_service.py#L461-L461), [retrieval_service.py](src/documind/services/retrieval_service.py#L735-L735)). The retrieval tool catches the resulting exception as a degraded query variant, so the graph proceeds with no candidates rather than performing retrieval.

### T8-04 — The graph drops the authenticated principal before retrieval

The graph wrapper always passes `principal=None` to the retrieval orchestrator ([graph.py](src/documind/rag/graph.py#L74-L77)). The tool forwards that value to the retrieval service ([retrieve_evidence.py](src/documind/rag/tools/retrieve_evidence.py#L82-L85)), while the production retrieval pipeline requires a `Principal` to build its authorization context ([retrieval_service.py](src/documind/services/retrieval_service.py#L388-L392), [retrieval_service.py](src/documind/services/retrieval_service.py#L461-L465)). `AgentState` retains only the principal subject string, not the authenticated principal or an authorization context usable by the service.

### T8-05 — Production graph construction supplies no allowed document set, so the RAG Permission Guard rejects every candidate

`main.py` builds the graph without `allowed_document_ids` ([main.py](src/documind/main.py#L270-L277)). `build_graph()` replaces the missing value with an empty set ([graph.py](src/documind/rag/graph.py#L53-L55)) and passes that set to both Permission Guard and Version Resolver ([graph.py](src/documind/rag/graph.py#L79-L83), [graph.py](src/documind/rag/graph.py#L97-L100)). The guard requires every canonical candidate's document ID to be in that set ([permission_guard.py](src/documind/rag/tools/permission_guard.py#L108-L114)), so every production candidate is filtered.

### T8-06 — The graph's Permission Guard is not a deterministic authorization recheck

The Task 8 graph is required to recheck the principal, labels, lifecycle, hold, and tombstone state from canonical metadata. The guard tool checks only chunk existence, version lifecycle, membership in a caller-provided document-ID set, and a document tombstone ([permission_guard.py](src/documind/rag/tools/permission_guard.py#L82-L123)). It does not receive `AuthorizationService`, `PolicyService`, the principal's groups/identity state, document labels, or legal holds. The static `allowed_document_ids` mechanism is not a current authorization decision.

### T8-07 — Evidence cache lifetime is graph-wide instead of request-scoped, and the first completed request permanently expires it

`build_graph()` allocates one `EvidenceCache` while the compiled graph is constructed ([graph.py](src/documind/rag/graph.py#L53-L55)); all invocations of that compiled graph close over that same cache. `response_formatter` expires it on the first normal completion ([graph.py](src/documind/rag/graph.py#L135-L139)). On later requests, `load_evidence()` attempts `EvidenceCache.put()` on the expired cache ([load_evidence.py](src/documind/rag/tools/load_evidence.py#L116-L118)), which raises by contract ([state.py](src/documind/rag/state.py#L342-L345)). Concurrent requests also share the same cache and encryption key rather than having isolated evidence caches.

### T8-08 — Abnormal graph exits leave evidence cache contents live

The only cache-expiry call is inside the response-formatter wrapper ([graph.py](src/documind/rag/graph.py#L135-L139)). Timeout and exception handling occur in `RAGService` after `ainvoke()` and return immediately without cache expiry ([service.py](src/documind/rag/service.py#L88-L113)). Any graph exception or runtime timeout before the formatter retains encrypted evidence in the shared cache described in T8-07.

## Graph routing and loop controls

### T8-09 — Rewrite and targeted-expansion routes bypass the nodes required by the graph contract

Both `rewrite` and `targeted_expansion` from Relevance Grader route directly back to `retrieval` ([graph.py](src/documind/rag/graph.py#L191-L217)). Task 8 requires the rewrite path to return to Query Rewriter and the targeted-expansion path to return to Planner. The current graph neither generates a new query variant for rewrite nor obtains the required bounded, in-scope plan for an expansion.

### T8-10 — Targeted expansions are never counted or bounded in a running graph

`targeted_expansions` is initialized to zero ([state.py](src/documind/rag/state.py#L300-L304)) and is only read by the Relevance Grader guard ([relevance_grader.py](src/documind/rag/nodes/relevance_grader.py#L178-L188)). No node writes an incremented value. A grader can therefore repeatedly return `targeted_expansion` without reaching its specified two-expansion limit; it instead follows the direct retrieval loop until the unrelated retrieval-attempt limit intervenes.

### T8-11 — The hallucination revision limit can send unresolved revision requests to citation verification

When the revision count is already at its maximum, `hallucination_grader_node()` only abstains if a claim's pre-existing `grounded` flag is false ([hallucination_grader.py](src/documind/rag/nodes/hallucination_grader.py#L45-L61)). It then still calls the grader. If that grader requests another revision, the node increments the count beyond the limit ([hallucination_grader.py](src/documind/rag/nodes/hallucination_grader.py#L164-L180)); the graph sees the now-over-limit number and routes directly to Citation Verifier rather than abstaining ([graph.py](src/documind/rag/graph.py#L227-L238)). Thus a model-reported unsupported or partial claim can remain in the draft after the revision cap.

### T8-12 — Invalid citation verification does not produce an abstention

The citation route sends both valid and invalid verification results to the formatter ([graph.py](src/documind/rag/graph.py#L246-L256)), without setting `abstention_reason` when `all_valid` is false. The formatter emits an ordinary answer whenever a draft exists and there is no abstention reason ([response_formatter.py](src/documind/rag/nodes/response_formatter.py#L47-L63)); it merely serializes the surviving verified citations. This permits an answer with invalid or missing citations, contrary to Task 8's required abstention on invalid citation.

### T8-13 — Failure and malformed-output paths in the hallucination grader are fail-open

If the grader returns unparsable output or raises an exception, the node records `HallucinationGrade(all_grounded=True)` and continues ([hallucination_grader.py](src/documind/rag/nodes/hallucination_grader.py#L132-L140), [hallucination_grader.py](src/documind/rag/nodes/hallucination_grader.py#L184-L190)). The graph then moves to citation verification. Task 8 requires failed required model routes and unsupported claims to end in a safe abstention or clarification, not a grounded pass.

### T8-14 — Generator schema failures are converted into a claim citing every evidence item

When the Generator's structured response cannot be parsed as JSON, it creates a synthetic claim from raw model text and assigns every reranked evidence ID to it ([generator.py](src/documind/rag/nodes/generator.py#L118-L134)). This bypasses the declared strict output schema and replaces the model's missing claim-to-evidence mapping with a blanket mapping. Task 8 requires every model output to pass the node schema and deterministic checks before it is used.

### T8-15 — Relevance Grader accepts unvalidated fallback JSON and unknown request kinds

When no validated structured result is returned, Relevance Grader directly parses `result.content` as JSON ([relevance_grader.py](src/documind/rag/nodes/relevance_grader.py#L114-L120)) but does not validate the parsed object against the output schema. Its loop guard returns unknown request kinds unchanged ([relevance_grader.py](src/documind/rag/nodes/relevance_grader.py#L174-L191)); the graph treats any unknown non-abstain kind as an `answer` path ([graph.py](src/documind/rag/graph.py#L192-L208)). Invalid model output can therefore proceed to generation rather than defaulting to a safe response.

### T8-16 — The comparison route bypasses the required schema-aware extraction stage

For an `answer` relevance result, a comparison route goes directly from Relevance Grader to Comparator ([graph.py](src/documind/rag/graph.py#L200-L208)). Extractor and Comparator independently route to Generator ([graph.py](src/documind/rag/graph.py#L219-L222)); there is no Extractor-to-Comparator path. This differs from the Task 8 / §7 graph contract, which resolves versions, runs schema-aware extraction, then performs the comparative analysis.

## Extraction, comparison, aggregation, and version resolution

### T8-17 — Schema-aware extraction always returns `pending_template` in the graph

The Extractor node constructs `ExtractStructuredInput(template_revision_id=None)` ([extractor.py](src/documind/rag/nodes/extractor.py#L43-L50)). The extraction tool calls a template loader only when both a loader and a non-null revision ID are present ([extract_structured.py](src/documind/rag/tools/extract_structured.py#L61-L66)); otherwise it unconditionally returns `pending_template=True`. Startup does not pass a template loader to `build_graph()` ([main.py](src/documind/main.py#L270-L277)). No extraction-route request can load an active approved template.

### T8-18 — The extraction model call bypasses both the injection-safety preamble and the signed-prompt registry

`extract_structured()` constructs its own system prompt and invokes `LLMService` directly ([extract_structured.py](src/documind/rag/tools/extract_structured.py#L86-L108)). It does not use `wrap_with_safety()`, a registered prompt template, prompt input/output validation, or prompt invocation recording. The injection-safety preamble is mandatory for every model invocation under §7.2 and Task 8.6.

### T8-19 — Extraction validation does not validate source spans against the authorized evidence text

The extraction tool only rejects source spans whose claimed `evidence_id` is absent from the input IDs ([extract_structured.py](src/documind/rag/tools/extract_structured.py#L124-L137)). It does not verify quoted text, offsets, field-dictionary rules, units, or that the spans are actually contained in the canonical authorized excerpts. The Task 8 extraction contract requires JSON-Schema, source-span, unit, and evidence-ID validation.

### T8-20 — Version comparison assigns evidence to versions by list position, not provenance

`compare_versions()` divides its evidence list in half and declares the first half to belong to the left version and the second half to the right ([compare_versions.py](src/documind/rag/tools/compare_versions.py#L62-L75)). Evidence IDs carry no version metadata in this tool, so retrieval ordering changes the comparison result and chunks from the wrong version can be compared. This is not the required deterministic comparison of explicitly resolved versions.

### T8-21 — Comparator prose claims are neither schema-validated nor restricted to valid evidence references

The Comparator invokes the QUERY role without an output schema ([comparator.py](src/documind/rag/nodes/comparator.py#L70-L81)). On non-JSON output it creates one claim from arbitrary model prose and assigns all reranked IDs as evidence ([comparator.py](src/documind/rag/nodes/comparator.py#L95-L102)); JSON claim evidence IDs are also copied with no authorized-set validation ([comparator.py](src/documind/rag/nodes/comparator.py#L83-L94)). Task 8 requires prose claims to have explicit, supplied evidence references and all model output to pass node schema and deterministic checks.

### T8-22 — Aggregation derives values from arbitrary text rather than validated fields

`aggregator_node()` scans every authorized excerpt using a broad number/unit regular expression ([aggregator.py](src/documind/rag/nodes/aggregator.py#L39-L60), [aggregator.py](src/documind/rag/nodes/aggregator.py#L112-L158)). It assigns every match the generic field name `value`, infers the operation from plan prose, and does not use an approved field rule or structured-extraction result. Numbers such as years, section numbers, IDs, and unrelated amounts can be aggregated together. This does not meet the Task 8 requirement to validate field types and apply aggregation over validated values.

### T8-23 — Aggregation permits incompatible-scale sums without conversion

The unit checker classifies units such as `m` and `cm` as one compatible group ([aggregate_values.py](src/documind/rag/tools/aggregate_values.py#L16-L29), [aggregate_values.py](src/documind/rag/tools/aggregate_values.py#L76-L98)), but `sum`, `avg`, `min`, and `max` operate on the unconverted decimal values and report the first non-empty unit ([aggregate_values.py](src/documind/rag/tools/aggregate_values.py#L139-L178)). For example, `1 m` and `100 cm` yield `101 m`. Task 8 allows mixed units only under an approved deterministic conversion table.

### T8-24 — Version Resolver cannot resolve planner document selectors expressed as document names

The Planner contract permits document names/selectors. `resolve_versions()` immediately executes `uuid.UUID(selector.document_id)` after checking membership in `allowed_document_ids` ([resolve_versions.py](src/documind/rag/tools/resolve_versions.py#L66-L80)); ordinary document names are not UUIDs and raise `ValueError`. The node does not catch that tool exception ([version_resolver.py](src/documind/rag/nodes/version_resolver.py#L53-L66)). This prevents required document/version selector resolution from the declarative plan.

### T8-25 — Version resolution is skipped unless a plan step contains both selectors

`version_resolver_node()` only creates a selector when a plan step supplies both `document_selector` and `version_selector` ([version_resolver.py](src/documind/rag/nodes/version_resolver.py#L33-L47)). A `resolve_versions` plan step with an omitted version selector, a retrieval selector, or a natural-language date/version constraint is ignored; the graph then proceeds without required resolved version references.

### T8-26 — Version Resolver can return a tombstoned or erased version as resolved

The explicit-version resolver selects by document/version number and treats any `COMPLETED` row as `resolved` ([resolve_versions.py](src/documind/rag/tools/resolve_versions.py#L139-L174)). It does not check `DocumentVersion.tombstone_generation`, the document's erased state, or version-scoped deletion tombstones. The latest and date-range queries similarly check only lifecycle ([resolve_versions.py](src/documind/rag/tools/resolve_versions.py#L109-L132), [resolve_versions.py](src/documind/rag/tools/resolve_versions.py#L177-L220)). Task 8 requires resolution to return authorized non-erased versions only.

## Citation provenance and authorization

### T8-27 — Citation verification never constructs canonical citation provenance for the response

For each claim/evidence pair, `citation_verifier_node()` initializes a citation with empty document/version IDs, version number zero, no page/section data, and no content hash ([citation_verifier.py](src/documind/rag/nodes/citation_verifier.py#L48-L82)). `verify_citations()` returns status records only, not canonical provenance ([verify_citations.py](src/documind/rag/tools/verify_citations.py#L58-L65), [verify_citations.py](src/documind/rag/tools/verify_citations.py#L89-L170)). The formatter consequently serializes citations containing empty provenance fields ([response_formatter.py](src/documind/rag/nodes/response_formatter.py#L75-L94)), despite Task 8 requiring canonical version/chunk/offset provenance.

### T8-28 — Final citation authorization is skipped when the graph uses its production empty document set

The citation tool only evaluates document membership when `allowed_document_ids` is truthy ([verify_citations.py](src/documind/rag/tools/verify_citations.py#L144-L153)). Production graph wiring supplies the empty set described in T8-05, making this check a no-op. There is no call to `AuthorizationService` in Citation Verifier, so it cannot enforce the required current-principal access recheck.

### T8-29 — Citation verification has no offsets or graph path to validate

`VerifyCitationsInput` contains claims, citation IDs, chunk IDs, hashes, evidence IDs, and principal subject only ([verify_citations.py](src/documind/rag/tools/verify_citations.py#L31-L39)). The graph supplies only `citation_id`, `claim_id`, `chunk_id`, and an empty hash ([citation_verifier.py](src/documind/rag/nodes/citation_verifier.py#L90-L108)). Page offsets, source spans, canonical document/version values, and graph-path provenance are not input to the verifier and therefore cannot be checked, although Task 8 requires all of them.

## Prompt, trace, persistence, and chat-memory contracts

### T8-30 — Prompt templates are not release artifacts in a signed manifest

The registry stores templates in memory and computes each SHA-256 from the same runtime Python string ([registry.py](src/documind/rag/prompts/registry.py#L20-L44), [templates.py](src/documind/rag/prompts/templates.py#L16-L31)). There is no signed manifest, signature verification, release-manifest integration, or policy-selected signed revision. `verify_integrity()` even accepts an empty stored hash as valid ([registry.py](src/documind/rag/prompts/registry.py#L32-L36)). This does not implement the §7.2 signed prompt-manifest boundary.

### T8-31 — Several prompt templates do not supply the mandatory input/output schemas

The specification requires every prompt artifact to carry input and output JSON Schemas. The Extractor, Comparator, and Session Compactor templates provide prompt text and role but omit both schemas, using the dataclass's empty defaults ([templates.py](src/documind/rag/prompts/templates.py#L242-L303), [templates.py](src/documind/rag/prompts/templates.py#L307-L327)). Comparator and Session Compactor also lack output schema enforcement at invocation.

### T8-32 — Prompt registry selection and invocation records do not reach the persisted AgentRun

Nodes append transient records to `PromptRegistry._invocation_log` ([registry.py](src/documind/rag/prompts/registry.py#L75-L98)), but `RAGService` never reads the registry, includes the records in state/response, or writes them through `write_trace`/audit ([service.py](src/documind/rag/service.py#L47-L115)). The chat endpoint expects `rag_result["prompt_revisions"]` ([chat.py](src/documind/api/chat.py#L457-L459)), but the RAG response has no such field (T8-02). Required per-run prompt revisions and schema-validation outcomes are not persisted.

### T8-33 — The `write_trace` tool is never used by the graph or RAG service

The only repository references to `write_trace()` are its implementation and focused unit tests; no graph node or `RAGService` invokes it. The graph also does not use the injected `audit_service` except in the static-document Permission Guard ([graph.py](src/documind/rag/graph.py#L79-L83)). Task 8 requires content-free trace/audit records for agent execution and its model/prompt schema outcomes.

### T8-34 — Session compaction is an unsafeguarded model invocation and cannot be persisted by the current chat schema

`_maybe_compact_session()` invokes the KEYWORDS model with an inline prompt, not the signed Session Compactor template and not the injection-safety wrapper ([chat.py](src/documind/api/chat.py#L217-L244)). It then persists a `ChatMessage` with `role="system"` ([chat.py](src/documind/api/chat.py#L248-L255)), while the ORM check constraint permits only `user` or `assistant` ([chat.py model](src/documind/models/chat.py#L54-L63)). At the 30-message threshold, the compaction write violates the database schema rather than providing the required bounded session summary.

### T8-35 — Session-memory loading can exceed the specified 4,096-token cap

`_load_session_messages()` deducts the stored summary from the 4,096-token budget but returns the entire summary even when that deduction takes the budget negative ([chat.py](src/documind/api/chat.py#L130-L147)). It also estimates tokens from byte length rather than enforcing a configured token budget. The returned summary plus messages can exceed the §7.1 maximum context.

## Required integration and acceptance coverage

### T8-36 — The required complete-graph and fixture coverage is absent

`tests/test_rag/test_graph_integration.py` compiles the graph and executes only the clarification direct path ([test_graph_integration.py](tests/test_rag/test_graph_integration.py#L53-L109)); it does not execute a complete authorized simple-query, comparison, aggregation, extraction, retrieval retry, targeted expansion, citation-invalid, or session-compaction graph path. The chat API test temporarily forces `app.state.rag_service = None` and verifies only the RAG-unavailable abstention ([test_chat.py](tests/test_api/test_chat.py#L122-L151)).

Accordingly, Task 8.6's required mocked-LLM full graph execution and its listed fixtures—permission divergence, prompt injection, relevance retry, targeted expansion, missing template, numeric aggregation, citation invalidation, model-route denial, and session compaction—do not exercise the implemented production integrations. The interface failures in T8-01 through T8-05 are therefore not covered by the focused tests.
