"""Document version reindex endpoint per §9.3."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from documind.domain.errors import (
    AuthenticationError,
    DomainError,
    PolicyUnavailableError,
)
from documind.schemas.common import error_response

router = APIRouter(prefix="/v1/document-versions", tags=["versions"])


def _principal(request: Request) -> object:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise AuthenticationError()
    return principal


@router.post("/{version_id}/reindex", status_code=202, response_model=None)
async def reindex_version(
    request: Request,
    version_id: uuid.UUID,
) -> dict[str, str] | JSONResponse:
    """Trigger a new projection revision for a document version.

    T6-21: Starts a ``RebuildProjectionWorkflow`` via Temporal and
    returns an asynchronous operation reference.
    """
    try:
        _principal(request)

        temporal_client: Any = getattr(request.app.state, "temporal_client", None)
        if temporal_client is None:
            raise PolicyUnavailableError("Temporal client is unavailable for reindex.")

        from documind.workflows.document_version import REBUILD_QUEUE
        from documind.workflows.maintenance.rebuild_projections import (
            RebuildProjectionInput,
            RebuildProjectionWorkflow,
        )

        # Start rebuild workflows for all three backends scoped to this version
        workflow_id = f"reindex-{version_id}"
        await temporal_client.start_workflow(
            RebuildProjectionWorkflow.run,
            RebuildProjectionInput(
                backend="qdrant",
                scope="version",
                scope_id=str(version_id),
                reason="reindex",
                requested_by="api",
            ),
            id=f"{workflow_id}-qdrant",
            task_queue=REBUILD_QUEUE,
        )

        return {
            "version_id": str(version_id),
            "status": "accepted",
            "workflow_id": workflow_id,
            "status_url": f"/v1/operations/{workflow_id}",
        }
    except DomainError as exc:
        return error_response(request, exc)
