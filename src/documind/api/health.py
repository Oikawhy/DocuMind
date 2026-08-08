"""Unauthenticated operational health endpoint."""

from fastapi import APIRouter
from pydantic import BaseModel

from documind.config import settings

router = APIRouter(tags=["health"])


class HealthStatus(BaseModel):
    """Initial installation status exposed to operators."""

    service: str
    migration_level: str
    release_digest: str


@router.get("/health", response_model=HealthStatus)
async def health() -> HealthStatus:
    """Return static bootstrap state until migration and manifest services exist."""
    return HealthStatus(
        service=settings.app_name,
        migration_level=settings.migration_level,
        release_digest=settings.release_digest,
    )
