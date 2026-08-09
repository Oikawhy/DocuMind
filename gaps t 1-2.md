# Gaps — Tasks 1–2

Audit date: 2026-08-09  
Scope: current working tree, checked only against Tasks 1–2 and their cited specification sections in `docs/plans/implement.md` and `docs/specs/2026-07-29-documind-self-hosted-architecture-design.md`. The working tree contains uncommitted changes; this report describes that current state.

This is a gap-only report. It contains no remediation plan or fix recommendation.

## Assessment by task and substep

| Task / substep | Status | Gaps found |
| --- | --- | --- |
| 1.1 Database engine, ENUMs, document/label models | Partial | Database interface/default, lifecycle evidence gates, current-version invariant |
| 1.2 Processing, outbox, policy, template, model-route | Partial | Policy-revision immutability and policy-body constraints |
| 1.3 Chunks, graph, projections, identity, chat, webhook, audit | Partial | Deterministic chunk IDs; identity, chat, webhook, audit-anchor schema contracts |
| 1.4 Alembic, indexes, tests | Partial | Three mandatory-index mismatches; migration safety/compatibility; required test coverage absent |
| 2.1 OpenBao secret service | Partial | No short-lived credential/lease behavior; SCIM bearer secret is stored in Settings rather than resolved through OpenBao |
| 2.2 Audit service | Partial | No critical-event anchor flow, no caller for `seal_anchor`, incomplete audit event taxonomy/immutability evidence |
| 2.3 OIDC validation | Partial | Token-type and nonce validation absent; authorization has no verified-identity state |
| 2.4 SCIM and identity projection | Partial | Required projection fields, reconciliation, delivery-failure evidence, group-delete/update behavior, and deactivation side effects absent |
| 2.5 Policy and label services | Partial | Activated policy records remain mutable; policy bodies are not constrained; duplicate/type-policy label validation is absent |
| 2.6 Deterministic authorization | Incomplete | Unknown resources and unlabeled resources can be allowed; version-scoped hold/tombstone checks are absent; non-document handlers do not call the service |
| 2.7 Middleware and Task 2 tests | Incomplete | Prefix-based authentication exclusions are overbroad; authorization tests fail against the current service API; required integration coverage is absent |

## Task 1 gaps

### T1-01 — Required database interface is not exported, and the production database URL fails open

Task 1 names `async_engine`, `AsyncSessionLocal`, and `get_db()` as its produced interface. `src/documind/database.py` exposes a private `_engine`, `get_engine()`, a lazy `AsyncSessionLocal` proxy, and `get_db()`, but no `async_engine` symbol (`database.py:16–18, 78–104`).

The same module documents a development-only fallback but returns the hard-coded `postgresql+asyncpg://documind:documind@localhost:5432/documind` URL whenever neither resolved environment value nor a concrete `database_url_ref` is supplied, without checking `Settings.debug` (`database.py:19–53`). This conflicts with the required secret/reference boundary and the no-default-credentials constraint.

### T1-02 — Lifecycle transition enforcement omits the required authorization/remediation evidence gates

The current lifecycle trigger permits `failed → processing` and `quarantined → processing` solely based on the old/new lifecycle values (`alembic/versions/004_projection_schema_alignment.py:180–186`). The specification requires an authorized replay before erasure for the former and recorded approved remediation for the latter. No replay authorization, approval record, or evidence reference is checked by the trigger.

### T1-03 — The completed-version pointer invariant is not implemented

`document.current_completed_version_id` is defined in the ORM and migration, but there is no assignment path anywhere under `src/`; its only runtime use is an authorization read (`src/documind/models/document.py:37–40`, `src/documind/domain/authorization_service.py:270–280`). Consequently, the required invariant that this pointer changes only after a non-erased version passes projection verification has no implementation or database enforcement.

### T1-04 — `document_chunk` IDs are random UUIDv4, not deterministic UUIDv5

Task 1 explicitly requires deterministic UUIDv5 chunk IDs. `DocumentChunk.id` defaults to `uuid.uuid4` (`src/documind/models/chunk.py:27`), and no UUIDv5 generation exists in the model or migration.

### T1-05 — Identity projection schema differs from the required physical schema and loses SCIM source-version data

`identity_subject.display_name` is non-null while the contract permits null, and both `scim_version` and `reconciled_at` are nullable despite being required (`src/documind/models/identity.py:16–23`; `001_initial_schema.py:547–557`).

