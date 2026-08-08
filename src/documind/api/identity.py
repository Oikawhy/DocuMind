"""``GET /v1/identity/me`` — authenticated caller identity per §9.2.

Returns subject, active state, effective role keys, policy revision,
and allowed actions.  Never emits raw IdP groups not needed by the UI.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from documind.schemas.identity import PrincipalResponse

router = APIRouter(prefix="/v1/identity", tags=["identity"])


@router.get("/me", response_model=None)
async def get_me(request: Request) -> PrincipalResponse | JSONResponse:
    """Return the authenticated caller's identity and effective permissions."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": "AUTHENTICATION_REQUIRED",
                    "message": "No authenticated principal.",
                    "trace_id": None,
                    "details": [],
                }
            },
        )

    # Resolve effective roles and allowed labels via the policy service.
    policy_service = request.app.state.policy_service
    role_mappings = await policy_service.get_role_mappings(principal.groups)

    effective_roles = sorted({rm.role_key for rm in role_mappings})
    allowed_labels = sorted({str(lid) for rm in role_mappings for lid in rm.allowed_label_ids})

    # Find the latest authorization policy revision for reporting.
    policy_revision: str | None = None
    auth_policy = await policy_service.get_active_policy("authorization", "default")
    if auth_policy is not None:
        policy_revision = f"{auth_policy.stable_key}:r{auth_policy.revision}"

    return PrincipalResponse(
        subject=principal.subject,
        display_name=principal.display_name,
        email=principal.email,
        active=principal.active,
        effective_roles=effective_roles,
        allowed_labels=allowed_labels,
        policy_revision=policy_revision,
    )
