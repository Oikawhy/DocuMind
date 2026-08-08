"""HTTP contracts for Task 3 document admission and operation polling."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from documind.domain.document_service import AdmissionResult
from documind.domain.errors import ResourceNotFoundError
from documind.main import app
from documind.services.identity_service import Principal


def _principal() -> Principal:
    return Principal(
        subject="writer@example.test",
        display_name="Writer",
        email=None,
        groups=["editors"],
        active=True,
        issuer="https://issuer.example.test",
    )


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


async def test_post_documents_returns_accepted_contract_and_forwards_multipart_source(
    client: AsyncClient,
) -> None:
    document_id, version_id, operation_id, label_id = (uuid.uuid4() for _ in range(4))
    service = AsyncMock()
    service.admit_document.return_value = AdmissionResult(document_id, version_id, operation_id)
    identity = AsyncMock()
    identity.validate_oidc_token.return_value = _principal()
    original_service = app.state.document_service
    original_identity = app.state.identity_service
    try:
        app.state.document_service = service
        app.state.identity_service = identity
        response = await client.post(
            "/v1/documents",
            headers={"Authorization": "Bearer test", "Idempotency-Key": "a" * 32},
            data={"title": "Quarterly report", "declared_type": "report", "labels": str(label_id)},
            files={"file": ("report.txt", b"document", "text/plain")},
        )
    finally:
        app.state.document_service = original_service
        app.state.identity_service = original_identity

    assert response.status_code == 202
    assert response.json() == {
        "document_id": str(document_id),
        "version_id": str(version_id),
        "operation_id": str(operation_id),
        "lifecycle_state": "accepted",
        "status_url": f"/v1/operations/{operation_id}",
    }
    call = service.admit_document.await_args.kwargs
    assert call["file"].filename == "report.txt"
    assert call["file"].content_type == "text/plain"
    assert call["labels"] == [label_id]


async def test_get_document_returns_safe_404_for_inaccessible_resource(client: AsyncClient) -> None:
    service = AsyncMock()
    service.get_document.side_effect = ResourceNotFoundError()
    identity = AsyncMock()
    identity.validate_oidc_token.return_value = _principal()
    original_service = app.state.document_service
    original_identity = app.state.identity_service
    try:
        app.state.document_service = service
        app.state.identity_service = identity
        response = await client.get(
            f"/v1/documents/{uuid.uuid4()}",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.state.document_service = original_service
        app.state.identity_service = original_identity

    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "DOCUMENT_NOT_FOUND"
    assert uuid.UUID(body["trace_id"])


async def test_missing_idempotency_key_is_rejected_by_fastapi(client: AsyncClient) -> None:
    identity = AsyncMock()
    identity.validate_oidc_token.return_value = _principal()
    original_identity = app.state.identity_service
    try:
        app.state.identity_service = identity
        response = await client.post(
            "/v1/documents",
            headers={"Authorization": "Bearer test"},
            data={"title": "Quarterly report", "declared_type": "report", "labels": str(uuid.uuid4())},
            files={"file": ("report.txt", b"document", "text/plain")},
        )
    finally:
        app.state.identity_service = original_identity

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


async def test_operation_polling_returns_safe_stage_status(client: AsyncClient) -> None:
    operation_id, document_id, version_id, trace_id = (uuid.uuid4() for _ in range(4))
    service = AsyncMock()
    service.get_operation.return_value = (
        SimpleNamespace(
            id=operation_id,
            operation_type="document_version_processing",
            status="accepted",
            document_id=document_id,
            version_id=version_id,
            safe_error_code=None,
        ),
        [
            SimpleNamespace(
                stage_name="admit",
                status="succeeded",
                trace_id=trace_id,
                started_at=datetime.now(UTC),
                ended_at=datetime.now(UTC),
                safe_error_code=None,
            )
        ],
    )
    identity = AsyncMock()
    identity.validate_oidc_token.return_value = _principal()
    original_service = app.state.document_service
    original_identity = app.state.identity_service
    try:
        app.state.document_service = service
        app.state.identity_service = identity
        response = await client.get(
            f"/v1/operations/{operation_id}",
            headers={"Authorization": "Bearer test"},
        )
    finally:
        app.state.document_service = original_service
        app.state.identity_service = original_identity

    assert response.status_code == 200
    assert response.json()["stages"] == [
        {
            "name": "admit",
            "status": "succeeded",
            "trace_id": str(trace_id),
            "started_at": response.json()["stages"][0]["started_at"],
            "ended_at": response.json()["stages"][0]["ended_at"],
            "safe_error_code": None,
        }
    ]
