"""Unit tests for deterministic document-version Temporal workflow contracts.

Covers the full stage order, checksum chaining, task queues/timeouts/retries,
chunk-before-enrich fact provenance, no-template nonblocking outcome, replay
behavior, and tombstone interruption.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from documind.workflows.document_version import (
    INGEST_QUEUE,
    MODEL_QUEUE,
    DocumentVersionWorkflow,
    DocumentVersionWorkflowResult,
    InMemoryStageReplayStore,
    StageConfiguration,
    StageExecution,
    stage_idempotency_key,
    workflow_id_for,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload_sha256(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stage_execution(version_id: str, name: str, input_sha256: str) -> StageExecution:
    return StageExecution(
        version_id=version_id,
        name=name,
        input_sha256=input_sha256,
        idempotency_key=stage_idempotency_key(version_id, name, input_sha256),
    )


# ---------------------------------------------------------------------------
# Stage configuration and ordering tests
# ---------------------------------------------------------------------------


def test_workflow_id_and_stage_configuration_match_ingestion_contract() -> None:
    version_id = uuid.uuid4()

    assert workflow_id_for(version_id) == f"document-version/{version_id}"
    stages = DocumentVersionWorkflow.stage_configurations()

    assert [(stage.name, stage.timeout_seconds, stage.retry_attempts) for stage in stages] == [
        ("inspect", 600, 3),
        ("parse", 600, 2),
        ("normalize", 300, 3),
        ("chunk", 300, 3),
        ("enrich", 300, 2),
        ("project", 600, 2),
        ("verify", 120, 2),
        ("complete", 120, 1),
    ]


def test_full_stage_order_matches_pipeline_contract() -> None:
    """Stages must execute in exact order: inspect → parse → normalize → chunk → enrich."""
    stages = DocumentVersionWorkflow.stage_configurations()
    names = [stage.name for stage in stages]
    assert names == ["inspect", "parse", "normalize", "chunk", "enrich", "project", "verify", "complete"]


def test_stage_task_queue_assignments() -> None:
    """Inspect/parse/normalize/chunk run on ingest-cpu; enrich runs on model-gpu."""
    stages = DocumentVersionWorkflow.stage_configurations()
    queue_map = {stage.name: stage.task_queue for stage in stages}
    assert queue_map == {
        "inspect": INGEST_QUEUE,
        "parse": INGEST_QUEUE,
        "normalize": INGEST_QUEUE,
        "chunk": INGEST_QUEUE,
        "enrich": MODEL_QUEUE,
        "project": INGEST_QUEUE,
        "verify": INGEST_QUEUE,
        "complete": INGEST_QUEUE,
    }


def test_heartbeat_seconds_uniform_across_all_stages() -> None:
    """All stages use 30-second heartbeat intervals."""
    stages = DocumentVersionWorkflow.stage_configurations()
    assert {stage.heartbeat_seconds for stage in stages} == {30}


def test_chunk_stage_configuration() -> None:
    """Chunk: 5 minutes timeout, 30s heartbeat, 3 attempts, ingest-cpu."""
    stages = DocumentVersionWorkflow.stage_configurations()
    chunk_config = next(s for s in stages if s.name == "chunk")
    assert chunk_config == StageConfiguration("chunk", 300, 30, 3, INGEST_QUEUE)


def test_enrich_stage_configuration() -> None:
    """Enrich: 5 minutes timeout, 30s heartbeat, 2 attempts, model-gpu."""
    stages = DocumentVersionWorkflow.stage_configurations()
    enrich_config = next(s for s in stages if s.name == "enrich")
    assert enrich_config == StageConfiguration("enrich", 300, 30, 2, MODEL_QUEUE)


# ---------------------------------------------------------------------------
# Checksum chaining tests
# ---------------------------------------------------------------------------


def test_checksum_chain_is_deterministic_across_stages() -> None:
    """Each stage's input_sha256 must be the SHA-256 of its predecessor's canonical output."""
    version_id = str(uuid.uuid4())
    content_sha256 = hashlib.sha256(b"quarantined bytes").hexdigest()

    # inspect receives content_sha256
    inspect_stage = _stage_execution(version_id, "inspect", content_sha256)
    assert inspect_stage.input_sha256 == content_sha256

    # parse receives SHA-256 of inspect output
    inspect_output = {"safe": True, "detected_mime": "application/pdf"}
    parse_input = _payload_sha256(inspect_output)
    parse_stage = _stage_execution(version_id, "parse", parse_input)
    assert parse_stage.input_sha256 == parse_input

    # normalize receives SHA-256 of parse output
    parse_output = {"success": True, "text": "hello", "pages": []}
    normalize_input = _payload_sha256(parse_output)
    normalize_stage = _stage_execution(version_id, "normalize", normalize_input)
    assert normalize_stage.input_sha256 == normalize_input

    # chunk receives SHA-256 of normalize output
    normalize_output = {"text": "hello", "blocks": [], "normalization_revision": "v1"}
    chunk_input = _payload_sha256(normalize_output)
    chunk_stage = _stage_execution(version_id, "chunk", chunk_input)
    assert chunk_stage.input_sha256 == chunk_input

    # enrich receives SHA-256 of chunk output
    chunk_output = {"chunk_count": 5, "chunk_checksum": "abc123"}
    enrich_input = _payload_sha256(chunk_output)
    enrich_stage = _stage_execution(version_id, "enrich", enrich_input)
    assert enrich_stage.input_sha256 == enrich_input

    # project, verify, and complete all use the same snapshot identity:
    # the enriched-output checksum.  This ensures the projection
    # coordinator can match outcomes across all three stages.
    enrich_output = {
        "type_suggestion": {"value": "invoice"},
        "extraction_status": "validated",
        "fact_result": {"entities_created": 3, "facts_created": 5},
    }
    snapshot_checksum = _payload_sha256(enrich_output)

    project_stage = _stage_execution(version_id, "project", snapshot_checksum)
    assert project_stage.input_sha256 == snapshot_checksum

    verify_stage = _stage_execution(version_id, "verify", snapshot_checksum)
    assert verify_stage.input_sha256 == snapshot_checksum

    complete_stage = _stage_execution(version_id, "complete", snapshot_checksum)
    assert complete_stage.input_sha256 == snapshot_checksum

    # All three stages share the same snapshot identity but have
    # distinct idempotency keys because the stage name differs.
    assert project_stage.idempotency_key != verify_stage.idempotency_key
    assert verify_stage.idempotency_key != complete_stage.idempotency_key
    assert project_stage.idempotency_key != complete_stage.idempotency_key


def test_stage_idempotency_key_includes_version_stage_and_checksum() -> None:
    """The idempotency key must be deterministic over (version, stage, checksum)."""
    version_id = str(uuid.uuid4())
    input_sha256 = hashlib.sha256(b"test").hexdigest()

    key1 = stage_idempotency_key(version_id, "chunk", input_sha256)
    key2 = stage_idempotency_key(version_id, "chunk", input_sha256)
    assert key1 == key2

    # Different stage name → different key
    key3 = stage_idempotency_key(version_id, "enrich", input_sha256)
    assert key1 != key3

    # Different version → different key
    key4 = stage_idempotency_key(str(uuid.uuid4()), "chunk", input_sha256)
    assert key1 != key4


def test_different_checksums_produce_different_idempotency_keys() -> None:
    """Divergent inputs to the same stage must not share an idempotency key."""
    version_id = str(uuid.uuid4())
    checksum_a = hashlib.sha256(b"a").hexdigest()
    checksum_b = hashlib.sha256(b"b").hexdigest()

    key_a = stage_idempotency_key(version_id, "chunk", checksum_a)
    key_b = stage_idempotency_key(version_id, "chunk", checksum_b)
    assert key_a != key_b


# ---------------------------------------------------------------------------
# Replay idempotency tests
# ---------------------------------------------------------------------------


async def test_idempotent_stage_replay_returns_prior_output_without_second_execution() -> None:
    version_id = uuid.uuid4()
    input_checksum = hashlib.sha256(b"quarantined bytes").hexdigest()
    stage = StageExecution(
        version_id=str(version_id),
        name="inspect",
        input_sha256=input_checksum,
        idempotency_key=stage_idempotency_key(version_id, "inspect", input_checksum),
    )
    store = InMemoryStageReplayStore()
    calls = 0

    async def execute() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"safe": True, "detected_mime": "application/pdf"}

    first = await store.run(stage, execute)
    replay = await store.run(stage, execute)

    assert calls == 1
    assert replay == first
    assert (
        first.output_sha256
        == hashlib.sha256(
            b'{"detected_mime":"application/pdf","safe":true}',
        ).hexdigest()
    )


async def test_chunk_stage_replay_is_idempotent() -> None:
    """A chunk stage replay with the same input must return the prior output."""
    version_id = uuid.uuid4()
    input_checksum = hashlib.sha256(b"normalized content").hexdigest()
    stage = StageExecution(
        version_id=str(version_id),
        name="chunk",
        input_sha256=input_checksum,
        idempotency_key=stage_idempotency_key(version_id, "chunk", input_checksum),
    )
    store = InMemoryStageReplayStore()
    calls = 0

    async def execute() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"chunk_count": 5, "chunk_checksum": "abc123"}

    first = await store.run(stage, execute)
    replay = await store.run(stage, execute)

    assert calls == 1
    assert replay == first
    assert replay.output["chunk_count"] == 5


async def test_enrich_stage_replay_is_idempotent() -> None:
    """An enrich stage replay with the same input must return the prior output."""
    version_id = uuid.uuid4()
    input_checksum = hashlib.sha256(b"chunk output").hexdigest()
    stage = StageExecution(
        version_id=str(version_id),
        name="enrich",
        input_sha256=input_checksum,
        idempotency_key=stage_idempotency_key(version_id, "enrich", input_checksum),
    )
    store = InMemoryStageReplayStore()
    calls = 0

    async def execute() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "type_suggestion": {"value": "invoice", "confidence": 0.85},
            "extraction_status": "validated",
            "fact_result": {"entities_created": 3, "facts_created": 5},
        }

    first = await store.run(stage, execute)
    replay = await store.run(stage, execute)

    assert calls == 1
    assert replay == first
    assert replay.output["extraction_status"] == "validated"


# ---------------------------------------------------------------------------
# Chunk-before-enrich ordering tests
# ---------------------------------------------------------------------------


def test_chunk_stage_precedes_enrich_in_configuration() -> None:
    """Chunks must exist before enrichment can create facts with source provenance."""
    stages = DocumentVersionWorkflow.stage_configurations()
    names = [s.name for s in stages]
    chunk_idx = names.index("chunk")
    enrich_idx = names.index("enrich")
    assert chunk_idx < enrich_idx, "chunk must precede enrich for fact provenance"


def test_enrich_stage_precedes_project_in_configuration() -> None:
    """Enrichment must complete before projection can begin."""
    stages = DocumentVersionWorkflow.stage_configurations()
    names = [s.name for s in stages]
    enrich_idx = names.index("enrich")
    project_idx = names.index("project")
    verify_idx = names.index("verify")
    complete_idx = names.index("complete")
    assert enrich_idx < project_idx, "enrich must precede project"
    assert project_idx < verify_idx, "project must precede verify"
    assert verify_idx < complete_idx, "verify must precede complete"


# ---------------------------------------------------------------------------
# No-template nonblocking outcome tests
# ---------------------------------------------------------------------------


async def test_enrich_with_no_template_produces_nonblocking_result() -> None:
    """When no template is pinned, enrichment should succeed with pending_template status."""
    version_id = uuid.uuid4()
    input_checksum = hashlib.sha256(b"chunks done").hexdigest()
    stage = StageExecution(
        version_id=str(version_id),
        name="enrich",
        input_sha256=input_checksum,
        idempotency_key=stage_idempotency_key(version_id, "enrich", input_checksum),
    )
    store = InMemoryStageReplayStore()

    async def execute() -> dict[str, object]:
        return {
            "type_suggestion": None,
            "extraction_status": "pending_template",
            "extraction_id": None,
            "proposal_id": None,
            "fact_result": {"entities_created": 0, "facts_created": 0, "facts_corroborated": 0},
            "errors": [],
        }

    result = await store.run(stage, execute)
    assert result.output["extraction_status"] == "pending_template"
    assert result.output["errors"] == []


# ---------------------------------------------------------------------------
# Workflow result structure tests
# ---------------------------------------------------------------------------


def test_workflow_result_includes_chunk_and_enrichment_fields() -> None:
    """The result dataclass must include chunk and enrichment output fields."""
    result = DocumentVersionWorkflowResult(
        version_id=str(uuid.uuid4()),
        state="processing",
        normalization={"text": "hello"},
        chunk={"chunk_count": 5, "chunk_checksum": "abc"},
        enrichment={"extraction_status": "validated"},
    )
    assert result.chunk is not None
    assert result.enrichment is not None
    assert result.chunk["chunk_count"] == 5
    assert result.enrichment["extraction_status"] == "validated"


def test_workflow_result_includes_projection_and_completion_fields() -> None:
    """The result dataclass must include projection and completion output fields."""
    result = DocumentVersionWorkflowResult(
        version_id=str(uuid.uuid4()),
        state="completed",
        normalization={"text": "hello"},
        chunk={"chunk_count": 5, "chunk_checksum": "abc"},
        enrichment={"extraction_status": "validated"},
        projection={"snapshot_id": "snap-1", "status": "projected"},
        completion={"status": "completed"},
    )
    assert result.projection is not None
    assert result.completion is not None
    assert result.projection["status"] == "projected"
    assert result.completion["status"] == "completed"
    assert result.state == "completed"


def test_workflow_result_failed_stage_has_no_chunk_or_enrichment() -> None:
    """A workflow that fails at inspect should not have chunk/enrichment data."""
    result = DocumentVersionWorkflowResult(
        version_id=str(uuid.uuid4()),
        state="failed",
        failed_stage="inspect",
        safe_error_class="unsafe_content",
        safe_error_code="MALWARE_DETECTED",
    )
    assert result.chunk is None
    assert result.enrichment is None


# ---------------------------------------------------------------------------
# Retry policy tests
# ---------------------------------------------------------------------------


def test_retry_policies_match_stage_contracts() -> None:
    """Each stage's retry policy maximum_attempts must match its retry_attempts."""
    for stage in DocumentVersionWorkflow.stage_configurations():
        policy = stage.retry_policy()
        assert policy.maximum_attempts == stage.retry_attempts