`identity_group_membership` stores `group_key` and `assigned_at`; the required columns are `group_external_id` and non-null `source_version`. There is therefore no persisted SCIM resource/version provenance for a membership (`identity.py:26–36`; `001_initial_schema.py:559–565`).

### T1-06 — Chat persistence model does not match the Task 1 schema contract

`chat_session` lacks the required subject foreign key, title, summary, summary revision, persisted message count, deleted timestamp, and updated timestamp. `chat_message` permits `system` and `tool` roles rather than the required `user`/`assistant` enum and lacks citation IDs, confidence, trace ID, feedback, and feedback comment. `agent_run` uses a different checkpoint/message-oriented record and lacks request-ID uniqueness, subject, policy/model/prompt revisions, graph path, timing, safe failure code, response hash, and the specified creation fields (`src/documind/models/chat.py:13–70`; `alembic/versions/001_initial_schema.py:567–600`).

### T1-07 — Webhook and webhook-delivery persistence model does not meet the required contract

`webhook` persists `target_url`, `event_type_glob`, and `secret_hash` rather than the required `destination_url`, `allowed_origin`, event set, and OpenBao `secret_reference`. `webhook_delivery` has no unique delivery ID, no 1–3 attempt check, no `next_attempt_at`, no creation/delivery timestamps, and uses a different status/response schema (`src/documind/models/webhook.py:21–62`; `001_initial_schema.py:602–627`).

### T1-08 — Hash formats are length-limited but not constrained to lowercase SHA-256, and generated SQL uses varying strings rather than `char(64)`

The physical-schema contract requires lowercase hexadecimal SHA-256 values and `char(64)` hash columns. The models/migrations use `String(64)`/`VARCHAR(64)` without a lowercase-hex check for document content, policy, stage, outbox, tombstone, chunk, projection, webhook, or audit hashes (for example `models/document.py:74`, `models/audit.py:31–32`, `001_initial_schema.py:195–196, 359–361, 440, 653–664`). Arbitrary 64-character values are accepted; the model tests themselves use non-hex values such as `"g" * 64`.

### T1-09 — Graph fact migration and ORM disagree on required precision

The current ORM declares `graph_fact.confidence` as `Numeric(4, 3)` (`src/documind/models/graph.py:73`), matching the Task 1 DDL, but the initial migration creates `Numeric(5, 3)` (`alembic/versions/001_initial_schema.py:506`). No later migration changes this precision.

### T1-10 — Audit anchors are not WORM/immutable records

The only immutable-record trigger is for `deletion_tombstone` (`alembic/versions/002_workflow_durability_guards.py:67–87`). `audit_anchor` has only a uniqueness constraint and can be updated or deleted by the application database role (`models/audit.py:50–64`, `001_initial_schema.py:637–649`), contrary to the Task 1 WORM-sealed anchor requirement.

### T1-11 — Mandatory index definitions diverge from §8.2

Three current index definitions do not match their required predicates/columns:

- `document_active_idx` is created on `created_at` ascending, not `created_at DESC` (`001_initial_schema.py:687–694`).
- `graph_fact_source_idx` filters `tombstone_generation = 0`; §8.2 requires `tombstoned_at IS NULL`, and `tombstoned_at` exists after migration 003 (`001_initial_schema.py:727–732`; `003_task_5_enrichment_contract.py:42–43`).
- `webhook_delivery_due_idx` is on `attempted_at`; §8.2 requires pending deliveries indexed by `next_attempt_at`, a column absent from the current model/migration (`001_initial_schema.py:739–740`; `models/webhook.py:50–54`).

### T1-12 — Audit partition maintenance is startup-only

`ensure_audit_partitions()` can create partitions and report an alert, but it is only called once during FastAPI startup (`src/documind/main.py:65–80`). No scheduled execution exists. A long-running installation can pass below the two-future-partition threshold without further creation or alert evaluation (`src/documind/services/partition_service.py:29–94`).

### T1-13 — Migration safety/compatibility requirements are absent; migration 004 discards active projection pointers

The specified migration process requires release-manifest compatibility ranges and idempotent preflight. None of migrations 001–004 contains either. In addition, migration 004 unconditionally drops `active_projection_generation` before recreating it and does not copy existing pointers (`alembic/versions/004_projection_schema_alignment.py:125–158`). Existing active-generation data is lost when this migration is applied.

### T1-14 — Required Task 1 test coverage is incomplete

