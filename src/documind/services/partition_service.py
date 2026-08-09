"""Ongoing audit partition creation and health monitoring per §8.2.

The installation job creates the next three audit partitions and alerts
when fewer than two future partitions exist.  This service is called at
application startup and can be scheduled periodically.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger()


@dataclass(frozen=True)
class PartitionHealth:
    """Result of a partition health check."""

    existing_future_partitions: int
    created_partitions: list[str]
    alert: bool  # True when fewer than 2 future partitions exist


async def ensure_audit_partitions(
    engine: AsyncEngine,
    *,
    months_ahead: int = 3,
) -> PartitionHealth:
    """Create monthly ``audit_event`` partitions up to *months_ahead*.

    Returns a ``PartitionHealth`` indicating how many future partitions
    exist and whether an alert should be raised.
    """
    now = datetime.datetime.now(datetime.UTC)
    created: list[str] = []

    async with engine.begin() as conn:
        for offset in range(months_ahead):
            month = now.month + offset
            year = now.year + (month - 1) // 12
            month = (month - 1) % 12 + 1

            next_month = month + 1
            next_year = year + (next_month - 1) // 12
            next_month = (next_month - 1) % 12 + 1

            partition_name = f"audit_event_y{year}m{month:02d}"

            # Check if partition already exists.
            result = await conn.execute(
                text(
                    "SELECT 1 FROM pg_class WHERE relname = :name AND relkind = 'r'"
                ),
                {"name": partition_name},
            )
            if result.scalar_one_or_none() is not None:
                continue

            # Create the partition.
            await conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {partition_name} "
                    f"PARTITION OF audit_event "
                    f"FOR VALUES FROM ('{year}-{month:02d}-01') "
                    f"TO ('{next_year}-{next_month:02d}-01')"
                )
            )
            created.append(partition_name)
            await logger.ainfo(
                "audit_partition_created",
                partition=partition_name,
            )

        # Count how many future partitions exist (months > current month).
        future_count = await _count_future_partitions(conn, now)

    alert = future_count < 2
    if alert:
        await logger.awarning(
            "audit_partition_alert",
            future_partitions=future_count,
            message="Fewer than 2 future audit partitions exist.",
        )

    return PartitionHealth(
        existing_future_partitions=future_count,
        created_partitions=created,
        alert=alert,
    )


async def _count_future_partitions(conn: object, now: datetime.datetime) -> int:
    """Count audit_event child partitions covering future months."""
    # List all child tables of audit_event.
    result = await conn.execute(  # type: ignore[union-attr]
        text(
            "SELECT c.relname "
            "FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE p.relname = 'audit_event'"
        )
    )
    partitions = [row[0] for row in result.fetchall()]

    # Parse partition names like audit_event_y2026m08 to check if they
    # cover a month strictly after the current one.
    future_count = 0
    current_ym = (now.year, now.month)
    for name in partitions:
        # Expected format: audit_event_y<YYYY>m<MM>
        try:
            parts = name.replace("audit_event_y", "").split("m")
            p_year, p_month = int(parts[0]), int(parts[1])
            if (p_year, p_month) > current_ym:
                future_count += 1
        except (ValueError, IndexError):
            continue

    return future_count
