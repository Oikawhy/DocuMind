"""AgentState TypedDict and supporting types per §7.3.

AgentState is created fresh for each chat or retrieval request.  It contains
opaque identifiers and bounded metadata, not direct object-store handles or
database connections.

Large authorized excerpts are stored in the ``EvidenceCache`` — an ephemeral
encrypted in-process cache keyed by evidence ID.  The cache expires at graph
completion and never crosses sessions.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from cryptography.fernet import Fernet

# ---------------------------------------------------------------------------
# State invariant constants (§7.3 table)
# ---------------------------------------------------------------------------

MAX_REWRITTEN_QUERIES: int = 3
MAX_QUERY_CHARS: int = 512
MAX_PLAN_STEPS: int = 5
MAX_RETRIEVAL_ATTEMPTS: int = 3
MAX_TARGETED_EXPANSIONS: int = 2
MAX_EVIDENCE_CHUNKS: int = 10
MAX_GENERATION_REVISIONS: int = 2
RUNTIME_BUDGET_SECONDS: int = 60


# ---------------------------------------------------------------------------
# Supporting value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanStep:
    """A single declarative sub-task from the Planner (§7.4).

    Each step has a whitelisted operation type and optional normalized
    entity/date/value filters.  No backend syntax is permitted.
    """

    operation: str  # whitelisted operation type
    description: str
    document_selector: str | None = None
    version_selector: str | None = None
    entity_filter: str | None = None
    date_filter: str | None = None
    value_filter: str | None = None


@dataclass(frozen=True)
class QueryHints:
    """Structured hints produced by the Query Rewriter (§7.4)."""

    entities: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    amounts: list[str] = field(default_factory=list)
    locale: str = "en"


@dataclass(frozen=True)
class RelevanceGrade:
    """Per-evidence relevance assessment from the Relevance Grader (§7.4)."""

    evidence_id: str
    grade: Literal["relevant", "partially_relevant", "irrelevant", "needs_more_context"]
    reason: str = ""


@dataclass(frozen=True)
class VersionRef:
    """Resolved canonical version reference (§7.4)."""

    document_id: str
    version_id: str
    version_number: int
    selector_used: str
    status: Literal["resolved", "missing", "inaccessible", "failed", "erased"] = "resolved"


@dataclass(frozen=True)
class StructuredExtraction:
    """Schema-validated extraction result (§7.4)."""

    template_id: str
    template_revision: int
    fields: dict[str, Any] = field(default_factory=dict)
    source_spans: dict[str, list[str]] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    valid: bool = True
    pending_template: bool = False


@dataclass(frozen=True)
class ComparisonResult:
    """Deterministic diff result from Comparative Analysis (§7.4)."""

    left_version_id: str
    right_version_id: str
    text_diff: dict[str, Any] = field(default_factory=dict)
    structured_diff: dict[str, Any] = field(default_factory=dict)
    timeline_diff: list[dict[str, Any]] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)


@dataclass(frozen=True)
class AggregationResult:
    """Deterministic aggregation result (§7.4)."""

    operation: Literal["sum", "avg", "min", "max", "count", "group_by"]
    field_name: str
    result: float | int | dict[str, float | int]
    unit: str | None = None
    input_values: list[dict[str, Any]] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    calculation_trace: str = ""


@dataclass(frozen=True)
class Claim:
    """A single claim in a draft answer with evidence references (§7.4)."""

    claim_id: str
    text: str
    evidence_ids: list[str] = field(default_factory=list)
    grounded: bool = True


@dataclass(frozen=True)
class Citation:
    """Verified citation linking a claim to canonical evidence (§7.4)."""

    citation_id: str
    claim_id: str
    document_id: str
    version_id: str
    version_number: int
    chunk_id: str
    page_start: int | None = None
    page_end: int | None = None
    section_path: list[str] = field(default_factory=list)
    excerpt: str = ""
    content_sha256: str = ""
    valid: bool = True
    invalidation_reason: str | None = None


@dataclass(frozen=True)
class DraftAnswer:
    """Generator output: answer text with explicit claim list (§7.4)."""

    text: str
    claims: list[Claim] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HallucinationIssue:
    """A single hallucination issue found by the Grader."""

    claim_id: str
    grade: Literal["unsupported", "partial", "grounded"]
    reason: str = ""
    suggested_action: Literal["remove", "qualify", "rewrite", "keep"] = "keep"


@dataclass(frozen=True)
class HallucinationGrade:
    """Structured hallucination grading result (§7.4)."""

    issues: list[HallucinationIssue] = field(default_factory=list)
    needs_revision: bool = False
    all_grounded: bool = True


@dataclass(frozen=True)
class CitationVerification:
    """Deterministic citation verification result (§7.4)."""

    all_valid: bool = True
    verified_citations: list[Citation] = field(default_factory=list)
    invalid_citations: list[Citation] = field(default_factory=list)
    failure_code: str | None = None


# ---------------------------------------------------------------------------
# AgentState — the full graph state per §7.3
# ---------------------------------------------------------------------------


class AgentState(TypedDict, total=False):
    """Full typed state for the LangGraph RAG agent per §7.3.

    Created fresh for each chat or retrieval request.  Contains only opaque
    identifiers and bounded metadata — no database connections or
    object-store handles.
    """

    # --- Request / trace / principal identifiers ---
    request_id: str
    trace_id: str
    principal_subject: str
    authorization_revision: int
    retrieval_policy_revision: int
    model_route_revisions: dict[str, int]
    locale: str
    original_question: str

    # --- Session information ---
    session_id: str | None
    session_summary: str | None
    chat_history: list[dict[str, str]]  # role/content pairs

    # --- Route / confidence / plan / queries ---
    route_type: Literal[
        "simple_qa",
        "comparison",
        "aggregation",
        "extraction",
        "summarization",
        "clarification",
        "out_of_scope",
    ]
    route_confidence: float
    plan: list[PlanStep]
    rewritten_queries: list[str]
    query_hints: QueryHints

    # --- Retrieval / expansion counters ---
    retrieval_attempts: int
    targeted_expansions: int
    candidate_ids: list[str]
    filtered_candidate_ids: list[str]
    filtered_out_count: int
    reranked_evidence_ids: list[str]
    relevance_grades: list[RelevanceGrade]
    degraded_branches: list[str]

    # --- Version resolution / analysis ---
    resolved_versions: list[VersionRef]
    extraction_results: list[StructuredExtraction]
    comparison_result: ComparisonResult | None
    aggregation_result: AggregationResult | None

    # --- Generation / grading / verification ---
    draft_answer: DraftAnswer | None
    hallucination_grade: HallucinationGrade | None
    citation_verification: CitationVerification | None
    generation_revisions: int
    confidence: Literal["high", "medium", "low"]
    abstention_reason: str | None
    final_response: dict[str, Any] | None
    agent_path: list[str]

    # --- Graph-scoped relevance loop request kind ---
    relevance_request_kind: Literal["answer", "rewrite", "targeted_expansion", "abstain"] | None

    # --- Runtime metadata ---
    start_time: float  # monotonic clock start for budget enforcement


def create_initial_state(
    *,
    question: str,
    principal_subject: str,
    trace_id: str | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    session_summary: str | None = None,
    chat_history: list[dict[str, str]] | None = None,
    locale: str = "en",
    authorization_revision: int = 0,
    retrieval_policy_revision: int = 0,
    model_route_revisions: dict[str, int] | None = None,
) -> AgentState:
    """Construct a fresh AgentState for a new request."""
    import time

    return AgentState(
        request_id=request_id or str(uuid.uuid4()),
        trace_id=trace_id or str(uuid.uuid4()),
        principal_subject=principal_subject,
        authorization_revision=authorization_revision,
        retrieval_policy_revision=retrieval_policy_revision,
        model_route_revisions=model_route_revisions or {},
        locale=locale,
        original_question=question,
        session_id=session_id,
        session_summary=session_summary,
        chat_history=chat_history or [],
        route_type="simple_qa",
        route_confidence=0.0,
        plan=[],
        rewritten_queries=[],
        query_hints=QueryHints(),
        retrieval_attempts=0,
        targeted_expansions=0,
        candidate_ids=[],
        filtered_candidate_ids=[],
        filtered_out_count=0,
        reranked_evidence_ids=[],
        relevance_grades=[],
        degraded_branches=[],
        resolved_versions=[],
        extraction_results=[],
        comparison_result=None,
        aggregation_result=None,
        draft_answer=None,
        hallucination_grade=None,
        citation_verification=None,
        generation_revisions=0,
        confidence="low",
        abstention_reason=None,
        final_response=None,
        agent_path=[],
        relevance_request_kind=None,
        start_time=time.monotonic(),
    )


# ---------------------------------------------------------------------------
# Ephemeral encrypted in-process evidence cache (§7.3)
# ---------------------------------------------------------------------------


class EvidenceCache:
    """In-process cache for authorized evidence excerpts.

    Keyed by evidence ID.  Values are Fernet-encrypted with a per-request
    symmetric key that is never persisted.  The cache expires at graph
    completion and never crosses sessions.
    """

    def __init__(self) -> None:
        self._key = Fernet.generate_key()
        self._fernet = Fernet(self._key)
        self._store: dict[str, bytes] = {}
        self._expired = False

    def put(self, evidence_id: str, content: str) -> None:
        """Store an evidence excerpt under its ID."""
        if self._expired:
            raise RuntimeError("EvidenceCache has expired; cannot store new evidence.")
        self._store[evidence_id] = self._fernet.encrypt(content.encode("utf-8"))

    def get(self, evidence_id: str) -> str | None:
        """Retrieve a decrypted evidence excerpt, or ``None`` if missing."""
        if self._expired:
            raise RuntimeError("EvidenceCache has expired; cannot retrieve evidence.")
        encrypted = self._store.get(evidence_id)
        if encrypted is None:
            return None
        return self._fernet.decrypt(encrypted).decode("utf-8")

    def contains(self, evidence_id: str) -> bool:
        """Check whether an evidence ID is in the cache."""
        return evidence_id in self._store and not self._expired

    def keys(self) -> list[str]:
        """Return all cached evidence IDs."""
        if self._expired:
            return []
        return list(self._store.keys())

    def expire_all(self) -> None:
        """Wipe all cached evidence and destroy the encryption key.

        Called at graph completion.  After this, no evidence can be
        retrieved or stored.
        """
        self._store.clear()
        # Overwrite key material before discarding.
        self._key = os.urandom(len(self._key))
        self._expired = True

    @property
    def is_expired(self) -> bool:
        return self._expired

    def __len__(self) -> int:
        if self._expired:
            return 0
        return len(self._store)
