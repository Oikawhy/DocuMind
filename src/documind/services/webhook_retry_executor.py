"""Webhook retry executor per §9.5.

T9-12: Implements an asyncio-based retry loop that re-delivers failed
webhook deliveries according to the backoff schedule defined in
``WebhookService``.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog

from documind.services.webhook_service import WebhookService

logger = structlog.get_logger()


async def execute_with_retries(
    service: WebhookService,
    webhook_id: uuid.UUID,
    outbox_event_id: uuid.UUID,
    body: bytes,
    secret: str,
) -> str:
    """Deliver a webhook event with retries per the defined schedule.

    Returns the final delivery state: ``'delivered'``, ``'failed'``, or
    ``'exhausted'``.
    """
    attempt = 1
    while True:
        result = await service.deliver_webhook(
            webhook_id=webhook_id,
            outbox_event_id=outbox_event_id,
            body=body,
            secret=secret,
            attempt=attempt,
        )

        if result.state == "delivered":
            await logger.ainfo(
                "webhook_retry_succeeded",
                webhook_id=str(webhook_id),
                attempt=attempt,
            )
            return "delivered"

        if result.state == "exhausted":
            await logger.awarning(
                "webhook_retry_exhausted",
                webhook_id=str(webhook_id),
                final_attempt=attempt,
            )
            return "exhausted"

        # Compute delay for next attempt.
        delay = service.get_retry_delay(attempt + 1)
        if delay is None:
            await logger.awarning(
                "webhook_retry_no_more_attempts",
                webhook_id=str(webhook_id),
                final_attempt=attempt,
            )
            return "exhausted"

        await logger.ainfo(
            "webhook_retry_scheduled",
            webhook_id=str(webhook_id),
            attempt=attempt,
            next_delay_s=delay,
        )
        await asyncio.sleep(delay)
        attempt += 1
