"""Deterministic confidence calculation per §7.5.

- **High**: ≥2 valid citations, no degraded branch, no retry, no revision,
  all claims verified.
- **Medium**: verified claims + permitted retry/revision/degraded/limited evidence.
- **Low**: explicit bounded partial answer with verified claims + stated limitation.
- **Abstention**: no valid evidence, invalid citation, mandatory route unavailable,
  auth change, ambiguity, loop limit.
"""

from __future__ import annotations

from typing import Literal

from documind.rag.state import AgentState


def calculate_confidence(state: AgentState) -> Literal["high", "medium", "low"]:
    """Calculate deterministic confidence level per §7.5.

    Does NOT return "abstention" — that is handled by setting
    ``abstention_reason`` in the state.
    """
    # Abstention conditions (caller sets abstention_reason separately).
    if state.get("abstention_reason"):
        return "low"

    citation_verification = state.get("citation_verification")
    hallucination_grade = state.get("hallucination_grade")
    draft = state.get("draft_answer")

    # No draft answer → low.
    if draft is None:
        return "low"

    # No citation verification → low.
    if citation_verification is None:
        return "low"

    # Invalid citations → low.
    if not citation_verification.all_valid:
        return "low"

    verified_count = len(citation_verification.verified_citations)
    has_degraded = len(state.get("degraded_branches", [])) > 0
    retry_count = state.get("retrieval_attempts", 0)
    revision_count = state.get("generation_revisions", 0)
    all_grounded = (
        hallucination_grade.all_grounded
        if hallucination_grade is not None
        else False
    )

    # High: ≥2 valid citations, no degraded, no retry, no revision, all grounded.
    if (
        verified_count >= 2
        and not has_degraded
        and retry_count <= 1
        and revision_count == 0
        and all_grounded
    ):
        return "high"

    # Medium: verified claims + some permitted issues.
    if verified_count >= 1 and all_grounded:
        return "medium"

    # Low: everything else.
    return "low"
