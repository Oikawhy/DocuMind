"""Citation Verifier node per §7.4 — deterministic citation checks.

Checks: all claims have ≥1 citation, cited chunk in evidence set,
canonical version/chunk/source-offset integrity, principal still has
access, graph path → allowed source.  Checks for tombstone/authorization
change after draft generation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from documind.rag.state import AgentState, Citation, CitationVerification

if TYPE_CHECKING:
    from documind.domain.authorization_context import AuthorizationContext

logger = structlog.get_logger(__name__)


async def citation_verifier_node(
    state: AgentState,
    *,
    auth_context: AuthorizationContext | None = None,
    session_factory: Any | None = None,
) -> dict[str, Any]:
    """Deterministically verify all citations from the draft answer.

    T8-12: Sets ``abstention_reason`` when citations are invalid.
    T8-28: Uses ``auth_context.document_ids`` for doc-ID checks,
    always performs the check (no truthiness guard on empty set).
    """
    draft = state.get("draft_answer")
    if draft is None:
        return {
            "citation_verification": CitationVerification(all_valid=False, failure_code="NO_DRAFT"),
            "agent_path": [*state.get("agent_path", []), "citation_verify:no_draft"],
        }

    reranked_ids = set(state.get("reranked_evidence_ids", []))
    all_valid = True
    verified: list[Citation] = []
    invalid: list[Citation] = []
    failure_code: str | None = None

    # Check that every claim has at least one evidence reference.
    uncovered_claims: list[str] = []
    for claim in draft.claims:
        if not claim.evidence_ids:
            uncovered_claims.append(claim.claim_id)
            all_valid = False

    if uncovered_claims:
        failure_code = "UNCOVERED_CLAIMS"

    # Verify each claim's evidence IDs are in the authorized set.
    for claim in draft.claims:
        for eid in claim.evidence_ids:
            citation = Citation(
                citation_id=f"cit_{claim.claim_id}_{eid}",
                claim_id=claim.claim_id,
                document_id="",
                version_id="",
                version_number=0,
                chunk_id=eid,
            )

            if eid not in reranked_ids:
                all_valid = False
                invalid.append(Citation(
                    citation_id=citation.citation_id,
                    claim_id=claim.claim_id,
                    document_id="",
                    version_id="",
                    version_number=0,
                    chunk_id=eid,
                    valid=False,
                    invalidation_reason="Evidence ID not in authorized set",
                ))
                if failure_code is None:
                    failure_code = "INVALID_CITATIONS"
                continue

            verified.append(citation)

    # Database integrity checks if session available.
    if session_factory is not None and (verified or invalid):
        from documind.rag.tools.verify_citations import (
            VerifyCitationsInput,
            verify_citations,
        )

        claims_data = [
            {"claim_id": c.claim_id, "text": c.text}
            for c in draft.claims
        ]
        citations_data = [
            {
                "citation_id": c.citation_id,
                "claim_id": c.claim_id,
                "chunk_id": c.chunk_id,
                "content_sha256": c.content_sha256,
            }
            for c in verified
        ]

        # T8-28: Always pass document IDs from auth_context.
        doc_ids = auth_context.document_ids if auth_context else set()
        sf = auth_context.session_factory if auth_context else session_factory

        input_data = VerifyCitationsInput(
            claims=claims_data,
            citations=citations_data,
            evidence_ids=list(reranked_ids),
            principal_subject=state["principal_subject"],
        )

        try:
            async with sf() as session:
                db_result = await verify_citations(
                    input_data,
                    session=session,
                    allowed_document_ids=doc_ids,
                )

            if not db_result.all_valid:
                all_valid = False
                failure_code = db_result.failure_code or "INVALID_CITATIONS"
                # Move invalid citations.
                for status in db_result.statuses:
                    if not status.valid:
                        for cit in verified[:]:
                            if cit.citation_id == status.citation_id:
                                verified.remove(cit)
                                invalid.append(Citation(
                                    citation_id=cit.citation_id,
                                    claim_id=cit.claim_id,
                                    document_id=cit.document_id,
                                    version_id=cit.version_id,
                                    version_number=cit.version_number,
                                    chunk_id=cit.chunk_id,
                                    valid=False,
                                    invalidation_reason=status.reason,
                                ))

            # T8-27: Populate provenance on verified citations.
            provenance_by_id = {
                s.citation_id: s.provenance
                for s in db_result.statuses
                if s.valid and s.provenance
            }
            enriched: list[Citation] = []
            for cit in verified:
                prov = provenance_by_id.get(cit.citation_id, {})
                if prov:
                    enriched.append(Citation(
                        citation_id=cit.citation_id,
                        claim_id=cit.claim_id,
                        document_id=prov.get("document_id", cit.document_id),
                        version_id=prov.get("version_id", cit.version_id),
                        version_number=prov.get("version_number", cit.version_number),
                        chunk_id=cit.chunk_id,
                        page_start=prov.get("page_start", cit.page_start),
                        page_end=prov.get("page_end", cit.page_end),
                        section_path=prov.get("section_path", cit.section_path),
                        content_sha256=prov.get("content_sha256", cit.content_sha256),
                        valid=True,
                    ))
                else:
                    enriched.append(cit)
            verified = enriched
        except Exception:
            logger.exception("citation_db_check_error")

    verification = CitationVerification(
        all_valid=all_valid,
        verified_citations=verified,
        invalid_citations=invalid,
        failure_code=failure_code,
    )

    # T8-12: Set abstention_reason when citations are invalid.
    update: dict[str, Any] = {
        "citation_verification": verification,
        "agent_path": [
            *state.get("agent_path", []),
            f"citation_verify:valid={all_valid},verified={len(verified)},invalid={len(invalid)}",
        ],
    }
    if not all_valid:
        update["abstention_reason"] = f"Citation verification failed: {failure_code}"

    return update
