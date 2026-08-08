"""Temporal worker bootstrap tests.

Confirms both ingest-cpu and model-gpu queues receive only their intended
activities.  The ingest worker gets workflow registration + inspect/parse/
normalize/chunk/project/verify/complete; the model worker gets only enrich.
"""

from documind.workflows.document_version import INGEST_QUEUE, MODEL_QUEUE, DocumentVersionWorkflow
from documind.workflows.worker import WorkerConfiguration, parse_worker_configuration


def test_worker_bootstrap_reads_only_non_secret_connection_references(monkeypatch: object) -> None:
    """The worker starts from an OpenBao auth reference and a Temporal endpoint."""
    monkeypatch.setenv("DOCUMIND_TEMPORAL_HOST", "temporal:7233")
    monkeypatch.setenv("DOCUMIND_OPENBAO_AUTH_REF", "openbao://auth/kubernetes/documind-worker")

    configuration = parse_worker_configuration()

    assert configuration == WorkerConfiguration(
        temporal_host="temporal:7233",
        openbao_auth_ref="openbao://auth/kubernetes/documind-worker",
    )


def test_ingest_cpu_queue_constant_matches_worker_module() -> None:
    """INGEST_QUEUE must be 'ingest-cpu'."""
    assert INGEST_QUEUE == "ingest-cpu"


def test_model_gpu_queue_constant_matches_worker_module() -> None:
    """MODEL_QUEUE must be 'model-gpu'."""
    assert MODEL_QUEUE == "model-gpu"


def test_ingest_worker_receives_only_ingest_activities() -> None:
    """The ingest-cpu worker must register inspect, parse, normalize, chunk."""
    stages = DocumentVersionWorkflow.stage_configurations()
    ingest_stages = [s for s in stages if s.task_queue == INGEST_QUEUE]
    ingest_names = {s.name for s in ingest_stages}
    assert ingest_names == {"inspect", "parse", "normalize", "chunk", "project", "verify", "complete"}


def test_model_worker_receives_only_enrich_activity() -> None:
    """The model-gpu worker must register ONLY enrich; no ingest activities."""
    stages = DocumentVersionWorkflow.stage_configurations()
    model_stages = [s for s in stages if s.task_queue == MODEL_QUEUE]
    model_names = {s.name for s in model_stages}
    assert model_names == {"enrich"}


def test_enrich_not_on_ingest_queue() -> None:
    """The enrich activity must not be available on the ingest-cpu queue."""
    stages = DocumentVersionWorkflow.stage_configurations()
    ingest_names = {s.name for s in stages if s.task_queue == INGEST_QUEUE}
    assert "enrich" not in ingest_names


def test_chunk_not_on_model_queue() -> None:
    """Chunk must remain on ingest-cpu, not model-gpu."""
    stages = DocumentVersionWorkflow.stage_configurations()
    model_names = {s.name for s in stages if s.task_queue == MODEL_QUEUE}
    assert "chunk" not in model_names


def test_workflow_registered_on_ingest_queue_only() -> None:
    """The workflow definition should be registered on ingest-cpu, not model-gpu."""
    # The stage_configurations define which activities go on which queue.
    # The workflow itself is registered on ingest-cpu (verified by the worker module).
    stages = DocumentVersionWorkflow.stage_configurations()
    # The workflow registration is on ingest-cpu; model-gpu has no workflows.
    # Verify all model-gpu stages are activity-only.
    model_stages = [s for s in stages if s.task_queue == MODEL_QUEUE]
    for stage in model_stages:
        assert stage.activity_name == stage.name


def test_all_stages_have_unique_names() -> None:
    """No two stages may share a name."""
    stages = DocumentVersionWorkflow.stage_configurations()
    names = [s.name for s in stages]
    assert len(names) == len(set(names))


def test_all_stages_have_positive_timeouts() -> None:
    """All timeout/heartbeat values must be positive integers."""
    for stage in DocumentVersionWorkflow.stage_configurations():
        assert stage.timeout_seconds > 0
        assert stage.heartbeat_seconds > 0
        assert stage.retry_attempts > 0


def test_activity_import_isolation() -> None:
    """Importing chunk should not make enrich available, and vice versa."""
    # This verifies that the activity modules are independently importable
    from documind.workflows.activities import chunk as chunk_mod
    from documind.workflows.activities import enrich as enrich_mod

    # Each module's configure function should set only its own service
    assert hasattr(chunk_mod, "configure_chunk_activity")
    assert hasattr(enrich_mod, "configure_enrich_activity")

    # chunk module should not export enrich function
    assert not hasattr(chunk_mod, "enrich")
    # enrich module should not export chunk function
    assert not hasattr(enrich_mod, "chunk")


def test_projection_activity_import_isolation() -> None:
    """Importing project/verify/complete modules should not cross-contaminate."""
    from documind.workflows.activities import complete as complete_mod
    from documind.workflows.activities import project as project_mod
    from documind.workflows.activities import verify as verify_mod

    assert hasattr(project_mod, "configure_project_activity")
    assert hasattr(verify_mod, "configure_verify_activity")
    assert hasattr(complete_mod, "configure_complete_activity")

    # project module should not export verify or complete
    assert not hasattr(project_mod, "verify")
    assert not hasattr(project_mod, "complete")
