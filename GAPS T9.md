# Task 9 Gap Audit

Scope: Task 9 in `docs/plans/implement.md` (chat API and memory, webhook service and delivery, console shell, router wiring, and required tests). The findings below are observed implementation gaps only; this report deliberately contains no remediation proposals.

## Chat and RAG integration

### T9-01 — `POST /v1/chat` is incompatible with the production RAG service

`api/chat.py` calls `rag_service.run_rag_query()` with `query`, `principal`, `document_ids`, `mode`, and `session_history` keyword arguments, then treats the result as a dictionary via `.get()` (`src/documind/api/chat.py:347-360`). The production `RAGService.run_rag_query()` accepts `question`, `principal_subject`, `session_id`, `session_summary`, and `chat_history`, and returns a `RAGResponse` dataclass (`src/documind/rag/service.py:60-115`).

The first call raises `TypeError` for the unexpected keyword arguments; if its call signature were reached, the subsequent `.get()` calls would also fail on `RAGResponse`. The route catches both failures and returns the `RAG_QUERY_FAILED` abstention, so a wired RAG service cannot provide Task 9 chat answers.

### T9-02 — enabled chat cannot persist `AgentRun.confidence`

The chat route supplies the string API confidence value (`"high"`, `"medium"`, or `"low"`) to `AgentRun.confidence` (`src/documind/api/chat.py:441-465`). The ORM declares that column as `float` (`src/documind/models/chat.py:107-113`) and the latest migration creates it as `DOUBLE PRECISION` (`alembic/versions/007_task_1_2_gap_remediation.py:345-364`).

On a migrated PostgreSQL database, every enabled chat transaction attempts to insert a text confidence into a floating-point column. The API tests use mocks and therefore do not exercise this database write.

### T9-03 — session compaction writes a database-invalid `system` message

`_maybe_compact_session()` persists the summary as `ChatMessage(role="system")` (`src/documind/api/chat.py:246-253`). The current chat-message constraint permits only `user` and `assistant` roles (`src/documind/models/chat.py:54-63`; `alembic/versions/007_task_1_2_gap_remediation.py:121-125`).

At the 30-message compaction threshold, the transaction attempts an invalid insert and cannot commit. This makes the required KEYWORDS compaction unavailable to real sessions.

### T9-04 — the memory loader can return context above the 4,096-token limit

The memory loader subtracts the estimated summary size from `budget`, but always returns the whole summary even when it alone exceeds `max_tokens` (`src/documind/api/chat.py:139-154`). It neither bounds nor omits an oversized summary. The returned `summary + messages` context can therefore exceed Task 9's 4,096-token maximum.

### T9-05 — session pagination is neither ordering-correct nor opaque

`GET /v1/chat/sessions` orders rows by `created_at DESC`, but applies its cursor as `ChatSession.id < UUID(cursor)` and emits a raw UUID (`src/documind/api/chat.py:527-570`). UUID order is unrelated to the result order, so pages can skip or duplicate sessions. The cursor is also not an opaque signed `(created_at, id)` continuation token.

### T9-06 — session deletion is not the required erasure operation and audit evidence is optional

`DELETE /v1/chat/sessions/{id}` immediately overwrites message content and returns `204` (`src/documind/api/chat.py:638-686`); it creates no operation record or asynchronous erasure operation. Its audit write occurs only when `app.state.audit_service` is non-null, and audit failure is not part of the session mutation transaction. The required erasure operation and required audit evidence are consequently absent.

### T9-07 — retention cleanup has no production scheduling or authoritative legal-hold check

`cleanup_expired_sessions()` exists only as a callable helper (`src/documind/workflows/maintenance/chat_retention.py:23-108`). It is not imported or registered by the worker (`src/documind/workflows/worker.py`), and there is no `ChatRetentionWorkflow` or Temporal schedule. Thus expiry cleanup never runs in production.

