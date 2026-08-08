"""IdentitySubject and IdentityGroupMembership ORM models matching §8.1."""

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from documind.models.base import Base


class IdentitySubject(Base):
    """SCIM-synced or manually-managed identity record."""

    __tablename__ = "identity_subject"

    subject: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    scim_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))


class IdentityGroupMembership(Base):
    """Group membership for identity subjects."""

    __tablename__ = "identity_group_membership"

    subject: Mapped[str] = mapped_column(
        ForeignKey("identity_subject.subject"),
        primary_key=True,
    )
    group_key: Mapped[str] = mapped_column(Text, primary_key=True)
    assigned_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
