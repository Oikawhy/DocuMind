# ruff: noqa: N815 — SCIM 2.0 (RFC 7643/7644) mandates camelCase field names.
"""Pydantic schemas for identity and SCIM 2.0 API responses."""

from __future__ import annotations

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# /v1/identity/me response
# ---------------------------------------------------------------------------


class PrincipalResponse(BaseModel):
    """Response for ``GET /v1/identity/me``.

    Per §9.2 this returns subject, active state, effective role keys,
    policy revision, and allowed actions.  It never emits raw IdP
    groups not needed by the UI.
    """

    subject: str
    display_name: str
    email: str | None = None
    active: bool
    effective_roles: list[str] = Field(default_factory=list)
    allowed_labels: list[str] = Field(default_factory=list)
    policy_revision: str | None = None


# ---------------------------------------------------------------------------
# SCIM 2.0 schemas (RFC 7643 / 7644 subset)
# ---------------------------------------------------------------------------


class SCIMEmail(BaseModel):
    """SCIM email value."""

    value: str
    primary: bool = False


class SCIMGroupRef(BaseModel):
    """SCIM group reference in a User resource."""

    value: str
    display: str | None = None


class SCIMMeta(BaseModel):
    """SCIM resource metadata."""

    resourceType: str = "User"
    created: str | None = None
    lastModified: str | None = None


class SCIMUserResource(BaseModel):
    """SCIM 2.0 User resource (RFC 7643 §4.1 subset)."""

    schemas: list[str] = Field(default_factory=lambda: ["urn:ietf:params:scim:schemas:core:2.0:User"])
    id: str
    userName: str
    displayName: str
    emails: list[SCIMEmail] | None = None
    active: bool = True
    groups: list[SCIMGroupRef] | None = None
    meta: SCIMMeta | None = None


class SCIMListResponse(BaseModel):
    """SCIM 2.0 ListResponse (RFC 7644 §3.4.2)."""

    schemas: list[str] = Field(
        default_factory=lambda: ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
    )
    totalResults: int
    Resources: list[SCIMUserResource] = Field(default_factory=list)


class SCIMPatchOp(BaseModel):
    """A single SCIM PATCH operation."""

    op: str
    path: str | None = None
    value: object = None


class SCIMPatchRequest(BaseModel):
    """SCIM 2.0 PatchOp request body (RFC 7644 §3.5.2)."""

    schemas: list[str] = Field(
        default_factory=lambda: ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
    )
    Operations: list[SCIMPatchOp]


class SCIMErrorResponse(BaseModel):
    """SCIM 2.0 error response (RFC 7644 §3.12)."""

    schemas: list[str] = Field(
        default_factory=lambda: ["urn:ietf:params:scim:api:messages:2.0:Error"],
    )
    status: str
    detail: str | None = None