The helper also treats legal holds as a caller-supplied `held_subjects` set rather than querying authoritative active holds (`src/documind/workflows/maintenance/chat_retention.py:23-62`). No production caller supplies that set. Its retention audit write is optional as well.

### T9-08 — persisted chat records omit response metadata needed for session history and the full agent-run record

The assistant `ChatMessage` is written with only role, text, and token count (`src/documind/api/chat.py:420-428`), leaving its available `citation_ids`, `confidence`, and `trace_id` fields unset (`src/documind/models/chat.py:44-52`). Reopened sessions therefore have no persisted citations, confidence, or trace linkage; the console explicitly replaces all loaded-message citations with an empty list (`console/src/pages/Chat.tsx:73-85`).

The same route leaves `AgentRun.graph_path`, `prompt_revisions`, `safe_failure_code`, and the other structured state unavailable from the RAG result unpopulated, while it stores only route/path in `graph_state_checkpoint` (`src/documind/api/chat.py:441-472`). This is not the required persisted agent-run record described by the Task 9 chat-memory dependency.

### T9-09 — model-backed chat/compaction routes can retain a closed OpenBao client

The application closes `secret_client` before it creates `OpenBaoCredentialResolver(secret_client)` (`src/documind/main.py:103-140`, `src/documind/main.py:236-255`). Any active model route that needs a secret reference consequently resolves through a closed `SecretService` HTTP client. This prevents those RAG and KEYWORDS-compactor model calls from functioning.

## Webhooks

### T9-10 — webhook registration does not implement the required DNS pinning / rebinding defense

`validate_target_url()` performs global `socket.getaddrinfo` resolution and returns a selected IP (`src/documind/services/webhook_service.py:59-143`), but `register_webhook()` discards that result (`src/documind/services/webhook_service.py:184-199`). No resolved IP exists in the webhook model, and `deliver()` opens an ordinary `httpx` client to the hostname (`src/documind/services/webhook_service.py:243-309`).

Delivery therefore performs a new unpinned DNS resolution with no controlled resolver or connection-time address recheck. The required DNS-rebinding protection is not present.

### T9-11 — the webhook secret-provisioning path is absent

The registration API accepts a caller-provided `secret_reference` and the service only stores that string (`src/documind/api/webhooks.py:63-95`; `src/documind/services/webhook_service.py:168-202`). Although `SecretService.put_secret()` exists (`src/documind/services/secret_service.py:90-113`), no webhook registration code uses it. The required registration-time OpenBao secret write/reference creation does not occur.

### T9-12 — webhook delivery retries are not executable and the recorded state cannot be selected for retry

`deliver()` performs an HTTP request immediately and writes a failed first attempt as `state="failed"`, only setting `next_attempt_at` (`src/documind/services/webhook_service.py:325-356`). There is no retry executor or any query of due delivery records anywhere in `src/`. The database retry index is limited to `state = 'pending'` (`alembic/versions/007_task_1_2_gap_remediation.py:244-251`), so failed rows with a next attempt time are not eligible for the intended index either.

The delay calculation also schedules the failed first delivery using `get_retry_delay(2)` (60 seconds), not the first 10-second delay (`src/documind/services/webhook_service.py:334-339`). The Task 9 three-attempt schedule and durable retry execution are therefore absent.

### T9-13 — webhook dispatcher uses exact matching and is not integrated into the outbox worker

`WebhookDispatcher` imports `fnmatch` but matches subscriptions with exact membership (`event_type in webhook.events`) (`src/documind/services/webhook_dispatcher.py:10`, `46-54`). Event globs do not work.

More importantly, the production worker creates only the Redis outbox dispatcher and the document-version Temporal consumer (`src/documind/workflows/worker.py:100-214`). It never constructs or calls `WebhookDispatcher`. The existing Redis consumer accepts only `io.documind.document-version.accepted.v1` and starts a document workflow (`src/documind/workflows/maintenance/outbox_dispatcher.py:139-188`); it does not dispatch webhook subscriptions. No outbox event can reach the webhook delivery service in production.

