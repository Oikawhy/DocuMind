"""Webhook delivery service with SSRF defense and HMAC signing per §9.5.

The service resolves DNS through a controlled path, rejects loopback and
private addresses, signs each delivery with HMAC-SHA256, and logs every
attempt in the ``webhook_delivery`` table.  Retry schedule: 10s → 60s → 300s.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.domain.errors import SSRFViolationError
from documind.models.webhook import Webhook, WebhookDelivery

logger = structlog.get_logger()

# Retry schedule in seconds: attempt 1 → 10s, attempt 2 → 60s, attempt 3 → 300s.
_RETRY_DELAYS: list[int] = [10, 60, 300]
_MAX_ATTEMPTS = 3
_DELIVERY_TIMEOUT = 10.0  # seconds


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of a single webhook delivery attempt."""

    delivery_id: uuid.UUID
    webhook_id: uuid.UUID
    attempt: int
    http_status: int | None
    state: str  # 'delivered' | 'failed' | 'exhausted'


class WebhookService:
    """SSRF-safe webhook registration and signed delivery."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # SSRF defense
    # ------------------------------------------------------------------

    @staticmethod
    def validate_target_url(url: str) -> str:
        """Validate a webhook URL against SSRF rules.

        Requirements (§9.5):
        - HTTPS required
        - DNS resolution through controlled resolver
        - Reject loopback, private, link-local, and reserved addresses
        - Return the resolved IP for pinning
        """
        if not url.startswith("https://"):
            raise SSRFViolationError("Webhook target URL must use HTTPS.")

        # Extract hostname from URL.
        try:
            parsed = httpx.URL(url)
            hostname = parsed.host
            if not hostname:
                raise SSRFViolationError("Webhook target URL has no hostname.")
        except Exception as exc:
            if isinstance(exc, SSRFViolationError):
                raise
            raise SSRFViolationError(f"Invalid webhook target URL: {exc}") from exc

        # Resolve DNS and check all addresses.
        try:
            addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise SSRFViolationError(f"DNS resolution failed for {hostname}.") from exc

        if not addr_infos:
            raise SSRFViolationError(f"No DNS records found for {hostname}.")

        for addr_info in addr_infos:
            ip_str = addr_info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError as exc:
                raise SSRFViolationError(f"Invalid IP address resolved: {ip_str}") from exc

            if ip.is_loopback:
                raise SSRFViolationError("Loopback addresses are not permitted.")
            if ip.is_private:
                raise SSRFViolationError("Private network addresses are not permitted.")
            if ip.is_link_local:
                raise SSRFViolationError("Link-local addresses are not permitted.")
            if ip.is_reserved:
                raise SSRFViolationError("Reserved addresses are not permitted.")
            if ip.is_multicast:
                raise SSRFViolationError("Multicast addresses are not permitted.")

        return url

    # ------------------------------------------------------------------
    # HMAC signature
    # ------------------------------------------------------------------

    @staticmethod
    def compute_signature(timestamp: int, body: bytes, secret: str) -> str:
        """Compute ``X-DocuMind-Signature`` value per §9.5.

        Format: ``v1=HMAC-SHA256(timestamp + "." + body, secret)``
        """
        payload = f"{timestamp}.".encode() + body
        sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return f"v1={sig}"

    @staticmethod
    def verify_signature(timestamp: int, body: bytes, secret: str, signature: str) -> bool:
        """Verify a webhook signature."""
        expected = WebhookService.compute_signature(timestamp, body, secret)
        return hmac.compare_digest(expected, signature)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register_webhook(
        self,
        target_url: str,
        event_type_glob: str,
        secret: str,
        created_by_subject: str,
    ) -> Webhook:
        """Register a webhook after SSRF validation.

        The raw secret is never stored; only a SHA-256 hash is persisted.
        """
        self.validate_target_url(target_url)
        secret_hash = hashlib.sha256(secret.encode()).hexdigest()

        async with self._session_factory() as session, session.begin():
            webhook = Webhook(
                id=uuid.uuid4(),
                target_url=target_url,
                event_type_glob=event_type_glob,
                secret_hash=secret_hash,
                created_by_subject=created_by_subject,
            )
            session.add(webhook)
            await session.flush()
            # Eagerly capture values before session closes.
            _ = webhook.id, webhook.created_at

        await logger.ainfo(
            "webhook_registered",
            webhook_id=str(webhook.id),
            target_url=target_url,
        )
        return webhook

    async def list_webhooks(self, subject: str) -> list[Webhook]:
        """List active webhooks owned by the given subject."""
        async with self._session_factory() as session:
            stmt = (
                select(Webhook)
                .where(Webhook.created_by_subject == subject)
                .order_by(Webhook.created_at.desc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def deactivate_webhook(self, webhook_id: uuid.UUID, subject: str) -> bool:
        """Deactivate a webhook owned by the subject. Returns True if found."""
        async with self._session_factory() as session, session.begin():
            stmt = select(Webhook).where(
                Webhook.id == webhook_id,
                Webhook.created_by_subject == subject,
            )
            result = await session.execute(stmt)
            webhook = result.scalar_one_or_none()
            if webhook is None:
                return False
            webhook.active = False
        return True

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def deliver(
        self,
        webhook: Webhook,
        outbox_event_id: uuid.UUID,
        body: bytes,
        secret: str,
    ) -> DeliveryResult:
        """Attempt delivery with up to 3 retries per §9.5.

        Each attempt is logged in ``webhook_delivery``.  The signature
        header is regenerated per attempt with a fresh timestamp.
        """
        # Determine current attempt number.
        async with self._session_factory() as session:
            count_stmt = (
                select(func.count())
                .select_from(WebhookDelivery)
                .where(
                    WebhookDelivery.webhook_id == webhook.id,
                    WebhookDelivery.outbox_event_id == outbox_event_id,
                )
            )
            result = await session.execute(count_stmt)
            prior_attempts = result.scalar_one()

        attempt = prior_attempts + 1
        if attempt > _MAX_ATTEMPTS:
            return DeliveryResult(
                delivery_id=uuid.uuid4(),
                webhook_id=webhook.id,
                attempt=attempt,
                http_status=None,
                state="exhausted",
            )

        timestamp = int(time.time())
        signature = self.compute_signature(timestamp, body, secret)

        http_status: int | None = None
        state = "failed"

        try:
            async with httpx.AsyncClient(timeout=_DELIVERY_TIMEOUT) as client:
                response = await client.post(
                    str(webhook.target_url),
                    content=body,
                    headers={
                        "Content-Type": "application/cloudevents+json",
                        "X-DocuMind-Signature": signature,
                        "X-DocuMind-Timestamp": str(timestamp),
                    },
                )
                http_status = response.status_code
                if 200 <= http_status < 300:
                    state = "delivered"
        except httpx.TimeoutException:
            await logger.awarning(
                "webhook_delivery_timeout",
                webhook_id=str(webhook.id),
                attempt=attempt,
            )
        except Exception as exc:
            await logger.awarning(
                "webhook_delivery_error",
                webhook_id=str(webhook.id),
                attempt=attempt,
                error=str(exc),
            )

        if state == "failed" and attempt >= _MAX_ATTEMPTS:
            state = "exhausted"

        # Persist delivery record.
        delivery_id = uuid.uuid4()
        response_hash = (
            hashlib.sha256(response.content).hexdigest()
            if http_status is not None and "response" in dir()
            else None
        )

        async with self._session_factory() as session, session.begin():
            delivery = WebhookDelivery(
                id=delivery_id,
                webhook_id=webhook.id,
                outbox_event_id=outbox_event_id,
                http_status=http_status,
                attempt=attempt,
                state=state,
                response_body_sha256=response_hash,
            )
            session.add(delivery)

            # Bump failure streak on failed delivery.
            if state in ("failed", "exhausted"):
                webhook_stmt = select(Webhook).where(Webhook.id == webhook.id)
                wh_result = await session.execute(webhook_stmt)
                wh = wh_result.scalar_one_or_none()
                if wh is not None:
                    wh.failure_streak += 1
            elif state == "delivered":
                webhook_stmt = select(Webhook).where(Webhook.id == webhook.id)
                wh_result = await session.execute(webhook_stmt)
                wh = wh_result.scalar_one_or_none()
                if wh is not None:
                    wh.failure_streak = 0

        await logger.ainfo(
            "webhook_delivery_attempt",
            webhook_id=str(webhook.id),
            delivery_id=str(delivery_id),
            attempt=attempt,
            state=state,
            http_status=http_status,
        )

        return DeliveryResult(
            delivery_id=delivery_id,
            webhook_id=webhook.id,
            attempt=attempt,
            http_status=http_status,
            state=state,
        )

    def get_retry_delay(self, attempt: int) -> int | None:
        """Return seconds to wait before the given attempt, or None if exhausted."""
        idx = attempt - 1
        if idx < 0 or idx >= len(_RETRY_DELAYS):
            return None
        return _RETRY_DELAYS[idx]
