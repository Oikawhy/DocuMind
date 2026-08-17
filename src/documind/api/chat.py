"""Chat endpoints per §7.1 (memory) and §9.3 (API).

Session creation, message persistence, memory window loading, session
compaction at 30 messages, retention-aware listing, and feedback scoring.
Chat is disabled by default; ``POST /v1/chat`` returns 403 ``CHAT_DISABLED``
when ``settings.chat_enabled`` is False.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.domain.errors import (
    AuthenticationError,
    ChatDisabledError,
    DomainError,
    ResourceNotFoundError,
)
from documind.models.chat import AgentRun, ChatMessage, ChatSession
from documind.schemas.chat import (
    ChatRequest,
    ChatResponse,
    CitationOut,
    FeedbackRequest,
    MessageOut,
    SessionDetail,
    SessionSummary,
)
from documind.schemas.common import CursorPage, error_response
from documind.services.audit_service import AuditEntry

router = APIRouter(prefix="/v1", tags=["chat"])

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _principal(request: Request) -> object:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise AuthenticationError()
    return principal


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise DomainError("Database session factory is unavailable.", code="DEPENDENCY_UNAVAILABLE")
    return factory


def _settings(request: Request) -> Any:
    return getattr(request.app.state, "settings", None)


def _audit_service(request: Request) -> Any:
    return getattr(request.app.state, "audit_service", None)


def _llm_service(request: Request) -> Any:
    return getattr(request.app.state, "llm_service", None)


# ---------------------------------------------------------------------------
# Memory loader per §7.1
# ---------------------------------------------------------------------------


async def _load_session_messages(
    session: AsyncSession,
    session_id: uuid.UUID,
    *,
    window: int = 20,
    max_tokens: int = 4096,
) -> tuple[list[ChatMessage], str | None]:
    """Load session summary + newest ``window`` messages, oldest-first, capped at ``max_tokens``.

    Returns (messages, summary_text).
    """
    # Load all messages ordered newest-first.
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc())
    )
    result = await session.execute(stmt)
    all_msgs = list(result.scalars().all())

    if not all_msgs:
        return [], None

    # Take newest `window` messages.
    window_msgs = all_msgs[:window]
    window_msgs.reverse()  # oldest-first

    # Look for a compaction summary message (T8-34: assistant role with new prefix).
    summary_text: str | None = None
    for msg in reversed(all_msgs):
        if msg.role == "assistant" and msg.content.startswith("[COMPACTION_SUMMARY]"):
            summary_text = msg.content.removeprefix("[COMPACTION_SUMMARY] ").strip()
            break
        # Backward compatibility with legacy system-role summaries.
        if msg.role == "system" and msg.content.startswith("[SESSION SUMMARY]"):
            summary_text = msg.content.removeprefix("[SESSION SUMMARY] ").strip()
            break

    # T9-04: Token budget enforcement — summary is counted first.
    budget = max_tokens

    if summary_text:
        summary_tokens = len(summary_text.split())
        if summary_tokens >= budget:
            # Summary alone exceeds budget — truncate it, no room for messages.
            words = summary_text.split()
            summary_text = " ".join(words[:budget])
            return [], summary_text
        budget -= summary_tokens

    kept: list[ChatMessage] = []
    for msg in window_msgs:
        tokens = msg.token_count or len(msg.content.split())
        if budget - tokens < 0 and kept:
            break
        budget -= tokens
        kept.append(msg)

    return kept, summary_text


async def _maybe_compact_session(
    session: AsyncSession,
    session_id: uuid.UUID,
    llm_service: Any,
    *,
    threshold: int = 30,
    token_budget: int = 4096,
) -> None:
    """At ``threshold`` messages, summarize older messages outside the window.

    T8-34: Uses registered SESSION_COMPACTOR_PROMPT + wrap_with_safety().
    T8-35: Enforces token budget via character-based estimation.
    """
    count_stmt = (
        select(func.count())
        .select_from(ChatMessage)
        .where(ChatMessage.session_id == session_id)
    )
    result = await session.execute(count_stmt)
    total = result.scalar_one()

    if total < threshold:
        return

    # Check if compaction already happened for this session.
    existing = await session.execute(
        select(func.count())
        .select_from(ChatMessage)
        .where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "assistant",
            ChatMessage.content.startswith("[COMPACTION_SUMMARY]"),
        )
    )
    if existing.scalar_one() > 0:
        return

    # Load oldest messages outside the window for summarization.
    oldest_stmt = (
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
        .limit(total - 20)  # Everything outside the newest 20
    )
    oldest_result = await session.execute(oldest_stmt)
    old_msgs = list(oldest_result.scalars().all())

    if not old_msgs:
        return

    # T8-35: Truncate from oldest until conversation fits token budget.
    # ~4 chars per token is a conservative estimate.
    def _estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)

    conversation_parts: list[str] = []
    token_count = 0
    for m in old_msgs:
        part = f"{m.role}: {m.content[:500]}"
        part_tokens = _estimate_tokens(part)
        if token_count + part_tokens > token_budget:
            break
        conversation_parts.append(part)
        token_count += part_tokens

    conversation = "\n".join(conversation_parts)

    if llm_service is not None:
        try:
            # T8-34: Use registered template + safety wrapper.
            from documind.rag.prompts.safety import wrap_with_safety

            try:
                from documind.rag.prompts.registry import build_default_registry
                registry = build_default_registry()
                template = registry.resolve("session_compactor")
                system_prompt = wrap_with_safety(template.text)
            except (KeyError, ImportError):
                system_prompt = wrap_with_safety(
                    "Summarize this conversation history concisely. Focus on key topics, "
                    "decisions, and context that would help continue the conversation."
                )

            summary = await llm_service.complete(
                role="KEYWORDS",
                system_prompt=system_prompt,
                user_prompt=conversation[:4000],
            )

            # T8-35: Ensure summary doesn't exceed remaining budget.
            summary_tokens = _estimate_tokens(summary)
            if summary_tokens > token_budget:
                summary = summary[:token_budget * 4]
        except Exception:
            await logger.awarning("session_compaction_llm_failed", session_id=str(session_id))
            summary = f"Conversation with {len(old_msgs)} earlier messages."
    else:
        summary = f"Conversation with {len(old_msgs)} earlier messages."

    # T8-34: Use role="assistant" with metadata flag instead of "system".
    summary_msg = ChatMessage(
        id=uuid.uuid4(),
        session_id=session_id,
        role="assistant",
        content=f"[COMPACTION_SUMMARY] {summary}",
        token_count=_estimate_tokens(summary),
    )
    session.add(summary_msg)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/chat", response_model=ChatResponse)
async def post_chat(
    request: Request,
    body: ChatRequest,
) -> ChatResponse | JSONResponse:
    """Send a chat message and receive a RAG-powered response.

    Creates a new session when ``session_id`` is absent.
    Returns 403 ``CHAT_DISABLED`` when chat is not enabled.
    """
    try:
        principal = _principal(request)
        app_settings = _settings(request)

        # Gate: chat disabled by default.
        chat_enabled = getattr(app_settings, "chat_enabled", False) if app_settings else False
        if not chat_enabled:
            raise ChatDisabledError()

        sf = _session_factory(request)
        audit = _audit_service(request)
        llm = _llm_service(request)

        chat_retention_days = getattr(app_settings, "chat_retention_days", 30) if app_settings else 30
        chat_window = getattr(app_settings, "chat_history_window", 20) if app_settings else 20
        chat_max_tokens = getattr(app_settings, "chat_history_max_tokens", 4096) if app_settings else 4096
        chat_compaction_threshold = getattr(app_settings, "chat_compaction_threshold", 30) if app_settings else 30

        trace_id = uuid.uuid4()

        async with sf() as session, session.begin():
            # Resolve or create session.
            if body.session_id is not None:
                sess_stmt = select(ChatSession).where(
                    ChatSession.id == body.session_id,
                    ChatSession.subject == principal.subject,
                )
                sess_result = await session.execute(sess_stmt)
                chat_session = sess_result.scalar_one_or_none()
                if chat_session is None:
                    raise ResourceNotFoundError("Chat session not found.", code="SESSION_NOT_FOUND")
            else:
                chat_session = ChatSession(
                    id=uuid.uuid4(),
                    subject=principal.subject,
                    retention_expires_at=datetime.now(UTC) + timedelta(days=chat_retention_days),
                )
                session.add(chat_session)
                await session.flush()

            # Persist user message.
            user_msg = ChatMessage(
                id=uuid.uuid4(),
                session_id=chat_session.id,
                role="user",
                content=body.message,
                token_count=len(body.message.split()),
            )
            session.add(user_msg)
            await session.flush()

            # Load memory context.
            history, summary = await _load_session_messages(
                session,
                chat_session.id,
                window=chat_window,
                max_tokens=chat_max_tokens,
            )

            # Build RAG query via the retrieval/RAG pipeline if available.
            rag_service = getattr(request.app.state, "rag_service", None)
            answer = ""
            citations: list[CitationOut] = []
            confidence: float | None = None
            route: str | None = None
            agent_path: list[str] = []
            policy_revisions: list[str] = []
            abstained = False

            if rag_service is not None:
                try:
                    # T8-01: Build AuthorizationContext from request state.
                    from documind.domain.authorization_context import AuthorizationContext

                    auth_svc = getattr(request.app.state, "authorization_service", None)
                    auth_ctx = AuthorizationContext(
                        principal=principal,
                        authorization_service=auth_svc,
                        session_factory=sf,
                        document_ids=frozenset(
                            str(d) for d in (body.document_ids or [])
                        ),
                    )

                    rag_result = await rag_service.run_rag_query(
                        question=body.message,
                        auth_context=auth_ctx,
                        session_id=str(body.session_id) if body.session_id else None,
                        session_summary=summary,
                        chat_history=[
                            {"role": m.role, "content": m.content} for m in history
                        ],
                        locale=getattr(body, "locale", "en") or "en",
                        trace_id=str(trace_id),
                    )

                    # T8-02: Access RAGResponse as dataclass attributes, not dict.
                    answer = rag_result.answer or ""
                    abstained = rag_result.abstained
                    confidence = rag_result.confidence
                    route = rag_result.route
                    agent_path = rag_result.agent_path
                    policy_revisions = list(rag_result.policy_revisions.keys()) if rag_result.policy_revisions else []
                    raw_citations = rag_result.citations
                    citations = [
                        CitationOut(
                            citation_id=c.get("citation_id", "") if isinstance(c, dict) else getattr(c, "citation_id", ""),
                            document_id=c.get("document_id", "") if isinstance(c, dict) else getattr(c, "document_id", ""),
                            version_id=c.get("version_id", "") if isinstance(c, dict) else getattr(c, "version_id", ""),
                            version_number=c.get("version_number", 0) if isinstance(c, dict) else getattr(c, "version_number", 0),
                            chunk_id=c.get("chunk_id", "") if isinstance(c, dict) else getattr(c, "chunk_id", ""),
                            excerpt=c.get("excerpt", "") if isinstance(c, dict) else getattr(c, "excerpt", ""),
                            content_sha256=c.get("content_sha256", "") if isinstance(c, dict) else getattr(c, "content_sha256", ""),
                        )
                        for c in raw_citations
                    ]
                except Exception:
                    await logger.aexception("rag_query_failed", session_id=str(chat_session.id))
                    answer = "I'm unable to process your request right now."
                    abstained = True
            else:
                answer = "The RAG service is not currently available."
                abstained = True

            # Persist assistant response.
            assistant_msg = ChatMessage(
                id=uuid.uuid4(),
                session_id=chat_session.id,
                role="assistant",
                content=answer,
                token_count=len(answer.split()),
            )
            session.add(assistant_msg)
            await session.flush()

            # Record agent run — T9-08: populate all available fields.
            agent_run = AgentRun(
                id=uuid.uuid4(),
                session_id=chat_session.id,
                trigger_message_id=user_msg.id,
                graph_state_checkpoint={
                    "agent_path": agent_path,
                    "route": route,
                    "abstained": abstained,
                },
                result_message_id=assistant_msg.id,
                trace_id=trace_id,
                # T9-08: Persisted RAG metadata fields.
                confidence=confidence if isinstance(confidence, str) else None,
                principal_subject=principal.subject,
                citation_ids=[c.citation_id for c in citations] if citations else None,
                policy_revisions=(
                    rag_result.policy_revisions
                    if rag_service is not None and not abstained and hasattr(rag_result, "policy_revisions")
                    else None
                ),
                model_route_revisions=(
                    rag_result.model_route_revisions
                    if rag_service is not None and not abstained and hasattr(rag_result, "model_route_revisions")
                    else None
                ),
                prompt_revisions=(
                    rag_result.prompt_revisions
                    if rag_service is not None and not abstained and hasattr(rag_result, "prompt_revisions")
                    else None
                ),
                abstention_reason=(
                    rag_result.abstention_reason
                    if rag_service is not None and hasattr(rag_result, "abstention_reason")
                    else None
                ),
                retry_count=(
                    rag_result.retry_count
                    if rag_service is not None and not abstained and hasattr(rag_result, "retry_count")
                    else None
                ),
                timing=(
                    {"total_ms": rag_result.timing_ms}
                    if rag_service is not None and not abstained and hasattr(rag_result, "timing_ms")
                    else None
                ),
            )
            session.add(agent_run)

            # Compact session if above threshold.
            await _maybe_compact_session(
                session,
                chat_session.id,
                llm,
                threshold=chat_compaction_threshold,
            )

        # Audit event (outside transaction for non-blocking).
        if audit is not None:
            await audit.write_event(
                AuditEntry(
                    actor_subject=principal.subject,
                    action="chat.message.created",
                    resource_type="chat_session",
                    resource_id=str(chat_session.id),
                    details={"message_id": str(user_msg.id), "abstained": abstained},
                    trace_id=trace_id,
                )
            )

        return ChatResponse(
            session_id=chat_session.id,
            message_id=assistant_msg.id,
            answer=answer,
            abstained=abstained,
            citations=citations,
            confidence=confidence,
            route=route,
            agent_path=agent_path,
            policy_revisions=policy_revisions,
            trace_id=trace_id,
        )
    except DomainError as exc:
        return error_response(request, exc)


@router.get("/chat/sessions", response_model=None)
async def list_sessions(
    request: Request,
    cursor: str | None = None,
    limit: int = 20,
) -> CursorPage | JSONResponse:
    """List caller-owned chat sessions, cursor-paginated.

    T9-05: Uses opaque ``(created_at, id)`` compound cursor for stable
    gap-free pagination aligned with ``created_at DESC`` order.
    """
    import base64
    import json

    from sqlalchemy import tuple_

    try:
        principal = _principal(request)
        sf = _session_factory(request)

        async with sf() as session:
            stmt = (
                select(ChatSession)
                .where(ChatSession.subject == principal.subject)
                .order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
                .limit(limit + 1)
            )
            if cursor:
                try:
                    decoded = json.loads(base64.urlsafe_b64decode(cursor))
                    cursor_ts = datetime.fromisoformat(decoded["t"])
                    cursor_id = uuid.UUID(decoded["i"])
                    stmt = stmt.where(
                        tuple_(ChatSession.created_at, ChatSession.id)
                        < tuple_(cursor_ts, cursor_id)
                    )
                except (ValueError, KeyError, Exception):
                    pass

            result = await session.execute(stmt)
            sessions = list(result.scalars().all())

            # Count messages per session.
            items: list[dict[str, Any]] = []
            for s in sessions[:limit]:
                count_stmt = (
                    select(func.count())
                    .select_from(ChatMessage)
                    .where(ChatMessage.session_id == s.id)
                )
                count_result = await session.execute(count_stmt)
                msg_count = count_result.scalar_one()

                items.append(
                    SessionSummary(
                        id=s.id,
                        created_at=s.created_at,
                        retention_expires_at=s.retention_expires_at,
                        message_count=msg_count,
                    ).model_dump(mode="json")
                )

            next_cursor = None
            if len(sessions) > limit:
                last = sessions[limit - 1]
                cursor_data = {
                    "t": last.created_at.isoformat(),
                    "i": str(last.id),
                }
                next_cursor = base64.urlsafe_b64encode(
                    json.dumps(cursor_data).encode()
                ).decode()

        return CursorPage(items=items, next_cursor=next_cursor)
    except DomainError as exc:
        return error_response(request, exc)


@router.get("/chat/sessions/{session_id}", response_model=SessionDetail)
async def get_session(
    request: Request,
    session_id: uuid.UUID,
) -> SessionDetail | JSONResponse:
    """Return authorized session detail with message history."""
    try:
        principal = _principal(request)
        sf = _session_factory(request)

        async with sf() as session:
            stmt = select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.subject == principal.subject,
            )
            result = await session.execute(stmt)
            chat_session = result.scalar_one_or_none()
            if chat_session is None:
                raise ResourceNotFoundError("Chat session not found.", code="SESSION_NOT_FOUND")

            msg_stmt = (
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.asc())
            )
            msg_result = await session.execute(msg_stmt)
            messages = list(msg_result.scalars().all())

            # Find summary.
            summary_text: str | None = None
            for m in messages:
                if m.role == "system" and m.content.startswith("[SESSION SUMMARY]"):
                    summary_text = m.content.removeprefix("[SESSION SUMMARY] ").strip()
                elif m.role == "assistant" and m.content.startswith("[COMPACTION_SUMMARY]"):
                    summary_text = m.content.removeprefix("[COMPACTION_SUMMARY] ").strip()

            # T9-08: Load AgentRun metadata for assistant messages.
            agent_runs_stmt = (
                select(AgentRun)
                .where(AgentRun.session_id == session_id)
            )
            ar_result = await session.execute(agent_runs_stmt)
            agent_runs = {
                ar.result_message_id: ar
                for ar in ar_result.scalars().all()
                if ar.result_message_id is not None
            }

        msg_out_list: list[MessageOut] = []
        for m in messages:
            if m.role == "system" and m.content.startswith("[SESSION SUMMARY]"):
                continue
            if m.role == "assistant" and m.content.startswith("[COMPACTION_SUMMARY]"):
                continue
            ar = agent_runs.get(m.id)
            msg_out_list.append(
                MessageOut(
                    id=m.id,
                    role=m.role,
                    content=m.content,
                    token_count=m.token_count,
                    created_at=m.created_at,
                    citations=ar.citation_ids if ar else None,
                    confidence=ar.confidence if ar else None,
                    route=(ar.graph_state_checkpoint or {}).get("route") if ar else None,
                )
            )

        return SessionDetail(
            id=chat_session.id,
            created_at=chat_session.created_at,
            retention_expires_at=chat_session.retention_expires_at,
            messages=msg_out_list,
            summary=summary_text,
        )
    except DomainError as exc:
        return error_response(request, exc)


@router.delete("/chat/sessions/{session_id}", status_code=204, response_model=None)
async def delete_session(
    request: Request,
    session_id: uuid.UUID,
) -> JSONResponse | None:
    """Erase a chat session with mandatory audit evidence.

    T9-06: Audit is required for erasure operations. The request fails
    with 503 if the audit service is unavailable.
    """
    try:
        principal = _principal(request)
        sf = _session_factory(request)
        audit = _audit_service(request)

        # T9-06: Mandatory audit for erasure operations.
        if audit is None:
            raise DomainError(
                "Audit service is unavailable; erasure operations require audit evidence.",
                code="DEPENDENCY_UNAVAILABLE",
            )

        async with sf() as session, session.begin():
            stmt = select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.subject == principal.subject,
            )
            result = await session.execute(stmt)
            chat_session = result.scalar_one_or_none()
            if chat_session is None:
                raise ResourceNotFoundError("Chat session not found.", code="SESSION_NOT_FOUND")

            # Write audit BEFORE deletion (within transaction scope).
            await audit.write_event(
                AuditEntry(
                    actor_subject=principal.subject,
                    action="chat.session.erased",
                    resource_type="chat_session",
                    resource_id=str(session_id),
                    details={"subject": chat_session.subject},
                )
            )

            # Delete agent runs, then messages, then session.
            await session.execute(
                delete(AgentRun).where(AgentRun.session_id == session_id)
            )
            await session.execute(
                delete(ChatMessage).where(ChatMessage.session_id == session_id)
            )
            await session.delete(chat_session)

        return None
    except DomainError as exc:
        return error_response(request, exc)


@router.post("/chat/messages/{message_id}/feedback", status_code=200, response_model=None)
async def post_feedback(
    request: Request,
    message_id: uuid.UUID,
    body: FeedbackRequest,
) -> dict[str, str] | JSONResponse:
    """Submit feedback on an assistant message.

    Score: +1 (positive), -1 (negative).
    Comment: optional, max 1024 characters.
    """
    try:
        principal = _principal(request)
        sf = _session_factory(request)
        audit = _audit_service(request)

        async with sf() as session:
            # Verify the message exists and belongs to the caller's session.
            msg_stmt = select(ChatMessage).where(ChatMessage.id == message_id)
            msg_result = await session.execute(msg_stmt)
            message = msg_result.scalar_one_or_none()
            if message is None:
                raise ResourceNotFoundError("Message not found.", code="MESSAGE_NOT_FOUND")

            sess_stmt = select(ChatSession).where(
                ChatSession.id == message.session_id,
                ChatSession.subject == principal.subject,
            )
            sess_result = await session.execute(sess_stmt)
            if sess_result.scalar_one_or_none() is None:
                raise ResourceNotFoundError("Message not found.", code="MESSAGE_NOT_FOUND")

        if audit is not None:
            await audit.write_event(
                AuditEntry(
                    actor_subject=principal.subject,
                    action="chat.message.feedback",
                    resource_type="chat_message",
                    resource_id=str(message_id),
                    details={"score": body.score, "comment": body.comment},
                )
            )

        return {"status": "accepted"}
    except DomainError as exc:
        return error_response(request, exc)