### T9-14 — delivery attempts do not create required audit evidence and concurrent attempts are not serialized

`WebhookService` has no `AuditService` dependency and writes only structured logs after an attempt (`src/documind/services/webhook_service.py:343-365`). Required webhook-delivery audit evidence is missing.

Attempt number is calculated with an unlocked count query before the HTTP call and the delivery record is inserted afterward (`src/documind/services/webhook_service.py:255-281`, `343-356`). Concurrent dispatchers can therefore issue duplicate HTTP requests for the same calculated attempt; the unique delivery constraint can only reject a later database insert, not the already-sent duplicate request.

### T9-15 — webhook management lacks authorization and required mutation safeguards

The registration, list, and deactivation routes check only that the request has an authenticated principal and then use ownership filters (`src/documind/api/webhooks.py:63-140`). Neither route nor service calls `AuthorizationService`, so any authenticated subject can create a delivery-capable public webhook. The create and deactivate mutations also accept no `Idempotency-Key`, and registration/deactivation audit writes are conditional on an audit service being present (`src/documind/api/webhooks.py:70-94`, `121-138`).

## Console shell

### T9-16 — a production console with missing OIDC authority fails open into an unauthenticated shell

When `VITE_OIDC_AUTHORITY` is empty, `App` renders every authenticated page without an `AuthProvider` or login gate (`console/src/App.tsx:92-116`). `VITE_DEV_TOKEN` is still compiled into the API-client fallback (`console/src/api.ts:222-227`; `console/src/hooks/useApi.ts:14-30`). This is not the required production OIDC configuration behavior.

### T9-17 — console document upload is nonfunctional for the API contract and has no transfer progress

The backend requires at least one `labels` form field (`src/documind/api/documents.py:43-63`). `UploadModal` has no label selector and calls `uploadDocument()` without labels (`console/src/components/UploadModal.tsx:21-40`); the client consequently submits no `labels` field (`console/src/api.ts:114-132`). A normal console upload receives request validation failure.

The same upload path uses `fetch` with no upload-progress mechanism (`console/src/api.ts:55-81`, `console/src/components/UploadModal.tsx:30-43`), so the required upload/progress behavior is missing.

### T9-18 — self-hosted font assets and the documented viewer limitation are absent

The console CSS refers to four local `/fonts/Inter-*.woff2` files (`console/src/index.css:4-31`), but the repository contains no `console/public/` font assets. These font URLs resolve to missing static files.

`DocumentViewer` presents metadata and version history only (`console/src/pages/DocumentViewer.tsx:1-228`) and contains no derivative-preview TODO. The Task 9 console implementation therefore neither provides a document preview nor records its acknowledged metadata-only limitation.

## Required-test coverage

### T9-19 — Task 9's required executable coverage is substantially absent

The focused Task 9 tests currently pass with mocks, but they do not exercise the required production flows:

- `tests/test_api/test_chat.py` covers disabled chat and a mocked no-RAG abstention, but does not cover a RAG-backed answer, session expiry, cursor pagination, erasure operation, feedback persistence/assistant-only behavior, memory budget, or successful/repeated compaction.
- `tests/test_services/test_chat_retention.py` covers an injected held-subject set and mocked cleanup only; it does not cover worker scheduling, authoritative legal holds, tombstone skip, or mandatory audit evidence.
- `tests/test_api/test_webhooks.py` mocks the service for registration and does not cover authorization, idempotency, active-list behavior, or delivery-log behavior.
- `tests/test_services/test_webhook_service.py` covers validation and signature helpers only; it does not execute an HTTP delivery, validate its headers and timeout, exercise DNS pinning, run a retry, test concurrency, or cover CloudEvent-to-webhook dispatch.

Consequently the tests do not expose the runtime integration failures listed above.