`tests/test_models/test_schema.py` checks table metadata, selected enum values, a few constraints, lifecycle reversal, and tombstone immutability. It contains no test for duplicate `(document_id, version_number)` or `(document_id, content_sha256)`, required indexes, audit partitions, audit-anchor immutability, deterministic chunk UUIDs, completed-version pointer gating, the failed/quarantined replay prerequisites, or the physical schemas for identity/chat/webhook records (`tests/test_models/test_schema.py:29–779`).

## Task 2 gaps

### T2-01 — Secret retrieval has no short-lived credential, lease, or renewal behavior; SCIM secret storage bypasses the service

`SecretService` accepts an already-provided token, performs a KV read, and returns a string. It has no OpenBao login/authentication method, lease metadata, lease renewal, revocation, or short-lived credential handling (`src/documind/services/secret_service.py:19–67`).

The SCIM bearer token is a direct `Settings.scim_bearer_token` value (`src/documind/config.py:111–112`) and is compared directly by the SCIM router (`src/documind/api/scim.py:41–54`), despite `Settings` stating that secret values are deliberately absent and the Task 2 contract requiring the OpenBao client.

### T2-02 — Audit service does not implement the critical-event anchor path or the complete required event metadata

`AuditEntry` contains no actor type or policy revision (`src/documind/services/audit_service.py:30–43`), both required in the audit taxonomy. The service accepts arbitrary `details` without a content/credential/prompt guard beyond a docstring.

`AuditService.seal_anchor()` only inserts a database record using a caller-provided signature and object key; it neither writes nor verifies a WORM object (`audit_service.py:179–211`). CodeGraph finds no call site for `seal_anchor`, so there is no critical-event synchronous anchor/durability-acknowledgement flow in the current application. There are also no direct AuditService tests; authorization tests replace it with a mock.

### T2-03 — OIDC validation omits required token-type and nonce checks

`IdentityService.validate_oidc_token()` validates signature, issuer, audience, expiry, not-before, and clock skew, but it does not validate a token type or nonce (`src/documind/services/identity_service.py:151–195`). The `Principal` value has no `verified_identity` field, and `AuthorizationService` checks only `principal.active` before proceeding (`identity_service.py:31–45`; `authorization_service.py:132–150`). This leaves the explicit `principal.active and verified_identity(principal)` authorization precondition unrepresented and unenforced.

### T2-04 — SCIM implementation is not an authoritative reconciled identity feed

The required `process_scim_event(event)` interface does not exist. Only independent create/update/deactivate helpers are present; no reconciliation method exists (`src/documind/services/identity_service.py:240–347`; CodeGraph has no `process_scim_event` or `reconcile` symbol).

Current behavior also omits these required SCIM outcomes:

- Create/update never store a SCIM resource version, and memberships never store their source version.
- Membership updates simply delete and replace local groups; they do not invalidate an authorization cache or retain the historical mapping revision (`identity_service.py:269–281, 308–320`).
- Deactivation only sets `active = False`; it does not revoke active API/session cache entries, cancel queued export operations, update reconciliation state, or emit an audit event (`identity_service.py:324–338`).
- No 15-minute snapshot reconciliation, delivered-event failure evidence, stale/conflicted-projection state, paginated reconciliation, or signed difference report exists.
- The SCIM router has no `/Groups` resource handling and `PATCH` recognizes only selected `replace` operations and `add groups`; it has no remove-group behavior or group-delete path (`api/scim.py:153–182`).

### T2-05 — Policy revision records are mutable and policy content is unconstrained

The policy service enforces selected status transitions in its own methods, but the database has no immutability trigger or update guard for activated policy revisions. The `PolicyRevision` model has normal mutable `body`, digest, approval, and status columns (`src/documind/models/policy.py:24–47`), and the only policy-related database triggers are unrelated lifecycle/tombstone triggers.

`PolicyService.create_revision()` accepts arbitrary JSON policy bodies and only hashes them (`src/documind/domain/policy_service.py:169–203`). It does not reject secret values, prompt text, raw endpoint credentials, or provider-response data, although the physical-schema policy contract prohibits all of them. Revision allocation reads `max(revision)` without a concurrency lock, so concurrent creates for one `(policy_kind, stable_key)` can race into a uniqueness failure rather than produce the next revision deterministically (`policy_service.py:183–203`).

### T2-06 — Label validation silently accepts duplicate requested labels and does not enforce declared-type policy intersection

