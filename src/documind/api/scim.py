# ruff: noqa: B008 — FastAPI Depends() in argument defaults is the standard DI pattern.
"""SCIM 2.0 identity provisioning endpoints per §4.1 / §9.2.

These endpoints receive lifecycle events from the customer's SCIM
provisioner (e.g. Entra ID, Okta) and project them into the local
``identity_subject`` / ``identity_group_membership`` tables.

SCIM endpoints use their own bearer-token authentication — not the
OIDC middleware — because the caller is the IdP provisioner, not a
human user.  IP/mTLS restriction is handled at the Traefik layer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from documind.config import settings
from documind.database import get_db
from documind.models.identity import IdentityGroupMembership, IdentitySubject
from documind.schemas.identity import (
    SCIMEmail,
    SCIMErrorResponse,
    SCIMGroupRef,
    SCIMListResponse,
    SCIMMeta,
    SCIMPatchRequest,
    SCIMUserResource,
)

router = APIRouter(prefix="/scim/v2", tags=["scim"])


# ------------------------------------------------------------------
# SCIM bearer-token guard
# ------------------------------------------------------------------


async def _verify_scim_token(authorization: str = Header(...)) -> None:
    """Validate the SCIM client bearer token.

    The token is a static secret configured via
    ``DOCUMIND_SCIM_BEARER_TOKEN``.  Production deployments add
    IP/mTLS restrictions at the ingress layer.
    """
    expected = getattr(settings, "scim_bearer_token", "")
    if not expected:
        raise HTTPException(status_code=401, detail="SCIM provisioning is not configured.")

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != expected:
        raise HTTPException(status_code=401, detail="Invalid SCIM bearer token.")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _to_scim_resource(identity: IdentitySubject, groups: list[str]) -> SCIMUserResource:
    """Map a local identity record to a SCIM User resource."""
    emails = [SCIMEmail(value=identity.email, primary=True)] if identity.email else None
    group_refs = [SCIMGroupRef(value=g) for g in groups] if groups else None
    return SCIMUserResource(
        id=identity.subject,
        userName=identity.subject,
        displayName=identity.display_name,
        emails=emails,
        active=identity.active,
        groups=group_refs,
        meta=SCIMMeta(
            created=identity.created_at.isoformat() if identity.created_at else None,
            lastModified=identity.updated_at.isoformat() if identity.updated_at else None,
        ),
    )


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------


@router.post("/Users", status_code=201, response_model=None, dependencies=[Depends(_verify_scim_token)])
async def create_user(request: Request, db: AsyncSession = Depends(get_db)) -> SCIMUserResource | JSONResponse:
    """Create a new identity subject from a SCIM provisioning event."""
    body = await request.json()
    identity_service = request.app.state.identity_service

    subject = body.get("userName", "")
    display_name = body.get("displayName", subject)
    emails = body.get("emails", [])
    email = emails[0]["value"] if emails else None
    raw_groups = body.get("groups", [])
    groups = [g.get("value", g.get("display", "")) for g in raw_groups if isinstance(g, dict)]

    await identity_service.process_scim_user_create(
        subject=subject,
        display_name=display_name,
        email=email,
        groups=groups,
    )

    # Reload to return the created resource.
    identity = await db.get(IdentitySubject, subject)
    if identity is None:
        return JSONResponse(
            status_code=500,
            content=SCIMErrorResponse(status="500", detail="Failed to create user.").model_dump(),
        )

    return _to_scim_resource(identity, groups)


@router.get("/Users", dependencies=[Depends(_verify_scim_token)])
async def list_users(db: AsyncSession = Depends(get_db)) -> SCIMListResponse:
    """List all identity subjects as SCIM User resources."""
    stmt = select(IdentitySubject)
    result = await db.execute(stmt)
    identities = result.scalars().all()

    resources: list[SCIMUserResource] = []
    for identity in identities:
        grp_stmt = select(IdentityGroupMembership.group_key).where(
            IdentityGroupMembership.subject == identity.subject,
        )
        grp_result = await db.execute(grp_stmt)
        groups = list(grp_result.scalars().all())
        resources.append(_to_scim_resource(identity, groups))

    return SCIMListResponse(totalResults=len(resources), Resources=resources)


@router.get("/Users/{user_id}", response_model=None, dependencies=[Depends(_verify_scim_token)])
async def get_user(user_id: str, db: AsyncSession = Depends(get_db)) -> SCIMUserResource | JSONResponse:
    """Get a single identity subject as a SCIM User resource."""
    identity = await db.get(IdentitySubject, user_id)
    if identity is None:
        return JSONResponse(
            status_code=404,
            content=SCIMErrorResponse(status="404", detail="User not found.").model_dump(),
        )

    grp_stmt = select(IdentityGroupMembership.group_key).where(
        IdentityGroupMembership.subject == user_id,
    )
    grp_result = await db.execute(grp_stmt)
    groups = list(grp_result.scalars().all())
    return _to_scim_resource(identity, groups)


@router.patch("/Users/{user_id}", response_model=None, dependencies=[Depends(_verify_scim_token)])
async def patch_user(
    user_id: str,
    patch: SCIMPatchRequest,
    request: Request,
) -> SCIMUserResource | JSONResponse:
    """Apply SCIM PATCH operations to an identity subject."""
    identity_service = request.app.state.identity_service

    active: bool | None = None
    display_name: str | None = None
    groups: list[str] | None = None

    for op in patch.Operations:
        if op.op.lower() == "replace":
            if op.path == "active" or (op.path is None and isinstance(op.value, dict) and "active" in op.value):
                val = op.value if op.path == "active" else op.value["active"]  # type: ignore[index]
                active = bool(val)
            if op.path == "displayName":
                display_name = str(op.value)
        elif op.op.lower() == "add" and op.path == "groups":
            if isinstance(op.value, list):
                groups = [g.get("value", "") for g in op.value if isinstance(g, dict)]

    await identity_service.process_scim_user_update(
        user_id,
        active=active,
        display_name=display_name,
        groups=groups,
    )

    # Reload and return.
    async with request.app.state.session_factory() as session:
        identity = await session.get(IdentitySubject, user_id)
        if identity is None:
            return JSONResponse(
                status_code=404,
                content=SCIMErrorResponse(status="404", detail="User not found.").model_dump(),
            )
        grp_stmt = select(IdentityGroupMembership.group_key).where(
            IdentityGroupMembership.subject == user_id,
        )
        grp_result = await session.execute(grp_stmt)
        user_groups = list(grp_result.scalars().all())
        return _to_scim_resource(identity, user_groups)


@router.delete("/Users/{user_id}", status_code=204, dependencies=[Depends(_verify_scim_token)])
async def delete_user(user_id: str, request: Request) -> None:
    """Deactivate an identity subject (SCIM DELETE → soft deactivation)."""
    identity_service = request.app.state.identity_service
    await identity_service.process_scim_user_deactivate(user_id)
