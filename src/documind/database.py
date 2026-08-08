"""Async SQLAlchemy engine, session factory, and FastAPI dependency."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# Default PostgreSQL URL for local development.
# Production resolves the OpenBao database_url_ref before engine creation.
_DEFAULT_DATABASE_URL = "postgresql+asyncpg://documind:documind@localhost:5432/documind"


def build_engine(database_url: str = _DEFAULT_DATABASE_URL) -> AsyncEngine:
    """Create an async engine from the resolved database URL."""
    return create_async_engine(database_url, echo=False)


async_engine = build_engine()

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields an async session and closes it after use."""
    async with AsyncSessionLocal() as session:
        yield session
