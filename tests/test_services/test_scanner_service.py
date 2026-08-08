"""Unit tests for quarantine inspection and malware safeguards."""

from __future__ import annotations

import io
import uuid
import zipfile
from collections.abc import Callable

from documind.services.scanner_service import ScannerService


class _ContentSource:
    def __init__(self, payload: bytes, declared_mime: str) -> None:
        self._payload = payload
        self._declared_mime = declared_mime

    async def read_version_bytes(self, version_id: uuid.UUID) -> bytes:
        return self._payload

    async def declared_mime_for(self, version_id: uuid.UUID) -> str:
        return self._declared_mime


class _ClamAV:
    def __init__(self, verdict: str) -> None:
        self._verdict = verdict
        self.calls: list[bytes] = []

    async def scan(self, payload: bytes) -> str:
        self.calls.append(payload)
        return self._verdict


def _mime_detector(mime: str) -> Callable[[bytes], str]:
    return lambda _: mime


def _zip_payload(contents: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", contents)
    return buffer.getvalue()


async def test_inspect_rejects_malware_without_exposing_scanner_signature() -> None:
    version_id = uuid.uuid4()
    clamav = _ClamAV("stream: Eicar-Test-Signature FOUND")
    service = ScannerService(
        content_source=_ContentSource(b"%PDF-1.7 safe-looking", "application/pdf"),
        clamav_client=clamav,
        mime_detector=_mime_detector("application/pdf"),
    )

    result = await service.inspect(version_id)

    assert result.safe is False
    assert result.safe_error_class == "unsafe_content"
    assert result.safe_error_code == "MALWARE_DETECTED"
    assert "Eicar" not in result.safe_message
    assert clamav.calls == [b"%PDF-1.7 safe-looking"]


async def test_inspect_rejects_zip_bomb_before_parser_or_scanner() -> None:
    version_id = uuid.uuid4()
    clamav = _ClamAV("stream: OK")
    payload = _zip_payload(b"0" * 32_768)
    service = ScannerService(
        content_source=_ContentSource(
            payload,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        clamav_client=clamav,
        mime_detector=_mime_detector("application/zip"),
        max_archive_ratio=2,
    )

    result = await service.inspect(version_id)

    assert result.safe is False
    assert result.safe_error_class == "unsafe_content"
    assert result.safe_error_code == "ARCHIVE_EXPANSION_LIMIT"
    assert clamav.calls == []


async def test_inspect_rejects_unsupported_magic_mime() -> None:
    version_id = uuid.uuid4()
    clamav = _ClamAV("stream: OK")
    service = ScannerService(
        content_source=_ContentSource(b"MZ\x00\x00", "application/octet-stream"),
        clamav_client=clamav,
        mime_detector=_mime_detector("application/x-dosexec"),
    )

    result = await service.inspect(version_id)

    assert result.safe is False
    assert result.detected_mime == "application/x-dosexec"
    assert result.safe_error_class == "unsupported_content"
    assert result.safe_error_code == "UNSUPPORTED_MIME"
    assert clamav.calls == []