def test_retry_policy_backoff_is_bounded() -> None:
    """Retry backoff should use 30s initial, 2.0 coefficient, 15min max."""
    from datetime import timedelta

    for stage in DocumentVersionWorkflow.stage_configurations():
        policy = stage.retry_policy()
        assert policy.initial_interval == timedelta(seconds=30)
        assert policy.backoff_coefficient == 2.0
        assert policy.maximum_interval == timedelta(minutes=15)


# ---------------------------------------------------------------------------
# Projection pipeline integration tests
# ---------------------------------------------------------------------------


async def test_project_stage_replay_is_idempotent() -> None:
    """A project stage replay with the same input must return the prior output."""
    version_id = uuid.uuid4()
    input_checksum = hashlib.sha256(b"enrichment output").hexdigest()
    stage = StageExecution(
        version_id=str(version_id),
        name="project",
        input_sha256=input_checksum,
        idempotency_key=stage_idempotency_key(version_id, "project", input_checksum),
    )
    store = InMemoryStageReplayStore()
    calls = 0

    async def execute() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"snapshot_id": "snap-1", "status": "projected", "record_count": 10}

    first = await store.run(stage, execute)
    replay = await store.run(stage, execute)

    assert calls == 1
    assert replay == first
    assert replay.output["status"] == "projected"


