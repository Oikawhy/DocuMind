"""Generator node per §7.4 — QUERY role answer generation.

Receives authorized evidence and analysis results.  Produces answer text
with explicit claim IDs and claim-to-evidence references.  Excludes
uncited titles, invented sources, and tool calls.

T8-14: Parse failures produce abstention, not synthetic blanket-cited claims.
"""

from __future__ import annotations

from typing import Any

import structlog

from documind.rag.prompts.safety import wrap_with_safety
from documind.rag.state import AgentState, Claim, DraftAnswer, EvidenceCache
from documind.services.llm_service import ModelRole

logger = structlog.get_logger(__name__)


async def generator_node(
    state: AgentState,
    *,
    llm_service: Any,
    evidence_cache: EvidenceCache,
    prompt_registry: Any | None = None,
) -> dict[str, Any]:
    """Generate an answer from authorized evidence.

    Produces a ``DraftAnswer`` with explicit claims and evidence references.
    """
    reranked_ids = state.get("reranked_evidence_ids", [])

    if not reranked_ids:
        return {
            "draft_answer": None,
            "abstention_reason": "No evidence available for generation",
            "agent_path": [*state.get("agent_path", []), "generator:no_evidence"],
        }

    # Build evidence context.
    evidence_texts: list[str] = []
    for eid in reranked_ids:
        content = evidence_cache.get(eid)
        if content:
            evidence_texts.append(f"[Evidence {eid}]: {content[:1000]}")

    if not evidence_texts:
        return {
            "draft_answer": None,
            "abstention_reason": "No evidence content available",
            "agent_path": [*state.get("agent_path", []), "generator:no_content"],
        }

    # Include analysis results if available.
    analysis_context = ""
    if state.get("comparison_result"):
        comp = state["comparison_result"]
        analysis_context += f"\nComparison: {comp.text_diff}"
    if state.get("aggregation_result"):
        agg = state["aggregation_result"]
        analysis_context += (
            f"\nAggregation ({agg.operation}): {agg.result} {agg.unit or ''}"
        )
    if state.get("extraction_results"):
        for ext in state["extraction_results"]:
            if ext.valid and not ext.pending_template:
                analysis_context += f"\nExtraction: {ext.fields}"

    template_text = (
        "Generate an answer using ONLY the provided authorized evidence. "
        "Return JSON with 'answer' and 'claims' array."
    )
    if prompt_registry is not None:
        try:
            template = prompt_registry.resolve("generator")
            template_text = template.text
        except KeyError:
            pass

    system_prompt = wrap_with_safety(template_text)

    user_prompt = (
        f"Question: {state['original_question']}\n"
        f"Route: {state.get('route_type', 'simple_qa')}\n\n"
        f"Evidence:\n" + "\n\n".join(evidence_texts)
    )
    if analysis_context:
        user_prompt += f"\n\nAnalysis results:{analysis_context}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    output_schema = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim_id": {"type": "string"},
                        "text": {"type": "string"},
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["claim_id", "text", "evidence_ids"],
                },
            },
        },
        "required": ["answer", "claims"],
    }

    try:
        from documind.rag.invoke_helpers import invoke_with_retry

        parsed, limitation_code = await invoke_with_retry(
            llm_service, ModelRole.QUERY, messages,
            output_schema=output_schema,
            template_name="generator",
        )

        # T8-14: Fail-closed on parse failure — no synthetic claims.
        if parsed is None:
            return {
                "draft_answer": None,
                "abstention_reason": f"Generator output invalid ({limitation_code})",
                "agent_path": [
                    *state.get("agent_path", []),
                    f"generator:parse_error_abstain:{limitation_code}",
                ],
            }

        answer_text = parsed.get("answer", "")
        raw_claims = parsed.get("claims", [])

        # Build claims with evidence validation.
        authorized_set = set(reranked_ids)
        claims: list[Claim] = []
        for claim_data in raw_claims:
            eid_refs = claim_data.get("evidence_ids", [])
            # Only keep evidence IDs that are in the authorized set.
            valid_eids = [e for e in eid_refs if e in authorized_set]
            claims.append(
                Claim(
                    claim_id=claim_data.get("claim_id", f"gen_{len(claims)}"),
                    text=claim_data.get("text", ""),
                    evidence_ids=valid_eids,
                    grounded=len(valid_eids) > 0,
                )
            )

        draft = DraftAnswer(
            text=answer_text,
            claims=claims,
            evidence_ids=reranked_ids,
        )

        if prompt_registry is not None:
            prompt_registry.record_invocation(
                "generator", 1, ModelRole.QUERY,
                input_valid=True, output_valid=True,
            )

        return {
            "draft_answer": draft,
            "agent_path": [
                *state.get("agent_path", []),
                f"generator:{len(claims)}_claims",
            ],
        }

    except Exception as exc:
        logger.exception("generator_node_error")
        return {
            "draft_answer": None,
            "abstention_reason": f"Generation error: {type(exc).__name__}",
            "agent_path": [*state.get("agent_path", []), "generator:error"],
        }
