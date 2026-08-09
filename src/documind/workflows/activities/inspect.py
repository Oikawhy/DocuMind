"""Temporal inspection activity with idempotency and tombstone guard hooks."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any, Protocol

from temporalio import activity

from documind.services.scanner_service import ScannerService
from documind.workflows.document_version import StageExecution, StageOutput, StageReplayStore


class TombstoneGuard(Protocol):
    """Check authoritative lifecycle state before an activity writes output."""

    async def assert_active(self, version_id: str) -> None:
        """Raise when the version was tombstoned or otherwise stopped."""


_scanner_service: ScannerService | None = None
_tombstone_guard: TombstoneGuard | None = None
_stage_store: StageReplayStore | None = None


def configure_inspection_activity(
    scanner_service: ScannerService,
    *,
    tombstone_guard: TombstoneGuard | None = None,
    stage_store: StageReplayStore,
) -> None:
    """Inject worker-owned dependencies; no client or credentials live in a workflow."""
    global _scanner_service, _stage_store, _tombstone_guard
    _scanner_service = scanner_service
    _tombstone_guard = tombstone_guard
    _stage_store = stage_store


@activity.defn(name="inspect")
async def inspect(stage: StageExecution) -> dict[str, Any]:
    """Inspect bytes once per idempotency key and expose a safe result only."""
    scanner = _scanner_service
    if scanner is None:
        raise RuntimeError("Inspection activity has not been configured.")
    await _assert_active(stage)

    async def execute() -> dict[str, Any]:
        result = await scanner.inspect(__import__("uuid").UUID(stage.version_id))
        return asdict(result)

    async with _heartbeat_loop(stage):
        output = await _run_stage(stage, execute, max_attempts=3)
    await _assert_active(stage)
    return _with_stage_checksum(output)


async def _run_stage(stage: StageExecution, execute: Any, *, max_attempts: int) -> StageOutput:
    if _stage_store is None:
        raise RuntimeError("Inspection activity requires a durable stage store.")
    return await _stage_store.run(stage, execute, max_attempts=max_attempts)


async def _assert_active(stage: StageExecution) -> None:
    if _tombstone_guard is not None:
        await _tombstone_guard.assert_active(stage.version_id)


def _with_stage_checksum(output: StageOutput) -> dict[str, Any]:
    payload = dict(output.output)
    payload["stage_output_sha256"] = output.output_sha256
    return payload


@asynccontextmanager
async def _heartbeat_loop(stage: StageExecution, interval: float = 10.0) -> AsyncIterator[None]:
    """Emit heartbeats every *interval* seconds until the wrapped work completes.

    The 10-second default gives a 3× safety margin on the 30-second
    heartbeat timeout configured by `StageConfiguration`.
    """
    async def _beat() -> None:
        while True:
            await asyncio.sleep(interval)
            activity.heartbeat({"stage": stage.name, "version_id": stage.version_id})

    task = asyncio.create_task(_beat())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