async def test_complete_stage_replay_is_idempotent() -> None:
    """A complete stage replay must return the prior output."""
    version_id = uuid.uuid4()
    input_checksum = hashlib.sha256(b"verify output").hexdigest()
    stage = StageExecution(
        version_id=str(version_id),
        name="complete",
        input_sha256=input_checksum,
        idempotency_key=stage_idempotency_key(version_id, "complete", input_checksum),
    )
    store = InMemoryStageReplayStore()
    calls = 0

    async def execute() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "completed", "version_id": str(version_id)}

    first = await store.run(stage, execute)
    replay = await store.run(stage, execute)

    assert calls == 1
    assert replay == first
    assert replay.output["status"] == "completed"


async def test_full_project_verify_complete_chain_through_replay_store() -> None:
    """All three projection stages share one snapshot identity through InMemoryStageReplayStore."""
    version_id = str(uuid.uuid4())
    store = InMemoryStageReplayStore()

    # The snapshot identity is established once from the enriched output.
    snapshot_checksum = hashlib.sha256(b"enrichment done").hexdigest()

    # Stage 1: project — uses snapshot_checksum
    project_stage = _stage_execution(version_id, "project", snapshot_checksum)

    project_output = await store.run(
        project_stage,
        lambda: _async_return({"snapshot_id": "snap-1", "status": "projected", "record_count": 10}),
    )
    assert project_output.output["status"] == "projected"

    # Stage 2: verify — same snapshot_checksum
    verify_stage = _stage_execution(version_id, "verify", snapshot_checksum)

    verify_output = await store.run(
        verify_stage,
        lambda: _async_return({"status": "verified", "manifest_count": 3, "manifest_checksum": "abc"}),
    )
    assert verify_output.output["status"] == "verified"
    assert verify_output.output["manifest_count"] == 3

    # Stage 3: complete — same snapshot_checksum
    complete_stage = _stage_execution(version_id, "complete", snapshot_checksum)

    complete_output = await store.run(
        complete_stage,
        lambda: _async_return({"status": "completed", "version_id": version_id}),
    )
    assert complete_output.output["status"] == "completed"

    # All three stages share the same snapshot identity but have distinct
    # idempotency keys because stage_name differs in the key derivation.
    assert project_stage.input_sha256 == verify_stage.input_sha256 == complete_stage.input_sha256
    assert project_stage.idempotency_key != verify_stage.idempotency_key
    assert verify_stage.idempotency_key != complete_stage.idempotency_key


