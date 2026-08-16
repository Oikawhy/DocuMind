"""Webhook event dispatcher — matches outbox events to webhook subscriptions.

Consumes outbox CloudEvent envelopes, matches them against active webhook
``events`` lists, resolves delivery secrets from
OpenBao, and invokes ``WebhookService.deliver()`` for each match.
"""

from __future__ import annotations

import fnmatch
import json
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.models.outbox import OutboxEvent
from documind.models.webhook import Webhook
from documind.services.webhook_service import WebhookService

logger = structlog.get_logger()


class WebhookDispatcher:
    """Match outbox events to webhook subscriptions and dispatch deliveries."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        webhook_service: WebhookService,
        secret_service: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._webhook_service = webhook_service
        self._secret_service = secret_service

    async def dispatch_event(self, outbox_event: OutboxEvent) -> int:
        """Dispatch a single outbox event to matching webhooks.

        Returns the number of deliveries attempted.
        """
        event_type = outbox_event.event_type

        # Find active webhooks whose glob matches this event type.
        async with self._session_factory() as session:
            stmt = select(Webhook).where(Webhook.active == True)  # noqa: E712
            result = await session.execute(stmt)
            all_webhooks = list(result.scalars().all())

        matching = [
            w for w in all_webhooks if event_type in (w.events or [])
        ]

        if not matching:
            return 0

        # Serialize the CloudEvent body.
        body = json.dumps(
            outbox_event.cloud_event, sort_keys=True, default=str
        ).encode()

        delivered = 0
        for webhook in matching:
            # Resolve secret from OpenBao.
            secret: str | None = None
            if (
                getattr(webhook, "secret_reference", None)
                and self._secret_service is not None
            ):
                try:
                    parts = webhook.secret_reference.rsplit("/", 1)
                    if len(parts) == 2:
                        secret = await self._secret_service.get_secret(
                            parts[0], parts[1]
                        )
                except Exception:
                    await logger.awarning(
                        "webhook_secret_resolution_failed",
                        webhook_id=str(webhook.id),
                    )
                    continue

            if secret is None:
                await logger.awarning(
                    "webhook_no_secret_available",
                    webhook_id=str(webhook.id),
                )
                continue

            try:
                await self._webhook_service.deliver(
                    webhook=webhook,
                    outbox_event_id=outbox_event.id,
                    body=body,
                    secret=secret,
                )
                delivered += 1
            except Exception:
                await logger.aexception(
                    "webhook_dispatch_delivery_failed",
                    webhook_id=str(webhook.id),
                    outbox_event_id=str(outbox_event.id),
                )

        return delivered

    async def process_pending_events(self, *, limit: int = 50) -> int:
        """Process a batch of pending outbox events.

        Returns the total number of deliveries attempted.
        """
        total = 0
        async with self._session_factory() as session:
            from documind.services.outbox_service import OutboxService

            outbox = OutboxService(session)
            events = await outbox.claim_pending(limit=limit)

        for event in events:
            total += await self.dispatch_event(event)

        return total
