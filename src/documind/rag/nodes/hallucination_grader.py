"""Hallucination Grader node per §7.4 — QUERY role claim verification.

Evaluates each claim against supplied evidence.  Produces structured
unsupported/partial/grounded issues.  Two-revision cap; unsupported
claims after cap trigger safe abstention.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from documind.rag.prompts.safety import wrap_with_safety
from documind.rag.state import (
    MAX_GENERATION_REVISIONS,
    AgentState,
    EvidenceCache,
    HallucinationGrade,
    HallucinationIssue,
)
from documind.services.llm_service import ModelRole

logger = structlog.get_logger(__name__)


async def hallucination_grader_node(
    state: AgentState,
    *,
    llm_service: Any,
    evidence_cache: EvidenceCache,
    prompt_registry: Any | None = None,
) -> dict[str, Any]:
    """Grade draft answer claims for hallucination.

    Returns structured issues and determines if revision is needed.
    Revision restricted to removal/qualification/rewrite of existing claims.
    """
    draft = state.get("draft_answer")
    if draft is None:
        return {
            "hallucination_grade": HallucinationGrade(all_grounded=False),
            "agent_path": [*state.get("agent_path", []), "hallucination:no_draft"],
        }

    revisions = state.get("generation_revisions", 0)

    # Check revision cap.
    if revisions >= MAX_GENERATION_REVISIONS:
        # Check if there are unsupported claims.
        has_unsupported = any(not c.grounded for c in draft.claims)
        if has_unsupported:
            return {
                "hallucination_grade": HallucinationGrade(
                    all_grounded=False, needs_revision=False,
                ),
                "abstention_reason": "Unsupported claims after maximum revisions",
                "agent_path": [
                    *state.get("agent_path", []),
                    "hallucination:max_revisions_unsupported",
                ],
            }

    # Build evidence context.
    evidence_texts: list[str] = []
    reranked_ids = state.get("reranked_evidence_ids", [])
    for eid in reranked_ids:
        content = evidence_cache.get(eid)
        if content:
            evidence_texts.append(f"[Evidence {eid}]: {content[:500]}")

    # Build claims context.
    claims_text = "\n".join(
        f"[Claim {c.claim_id}]: {c.text} (evidence: {', '.join(c.evidence_ids)})"
        for c in draft.claims
    )

    template_text = (
        "Grade each claim for hallucination. Return JSON with 'issues', "
        "'needs_revision', and 'all_grounded'."
    )
    if prompt_registry is not None:
        try:
            template = prompt_registry.resolve("hallucination_grader")
            template_text = template.text
        except KeyError:
            pass

    system_prompt = wrap_with_safety(template_text)

    user_prompt = (
        f"Draft answer: {draft.text}\n\n"
        f"Claims:\n{claims_text}\n\n"
        f"Evidence:\n" + "\n\n".join(evidence_texts)
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    output_schema = {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim_id": {"type": "string"},
                        "grade": {
                            "type": "string",
                            "enum": ["unsupported", "partial", "grounded"],
                        },
                        "reason": {"type": "string"},
                        "suggested_action": {
                            "type": "string",
                            "enum": ["remove", "qualify", "rewrite", "keep"],
                        },
                    },
                    "required": ["claim_id", "grade"],
                },
            },
            "needs_revision": {"type": "boolean"},
            "all_grounded": {"type": "boolean"},
        },
        "required": ["issues", "needs_revision", "all_grounded"],
    }

    try:
        result = await llm_service.invoke(
            ModelRole.QUERY, messages, json_schema=output_schema,
        )

        if result.structured and result.structured.valid:
            parsed = result.structured.parsed
        else:
            try:
                parsed = json.loads(result.content)
            except (json.JSONDecodeError, TypeError):
                return {
                    "hallucination_grade": HallucinationGrade(all_grounded=True),
                    "agent_path": [
                        *state.get("agent_path", []),
                        "hallucination:parse_error_pass",
                    ],
                }

        issues: list[HallucinationIssue] = []
        for issue_data in parsed.get("issues", []):
            issues.append(
                HallucinationIssue(
                    claim_id=issue_data.get("claim_id", ""),
                    grade=issue_data.get("grade", "grounded"),
                    reason=issue_data.get("reason", ""),
                    suggested_action=issue_data.get("suggested_action", "keep"),
                )
            )

        needs_revision = parsed.get("needs_revision", False)
        all_grounded = parsed.get("all_grounded", True)

        grade = HallucinationGrade(
            issues=issues,
            needs_revision=needs_revision,
            all_grounded=all_grounded,
        )

        update: dict[str, Any] = {
            "hallucination_grade": grade,
            "agent_path": [
                *state.get("agent_path", []),
                f"hallucination:grounded={all_grounded},revision={needs_revision}",
            ],
        }

        # Increment revision counter if revision is needed.
        if needs_revision:
            update["generation_revisions"] = revisions + 1

        if prompt_registry is not None:
            prompt_registry.record_invocation(
                "hallucination_grader", 1, ModelRole.QUERY,
                input_valid=True, output_valid=True,
            )

        return update

    except Exception:
        logger.exception("hallucination_grader_error")
        return {
            "hallucination_grade": HallucinationGrade(all_grounded=True),
            "agent_path": [*state.get("agent_path", []), "hallucination:error_pass"],
        }