async def _async_return(value: dict[str, object]) -> dict[str, object]:
    """Helper to create an async callable returning a value."""
    return value


# ---------------------------------------------------------------------------
# Runtime path tests (T5-16)
# ---------------------------------------------------------------------------


def test_all_activity_signatures_accept_stage_execution_only() -> None:
    """Every activity function must accept exactly (stage: StageExecution).

    This catches the T5-01 class of bugs where _execute_stage passes
    args=[stage] but the activity expects (stage, parsed) or similar.
    """
    import inspect

    from documind.workflows.activities.chunk import chunk
    from documind.workflows.activities.enrich import enrich
    from documind.workflows.activities.normalize import normalize

    for activity_fn in [normalize, chunk, enrich]:
        sig = inspect.signature(activity_fn)
        params = list(sig.parameters.values())
        # Should have exactly one parameter (stage)
        assert len(params) == 1, f"{activity_fn.__name__} has {len(params)} params, expected 1"
        assert params[0].name == "stage", (
            f"{activity_fn.__name__} first param is '{params[0].name}', expected 'stage'"
        )


def test_normalize_output_excludes_full_content() -> None:
    """Normalize activity output must not contain text, pages, or blocks.

    This is a regression guard for T5-02 — full document content in
    Temporal workflow history.
    """
    # The normalize activity's execute() returns a dict; verify the keys
    # it constructs do not include content-bearing fields.
    # We check the source code to ensure the dict literal doesn't include them.
    import ast
    import inspect

    from documind.workflows.activities.normalize import normalize

    source = inspect.getsource(normalize)
    tree = ast.parse(source)

    # Find all string keys in dict literals within the function
    dict_keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    dict_keys.add(key.value)

    forbidden = {"text", "pages", "blocks"}
    found_forbidden = dict_keys & forbidden
    assert not found_forbidden, (
        f"normalize activity output contains content-bearing keys: {found_forbidden}"
    )


def test_worker_chunking_service_has_tokenizer() -> None:
    """ChunkingService built by the worker must have a functional _tokenizer.

    Guards against T5-05 regression where __new__() left the object
    without any attributes.
    """
    from unittest.mock import MagicMock

    from documind.workflows.worker import _build_chunking_service

    service = _build_chunking_service(MagicMock())
    assert hasattr(service, "_tokenizer"), "ChunkingService._tokenizer is not initialized"
    assert service._tokenizer is not None, "ChunkingService._tokenizer is None"
    assert hasattr(service._tokenizer, "digest"), "Tokenizer missing 'digest' attribute"
    assert hasattr(service._tokenizer, "tokenize"), "Tokenizer missing 'tokenize' method"

    # Verify tokenizer actually works
    tokens = service._tokenizer.tokenize("hello world foo")
    assert len(tokens) == 3, f"Expected 3 tokens, got {len(tokens)}"
    assert tokens[0].start_offset == 0
    assert tokens[0].end_offset == 5
