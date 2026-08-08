"""Unit tests for WebhookService — SSRF defense, HMAC signing, delivery."""

from __future__ import annotations

import hashlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from documind.domain.errors import SSRFViolationError
from documind.services.webhook_service import WebhookService


# ---------------------------------------------------------------------------
# SSRF validation
# ---------------------------------------------------------------------------


class TestSSRFValidation:
    """SSRF defense tests per §9.5."""

    def test_rejects_http_url(self) -> None:
        with pytest.raises(SSRFViolationError, match="HTTPS"):
            WebhookService.validate_target_url("http://example.com/hook")

    def test_rejects_empty_hostname(self) -> None:
        with pytest.raises(SSRFViolationError):
            WebhookService.validate_target_url("https:///path")

    @patch("documind.services.webhook_service.socket.getaddrinfo")
    def test_rejects_loopback(self, mock_dns: MagicMock) -> None:
        mock_dns.return_value = [
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]
        with pytest.raises(SSRFViolationError, match="Loopback"):
            WebhookService.validate_target_url("https://localhost/hook")

    @patch("documind.services.webhook_service.socket.getaddrinfo")
    def test_rejects_private_ipv4(self, mock_dns: MagicMock) -> None:
        mock_dns.return_value = [
            (2, 1, 6, "", ("10.0.0.1", 0)),
        ]
        with pytest.raises(SSRFViolationError, match="Private"):
            WebhookService.validate_target_url("https://internal.corp/hook")

    @patch("documind.services.webhook_service.socket.getaddrinfo")
    def test_rejects_private_192_168(self, mock_dns: MagicMock) -> None:
        mock_dns.return_value = [
            (2, 1, 6, "", ("192.168.1.1", 0)),
        ]
        with pytest.raises(SSRFViolationError, match="Private"):
            WebhookService.validate_target_url("https://router.local/hook")

    @patch("documind.services.webhook_service.socket.getaddrinfo")
    def test_rejects_link_local(self, mock_dns: MagicMock) -> None:
        mock_dns.return_value = [
            (2, 1, 6, "", ("169.254.0.1", 0)),
        ]
        with pytest.raises(SSRFViolationError, match="Private"):
            WebhookService.validate_target_url("https://link-local.test/hook")

    @patch("documind.services.webhook_service.socket.getaddrinfo")
    def test_accepts_public_ip(self, mock_dns: MagicMock) -> None:
        mock_dns.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]
        result = WebhookService.validate_target_url("https://example.com/hook")
        assert result == "https://example.com/hook"

    @patch("documind.services.webhook_service.socket.getaddrinfo")
    def test_rejects_dns_failure(self, mock_dns: MagicMock) -> None:
        import socket
        mock_dns.side_effect = socket.gaierror("DNS resolution failed")
        with pytest.raises(SSRFViolationError, match="DNS resolution"):
            WebhookService.validate_target_url("https://nonexistent.invalid/hook")


# ---------------------------------------------------------------------------
# HMAC signature
# ---------------------------------------------------------------------------


class TestHMACSignature:
    """HMAC-SHA256 signing per §9.5."""

    def test_compute_and_verify_roundtrip(self) -> None:
        ts = 1700000000
        body = b'{"type": "document.version.completed"}'
        secret = "my-webhook-secret-key-32-chars!!"

        signature = WebhookService.compute_signature(ts, body, secret)
        assert signature.startswith("v1=")
        assert len(signature) == 3 + 64  # "v1=" + 64 hex chars

        assert WebhookService.verify_signature(ts, body, secret, signature)

    def test_wrong_secret_fails_verification(self) -> None:
        ts = 1700000000
        body = b'{"event": "test"}'
        signature = WebhookService.compute_signature(ts, body, "correct-secret-32-characters!!!!")
        assert not WebhookService.verify_signature(ts, body, "wrong-secret-32-characters-here!", signature)

    def test_tampered_body_fails_verification(self) -> None:
        ts = 1700000000
        body = b'{"event": "test"}'
        secret = "the-secret-32-chars-minimum-here"
        signature = WebhookService.compute_signature(ts, body, secret)
        assert not WebhookService.verify_signature(ts, b'{"event": "tampered"}', secret, signature)

    def test_different_timestamp_fails_verification(self) -> None:
        body = b'{"event": "test"}'
        secret = "the-secret-32-chars-minimum-here"
        signature = WebhookService.compute_signature(1000, body, secret)
        assert not WebhookService.verify_signature(2000, body, secret, signature)


# ---------------------------------------------------------------------------
# Retry schedule
# ---------------------------------------------------------------------------


class TestRetrySchedule:
    """Retry delays per §9.5: 10s → 60s → 300s."""

    def test_attempt_delays(self) -> None:
        service = WebhookService(session_factory=MagicMock())
        assert service.get_retry_delay(1) == 10
        assert service.get_retry_delay(2) == 60
        assert service.get_retry_delay(3) == 300

    def test_exhausted_returns_none(self) -> None:
        service = WebhookService(session_factory=MagicMock())
        assert service.get_retry_delay(4) is None
        assert service.get_retry_delay(0) is None


# ---------------------------------------------------------------------------
# Secret hash storage
# ---------------------------------------------------------------------------


class TestSecretStorage:
    """Verify the service stores only a hash, never the raw secret."""

    @pytest.mark.asyncio
    async def test_register_stores_hash_not_raw_secret(self) -> None:
        mock_begin_ctx = MagicMock()
        mock_begin_ctx.__aenter__ = AsyncMock(return_value=None)
        mock_begin_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=mock_begin_ctx)
        mock_session.flush = AsyncMock()

        captured_webhook = None

        def capture_add(obj: object) -> None:
            nonlocal captured_webhook
            captured_webhook = obj

        mock_session.add = capture_add

        mock_factory = MagicMock(return_value=mock_session)

        service = WebhookService(session_factory=mock_factory)
        raw_secret = "my-super-secret-webhook-key-here!"

        with patch.object(WebhookService, "validate_target_url", return_value="https://example.com/hook"):
            webhook = await service.register_webhook(
                target_url="https://example.com/hook",
                event_type_glob="*",
                secret=raw_secret,
                created_by_subject="admin@test",
            )

        assert captured_webhook is not None
        expected_hash = hashlib.sha256(raw_secret.encode()).hexdigest()
        assert captured_webhook.secret_hash == expected_hash
