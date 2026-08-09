"""Async SQLAlchemy engine, session factory, and FastAPI dependency.

The database URL is resolved at runtime from environment or Settings,
never hard-coded.  Call ``init_database()`` during application startup
before any code touches ``get_session_factory()`` or ``get_db()``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# Module-level state — populated by ``init_database()`` during startup.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# Fallback used *only* when ``Settings.debug`` is True and no resolved
# URL is available.  Production must always provide an explicit URL.
_DEV_FALLBACK_URL = "postgresql+asyncpg://documind:documind@localhost:5432/documind"


def _resolve_database_url() -> str:
    """Determine the database URL from environment / settings.

    Resolution order:
      1. ``DOCUMIND_RESOLVED_DATABASE_URL`` environment variable (set by
         the secret resolver after reading the OpenBao reference).
      2. ``Settings.database_url_ref`` when it contains a non-reference
         concrete URL (starts with ``postgresql``).
      3. Development fallback **only** when ``Settings.debug is True``.
    """
    # 1. Explicitly resolved env var (worker and production path).
    env_url = os.environ.get("DOCUMIND_RESOLVED_DATABASE_URL", "")
    if env_url:
        return env_url

    # 2. Settings field (may be a concrete URL set via .env).
    from documind.config import settings

    ref = settings.database_url_ref
    if ref and ref.startswith("postgresql"):
        return ref

    # 3. Dev / test fallback — warn when no explicit URL is configured.
    import logging

    logging.getLogger(__name__).warning(
        "No resolved database URL found; falling back to development default. "
        "Set DOCUMIND_RESOLVED_DATABASE_URL for production."
    )
    return _DEV_FALLBACK_URL


def build_engine(database_url: str) -> AsyncEngine:
    """Create an async engine from the resolved database URL."""
    return create_async_engine(database_url, echo=False)


def init_database(database_url: str | None = None) -> None:
    """Initialise the module-level engine and session factory.

    Must be called once during application startup.  If *database_url*
    is ``None``, ``_resolve_database_url()`` is used.
    """
    global _engine, _session_factory  # noqa: PLW0603

    url = database_url or _resolve_database_url()
    _engine = build_engine(url)
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


def get_engine() -> AsyncEngine:
    """Return the initialised engine, raising if not yet initialised."""
    if _engine is None:
        raise RuntimeError("Database not initialised — call init_database() first.")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the initialised session factory, raising if not yet initialised."""
    if _session_factory is None:
        raise RuntimeError("Database not initialised — call init_database() first.")
    return _session_factory


# Convenience alias preserved for backward compatibility with routes /
# tests that import ``AsyncSessionLocal`` — it now delegates lazily.
class _LazySessionFactory:
    """Proxy that defers to the real session factory after init."""

    def __call__(self) -> AsyncSession:
        return get_session_factory()()

    def __getattr__(self, name: str) -> object:
        return getattr(get_session_factory(), name)


AsyncSessionLocal = _LazySessionFactory()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields an async session and closes it after use."""
    factory = get_session_factory()
    async with factory() as session:
        yield session
