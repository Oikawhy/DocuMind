"""Comparator node per §7.4 — deterministic diff with prose claims.

Produces deterministic normalized text/structured/timeline diffs.
Prose generation uses the QUERY role.  Emits claim/evidence pairs.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from documind.rag.prompts.safety import wrap_with_safety
from documind.rag.state import AgentState, Claim, ComparisonResult, EvidenceCache
from documind.services.llm_service import ModelRole

logger = structlog.get_logger(__name__)


async def comparator_node(
    state: AgentState,
    *,
    llm_service: Any,
    evidence_cache: EvidenceCache,
    prompt_registry: Any | None = None,
) -> dict[str, Any]:
    """Compare document versions and emit claims with evidence.

    Only runs for ``comparison`` route type.
    """
    route = state.get("route_type", "simple_qa")

    if route != "comparison":
        return {
            "comparison_result": None,
            "agent_path": [*state.get("agent_path", []), "comparator:skipped"],
        }

    from documind.rag.tools.compare_versions import CompareVersionsInput, compare_versions

    resolved = state.get("resolved_versions", [])
    if len(resolved) < 2:
        return {
            "comparison_result": None,
            "abstention_reason": "Insufficient versions for comparison",
            "agent_path": [*state.get("agent_path", []), "comparator:insufficient_versions"],
        }

    left = resolved[0]
    right = resolved[1]

    reranked_ids = state.get("reranked_evidence_ids", [])

    # Deterministic diff.
    input_data = CompareVersionsInput(
        left_version_id=left.version_id,
        right_version_id=right.version_id,
        evidence_ids=reranked_ids,
    )

    # T8-20: Build evidence_metadata from state.
    evidence_metadata = state.get("evidence_version_metadata", {})
    diff_result = await compare_versions(
        input_data, evidence_cache, evidence_metadata=evidence_metadata,
    )

    # Generate prose claims via QUERY role.
    claims: list[Claim] = []
    if diff_result.structured_diff:
        template_text = (
            "Express these document differences as clear claims with evidence references."
        )
        if prompt_registry is not None:
            try:
                template = prompt_registry.resolve("comparator")
                template_text = template.text
            except KeyError:
                pass

        system_prompt = wrap_with_safety(template_text)

        diff_summary = json.dumps(
            [d.model_dump() for d in diff_result.structured_diff],
            default=str,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Differences:\n{diff_summary}"},
        ]

        # T8-21: Output schema for structured claim generation.
        output_schema = {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim_id": {"type": "string"},
                            "text": {"type": "string"},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["claim_id", "text", "evidence_ids"],
                    },
                },
            },
            "required": ["claims"],
        }

        try:
            from documind.rag.invoke_helpers import invoke_with_retry

            parsed, limitation_code = await invoke_with_retry(
                llm_service, ModelRole.QUERY, messages,
                output_schema=output_schema,
                template_name="comparator",
            )

            if parsed is None:
                # T8-21: Fail-closed on parse failure.
                return {
                    "comparison_result": None,
                    "abstention_reason": f"Comparator output invalid ({limitation_code})",
                    "agent_path": [
                        *state.get("agent_path", []),
                        f"comparator:parse_error_abstain:{limitation_code}",
                    ],
                }

            raw_claims = parsed.get("claims", []) if isinstance(parsed, dict) else []
            authorized_ids = set(reranked_ids)
            for i, claim_data in enumerate(raw_claims):
                # T8-21: Validate evidence_ids against authorized set.
                claim_eids = claim_data.get("evidence_ids", [])
                valid_eids = [e for e in claim_eids if e in authorized_ids]
                claims.append(
                    Claim(
                        claim_id=claim_data.get("claim_id", f"comp_claim_{i}"),
                        text=claim_data.get("text", ""),
                        evidence_ids=valid_eids,
                    )
                )
        except Exception:
            logger.exception("comparator_prose_error")

    comparison = ComparisonResult(
        left_version_id=left.version_id,
        right_version_id=right.version_id,
        text_diff=diff_result.text_diff,
        structured_diff={
            "entries": [d.model_dump() for d in diff_result.structured_diff],
        },
        claims=claims,
    )

    return {
        "comparison_result": comparison,
        "agent_path": [
            *state.get("agent_path", []),
            f"comparator:{len(claims)}_claims",
        ],
    }
