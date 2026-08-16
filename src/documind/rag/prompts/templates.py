"""Prompt template definitions for all model-assisted RAG nodes per §7.2.

Each template specifies its permitted role, input/output JSON Schema,
token limits, and language rules.  SHA-256 hashes are computed on
registration for integrity verification.
"""

from __future__ import annotations

import hashlib

from documind.rag.prompts.registry import PromptTemplate
from documind.services.llm_service import ModelRole


def _with_hash(template: PromptTemplate) -> PromptTemplate:
    """Return a copy of the template with its SHA-256 hash computed."""
    sha = hashlib.sha256(template.text.encode("utf-8")).hexdigest()
    return PromptTemplate(
        name=template.name,
        revision=template.revision,
        text=template.text,
        permitted_role=template.permitted_role,
        input_schema=template.input_schema,
        output_schema=template.output_schema,
        max_input_tokens=template.max_input_tokens,
        max_output_tokens=template.max_output_tokens,
        language_rules=template.language_rules,
        sha256=sha,
    )


# ---------------------------------------------------------------------------
# Router prompt (§7.4 — KEYWORDS role)
# ---------------------------------------------------------------------------

ROUTER_PROMPT = _with_hash(PromptTemplate(
    name="router",
    revision=1,
    permitted_role=ModelRole.KEYWORDS,
    max_input_tokens=2048,
    max_output_tokens=256,
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "locale": {"type": "string"},
            "session_summary": {"type": "string"},
        },
        "required": ["question"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "route": {
                "type": "string",
                "enum": [
                    "simple_qa", "comparison", "aggregation", "extraction",
                    "summarization", "clarification", "out_of_scope",
                ],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "clarification_topic": {"type": "string"},
        },
        "required": ["route", "confidence"],
    },
    text=(
        "You are a query router for a document intelligence system. "
        "Classify the user's question into exactly one of these categories:\n"
        "- simple_qa: direct factual question answerable from document content\n"
        "- comparison: comparing two or more document versions or values\n"
        "- aggregation: calculating sums, averages, counts, or other numeric operations\n"
        "- extraction: extracting structured data from documents using a template\n"
        "- summarization: summarizing document content or sections\n"
        "- clarification: the question is ambiguous and needs clarification\n"
        "- out_of_scope: the question is unrelated to documents in the system\n\n"
        "Return a JSON object with 'route' (the category), 'confidence' (0-1), "
        "and optionally 'clarification_topic' if the route is 'clarification'.\n\n"
        "Do not choose an access policy. Do not choose a provider. "
        "Do not include any security-related fields."
    ),
))


# ---------------------------------------------------------------------------
# Planner prompt (§7.4 — QUERY role)
# ---------------------------------------------------------------------------

PLANNER_PROMPT = _with_hash(PromptTemplate(
    name="planner",
    revision=1,
    permitted_role=ModelRole.QUERY,
    max_input_tokens=4096,
    max_output_tokens=1024,
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "route": {"type": "string"},
            "available_documents": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["question", "route"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string"},
                        "description": {"type": "string"},
                        "document_selector": {"type": "string"},
                        "version_selector": {"type": "string"},
                        "entity_filter": {"type": "string"},
                        "date_filter": {"type": "string"},
                    },
                    "required": ["operation", "description"],
                },
            },
        },
        "required": ["steps"],
    },
    text=(
        "You are a query planner for a document intelligence system. "
        "Break the user's question into at most 5 declarative sub-tasks. "
        "Each sub-task must use one of these whitelisted operation types:\n"
        "- resolve_versions: find specific document versions\n"
        "- retrieve_evidence: search for relevant document content\n"
        "- extract_structured: extract data using an approved template\n"
        "- compare_versions: compare two document versions\n"
        "- aggregate_values: perform numeric calculations\n\n"
        "CONSTRAINTS:\n"
        "- Do NOT include any backend syntax (Cypher, SQL, search DSL)\n"
        "- Do NOT include group IDs, label IDs, or provider settings\n"
        "- Do NOT reference specific database tables or internal APIs\n"
        "- Use only document names, version selectors, and natural language filters\n\n"
        "Return a JSON object with a 'steps' array."
    ),
))


# ---------------------------------------------------------------------------
# Query Rewriter prompt (§7.4 — KEYWORDS role)
# ---------------------------------------------------------------------------