`LabelService.validate_labels()` removes duplicates before validation instead of rejecting them (`src/documind/domain/label_service.py:50–51`). Its input is a precomputed `allowed_label_ids` set rather than a principal, and it does not resolve the declared type's allowed labels or policy revision. The required admission intersection of caller permissions, requested labels, and declared-type mapping is therefore absent from this Task 2 service (`label_service.py:28–79`).

### T2-07 — Authorization can allow an unknown resource and an unlabeled resource

When `_load_resource_descriptor()` cannot find a document, it returns `(None, None)` (`src/documind/domain/authorization_service.py:269–273`). `authorize()` then skips lifecycle and label checks; an active principal with a role permitting the action reaches `ALLOW` (`authorization_service.py:169–235`). This violates both canonical-resource authorization and the required 404-equivalent denial for unknown read targets.

For an existing document with no labels, `_load_resource_descriptor()` returns an empty list. The subset check treats the empty set as permitted, so an unlabeled resource is allowed rather than denied as a resource with missing labels (`authorization_service.py:286–293, 218–226`).

### T2-08 — Authorization ignores resource type and does not apply version-scoped holds or tombstones

`resource_type` is included in the method signature but never used to load a resource. Every lookup treats `resource_id` as a `Document` ID (`authorization_service.py:103–109, 260–293`). Version/chunk authorization cannot resolve its canonical document/version lifecycle and labels through this interface.

The tombstone query checks only `DeletionTombstone.document_id`, and the legal-hold query checks only `LegalHold.document_id` (`authorization_service.py:295–314`). A version-level tombstone or legal hold is not evaluated for a version resource, even though the schema and Task 2 contract support version scope.

### T2-09 — Required authorization boundaries are bypassed by current customer-facing handlers

CodeGraph finds only one production caller of `AuthorizationService.authorize`: `DocumentService._require_authorized()` (`src/documind/domain/document_service.py:900–915`). The reindex route only confirms a principal exists and returns an accepted stub (`src/documind/api/versions.py:28–49`). Webhook registration/listing/deactivation pass the authenticated principal directly to `WebhookService` without an authorization decision (`src/documind/api/webhooks.py:51–99`). Retrieval/comparison and chat similarly obtain a principal but do not call the deterministic authorization service at their API boundary (`api/retrieval.py:40–69`; `api/chat.py:208–383`).

This leaves the Task 2 requirement to authorize before webhook administration, projection/reindex work, retrieval, and agent/chat processing unmet in the present route graph.

### T2-10 — Middleware authentication exclusions use prefix matching

`_is_exempt()` accepts any path starting with `/health`, `/scim/v2`, `/docs`, or `/openapi.json` (`src/documind/api/middleware.py:23–29`). Paths such as `/health-private` or `/docs-private` would bypass OIDC if a handler is introduced under such a prefix. The exemption check is broader than the named endpoints/SCIM route tree.

### T2-11 — Required Task 2 tests are absent or no longer execute against the service contract

The specified Task 2 tests include real OIDC validation, SCIM deactivation cascades, missing-label denial, and fail-closed behavior. There is no direct test module for `IdentityService`, `AuditService`, `SecretService`, `PolicyService`, or `LabelService`. `test_identity.py` mocks `IdentityService.validate_oidc_token`; it does not exercise JWK/signature/issuer/audience/expiry handling. `test_scim.py` mocks the identity service and accepts a reload failure, so it does not verify a persisted SCIM projection or deactivation side effects.

The current authorization test suite is broken. Running:

```text
.venv/bin/pytest -xvv tests/test_domain/test_authorization.py
```

fails on its first test with:

```text
TypeError: AuthorizationService.authorize() got an unexpected keyword argument 'resource_labels'
```

The cause is visible in the current worktree: `AuthorizationService.authorize()` was changed to remove caller-provided labels/lifecycle and load canonical metadata (`src/documind/domain/authorization_service.py:103–170`), while all affected tests still pass `resource_labels` and `resource_lifecycle` (`tests/test_domain/test_authorization.py:123–449`). As a result, the required authorization assertions do not run.

## Evidence summary

- Source and migration review used the CodeGraph index before direct source inspection.
- Current working-tree status includes uncommitted implementation changes; no source changes were made for this audit.
- The targeted authorization test failure above was reproduced directly. The Task 1 model-test command did not return a conclusive result in this audit session, so this report does not claim a Task 1 test pass/fail outcome.
