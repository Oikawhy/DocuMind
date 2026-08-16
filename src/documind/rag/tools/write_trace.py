"""write_trace — content-free audit trace tool per §7.6.

Writes a content-free run event to the audit service and returns
an auditable trace event ID.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel

SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Versioned input / output schemas
# ---------------------------------------------------------------------------


class WriteTraceInput(BaseModel):
    """Input schema for write_trace tool."""

    event_type: str
    principal_subject: str
    trace_id: str
    metadata: dict[str, Any] = {}
    schema_version: str = SCHEMA_VERSION


class WriteTraceOutput(BaseModel):
    """Output schema for write_trace tool."""

    event_id: str
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


async def write_trace(
    input_data: WriteTraceInput,
    audit_service: Any,
) -> WriteTraceOutput:
    """Write a content-free trace event to the audit service.

    Returns an auditable event ID.  The event contains no document content,
    prompts, or model outputs — only metadata identifiers.
    """
    from documind.services.audit_service import AuditEntry

    event_id = str(uuid.uuid4())

    entry = AuditEntry(
        actor_subject=input_data.principal_subject,
        action=f"rag.trace.{input_data.event_type}",
        resource_type="agent_run",
        resource_id=event_id,
        details={
            "event_type": input_data.event_type,
            **{k: v for k, v in input_data.metadata.items() if not isinstance(v, (bytes, memoryview))},
        },
        trace_id=uuid.UUID(input_data.trace_id) if input_data.trace_id else None,
    )

    await audit_service.write_event(entry)

    return WriteTraceOutput(event_id=event_id)
