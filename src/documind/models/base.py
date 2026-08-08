"""SQLAlchemy declarative base and shared model utilities."""

from datetime import datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase

# PostgreSQL naming convention for constraints and indexes.
# This ensures Alembic auto-generates deterministic names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _enum_values(enum_cls: type) -> list[str]:
    """Extract .value strings from a StrEnum for SQLAlchemy Enum(values_callable=...)."""
    return [v.value for v in enum_cls]


class Base(DeclarativeBase):
    """Application-wide declarative base."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Map Python datetime → TIMESTAMPTZ (not TIMESTAMP WITHOUT TIME ZONE).
    type_annotation_map = {
        datetime: DateTime(timezone=True),
    }
