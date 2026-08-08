"""Test fixtures for ORM model tests using async PostgreSQL.

Schema setup uses Alembic migration (verified working) run once synchronously.
"""

import contextlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_TEST_DATABASE_URL = os.environ.get(
    "DOCUMIND_TEST_DATABASE_URL",
    "postgresql+asyncpg://documind:documind@localhost:5433/documind_test",
)

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
_ALEMBIC_BIN = os.path.join(_PROJECT_ROOT, ".venv", "bin", "alembic")


def _run_alembic_migration():
    """Run Alembic downgrade base + upgrade head once, synchronously."""
    env = {**os.environ, "ALEMBIC_DATABASE_URL": _TEST_DATABASE_URL}

    # Downgrade to base first (ignore errors if DB is already clean)
    subprocess.run(
        [_ALEMBIC_BIN, "downgrade", "base"],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        check=False,
    )

    # Upgrade to head
    result = subprocess.run(
        [_ALEMBIC_BIN, "upgrade", "head"],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Alembic upgrade STDERR:\n{result.stderr}", file=sys.stderr)
        raise RuntimeError(f"Alembic upgrade failed (rc={result.returncode})")


# Run migration once at import time
_run_alembic_migration()


@pytest_asyncio.fixture
async def async_session() -> AsyncIterator[AsyncSession]:
    """Yield a transactional session that rolls back after each test."""
    engine = create_async_engine(_TEST_DATABASE_URL, echo=False, pool_size=1)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        await session.begin()
        try:
            yield session
        finally:
            with contextlib.suppress(Exception):
                await session.rollback()
    with contextlib.suppress(Exception):
        await engine.dispose()
