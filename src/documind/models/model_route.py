"""ModelRouteRevision ORM model matching §8.1."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base, _enum_values
from documind.models.enums import PolicyStatus


class ModelRouteRevision(Base):
    """Role-based model route configuration revision."""

    __tablename__ = "model_route_revision"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PolicyStatus] = mapped_column(
        Enum(PolicyStatus, name="policy_status", create_type=False, values_callable=_enum_values),
        nullable=False,
    )
    route_configuration: Mapped[dict] = mapped_column(JSONB, nullable=False)
    external_consent_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("policy_revision.id"),
        nullable=True,
    )
    secret_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "role IN ('KEYWORDS', 'EXTRACT', 'QUERY', 'VLM')",
            name="valid_model_role",
        ),
        UniqueConstraint("role", "revision", name="uq_model_route_revision"),
    )