QUERY_REWRITER_PROMPT = _with_hash(PromptTemplate(
    name="query_rewriter",
    revision=1,
    permitted_role=ModelRole.KEYWORDS,
    max_input_tokens=2048,
    max_output_tokens=256,
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "session_context": {"type": "string"},
            "locale": {"type": "string"},
        },
        "required": ["question"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "string", "maxLength": 512},
            },
            "hints": {
                "type": "object",
                "properties": {
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "dates": {"type": "array", "items": {"type": "string"}},
                    "amounts": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "required": ["queries"],
    },
    text=(
        "You are a query rewriter for a document retrieval system. "
        "Given the user's question and optional session context, produce "
        "up to 3 alternative query formulations (max 512 characters each). "
        "Also extract entity, date, and amount hints.\n\n"
        "Rules:\n"
        "- Resolve coreferences from the session context\n"
        "- Normalize dates and quantities\n"
        "- Extract entity mentions as hints\n"
        "- Do NOT produce an answer — only query variants and hints\n"
        "- Do NOT make authority decisions — corrections like 'only for 2025' "
        "become validated date filter hints\n\n"
        "Return a JSON object with 'queries' (array of strings) and "
        "'hints' (object with entities, dates, amounts arrays)."
    ),
))


# ---------------------------------------------------------------------------
# Relevance Grader prompt (§7.4 — QUERY role)
# ---------------------------------------------------------------------------

RELEVANCE_GRADER_PROMPT = _with_hash(PromptTemplate(
    name="relevance_grader",
    revision=1,
    permitted_role=ModelRole.QUERY,
    max_input_tokens=4096,
    max_output_tokens=1024,
    output_schema={
        "type": "object",
        "properties": {
            "grades": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "evidence_id": {"type": "string"},
                        "grade": {
                            "type": "string",
                            "enum": ["relevant", "partially_relevant", "irrelevant", "needs_more_context"],
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["evidence_id", "grade"],
                },
            },
            "request_kind": {
                "type": "string",
                "enum": ["answer", "rewrite", "targeted_expansion", "abstain"],
            },
        },
        "required": ["grades", "request_kind"],
    },
    text=(
        "You are a relevance grader for a document intelligence system. "
        "For each piece of evidence, assess whether it is relevant to "
        "answering the user's question.\n\n"
        "Grade each evidence item as one of:\n"
        "- relevant: directly answers or supports the question\n"
        "- partially_relevant: contains some useful information\n"
        "- irrelevant: not related to the question\n"
        "- needs_more_context: relevant topic but insufficient for an answer\n\n"
        "Also determine the overall request kind:\n"
        "- answer: sufficient evidence to generate a response\n"
        "- rewrite: evidence is weak, try a different query formulation\n"
        "- targeted_expansion: need more context on a specific aspect\n"
        "- abstain: insufficient evidence to answer reliably\n\n"
        "Return a JSON object with 'grades' array and 'request_kind'."
    ),
))


# ---------------------------------------------------------------------------
# Extractor prompt (§7.4 — EXTRACT role)
# ---------------------------------------------------------------------------

EXTRACTOR_PROMPT = _with_hash(PromptTemplate(
    name="extractor",
    revision=1,
    permitted_role=ModelRole.EXTRACT,
    max_input_tokens=4096,
    max_output_tokens=4096,
    text=(
        "You are a structured data extractor. Extract data from the provided "
        "evidence according to the JSON Schema supplied in the system prompt. "
        "For each populated field, provide source spans (exact quotes from "
        "the evidence text). Return valid JSON matching the schema.\n\n"
        "Rules:\n"
        "- Only extract data that appears in the provided evidence\n"
        "- Include source_spans with exact text quotes and evidence_id references\n"
        "- Validate units where applicable\n"
        "- If a field cannot be populated from the evidence, omit it\n"
        "- Do NOT invent or fabricate data"
    ),
))


# ---------------------------------------------------------------------------
# Comparator prompt (§7.4 — QUERY role)
# ---------------------------------------------------------------------------

