"""Temporal client construction from the application's namespace configuration."""

from __future__ import annotations

from temporalio.client import Client

from documind.config import Settings


async def get_temporal_client(settings: Settings | None = None) -> Client:
    """Connect to Temporal with the configured namespace, never the SDK default."""
    resolved = settings or Settings()
    return await Client.connect(resolved.temporal_host, namespace=resolved.temporal_namespace)


create_temporal_client = get_temporal_client