COMPARATOR_PROMPT = _with_hash(PromptTemplate(
    name="comparator",
    revision=1,
    permitted_role=ModelRole.QUERY,
    max_input_tokens=4096,
    max_output_tokens=2048,
    text=(
        "You are a document comparison analyst. Given deterministic diff "
        "results between two document versions, express meaningful changes "
        "in clear prose. Each statement must be a claim with explicit "
        "evidence references.\n\n"
        "Rules:\n"
        "- Every claim must reference specific evidence IDs\n"
        "- Use the deterministic diff as ground truth\n"
        "- Do NOT add information not present in the diff or evidence\n"
        "- Organize claims by significance"
    ),
))


# ---------------------------------------------------------------------------
# Generator prompt (§7.4 — QUERY role)
# ---------------------------------------------------------------------------

GENERATOR_PROMPT = _with_hash(PromptTemplate(
    name="generator",
    revision=1,
    permitted_role=ModelRole.QUERY,
    max_input_tokens=4096,
    max_output_tokens=2048,
    output_schema={
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
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["claim_id", "text", "evidence_ids"],
                },
            },
        },
        "required": ["answer", "claims"],
    },
    text=(
        "You are a document intelligence assistant. Generate an answer to "
        "the user's question using ONLY the provided authorized evidence. "
        "Every factual statement must be a numbered claim with explicit "
        "evidence ID references.\n\n"
        "STRICT RULES:\n"
        "- Do NOT cite document titles that aren't backed by evidence\n"
        "- Do NOT invent, fabricate, or hallucinate sources\n"
        "- Do NOT invoke tools or suggest tool calls\n"
        "- If evidence is insufficient, state that clearly\n"
        "- Include uncertainty when evidence is ambiguous\n\n"
        "Return a JSON object with 'answer' (text) and 'claims' "
        "(array of {claim_id, text, evidence_ids})."
    ),
))


# ---------------------------------------------------------------------------
# Hallucination Grader prompt (§7.4 — QUERY role)
# ---------------------------------------------------------------------------

HALLUCINATION_GRADER_PROMPT = _with_hash(PromptTemplate(
    name="hallucination_grader",
    revision=1,
    permitted_role=ModelRole.QUERY,
    max_input_tokens=4096,
    max_output_tokens=1024,
    output_schema={
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
    },
    text=(
        "You are a hallucination grader. Review each claim in the draft "
        "answer against the supplied evidence. Grade each claim as:\n"
        "- grounded: fully supported by the evidence\n"
        "- partial: partially supported, some details not in evidence\n"
        "- unsupported: not supported by any provided evidence\n\n"
        "For unsupported or partial claims, suggest one of:\n"
        "- remove: delete the claim entirely\n"
        "- qualify: add uncertainty language\n"
        "- rewrite: restate using only supported facts\n"
        "- keep: the claim is grounded, no action needed\n\n"
        "Return a JSON object with 'issues' array, 'needs_revision' boolean, "
        "and 'all_grounded' boolean."
    ),
))


# ---------------------------------------------------------------------------
# Session Compactor prompt (§7.1 — KEYWORDS role)
# ---------------------------------------------------------------------------

SESSION_COMPACTOR_PROMPT = _with_hash(PromptTemplate(
    name="session_compactor",
    revision=1,
    permitted_role=ModelRole.KEYWORDS,
    max_input_tokens=4096,
    max_output_tokens=256,
    text=(
        "Summarize this conversation history concisely. Focus on:\n"
        "- Key topics discussed\n"
        "- Decisions made\n"
        "- Unresolved user constraints and document/version references\n"
        "- Context needed to continue the conversation\n\n"
        "Rules:\n"
        "- Do NOT cache or reproduce external document content\n"
        "- Do NOT include cross-session information\n"
        "- Do NOT turn chat history into a retrieval corpus\n"
        "- Keep the summary factual and bounded"
    ),
))


# ---------------------------------------------------------------------------
# All templates for registry loading
# ---------------------------------------------------------------------------

ALL_TEMPLATES: list[PromptTemplate] = [
    ROUTER_PROMPT,
    PLANNER_PROMPT,
    QUERY_REWRITER_PROMPT,
    RELEVANCE_GRADER_PROMPT,
    EXTRACTOR_PROMPT,
    COMPARATOR_PROMPT,
    GENERATOR_PROMPT,
    HALLUCINATION_GRADER_PROMPT,
    SESSION_COMPACTOR_PROMPT,
]
