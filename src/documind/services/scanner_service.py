"""Safe quarantine inspection using ClamAV, MIME detection, and ZIP defenses."""

from __future__ import annotations

import asyncio
import io
import struct
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

_MAX_ARCHIVE_NESTING = 1
_MAX_ARCHIVE_RATIO = 20
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 500 * 1024 * 1024

_ALLOWED_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
        "application/xml",
        "text/xml",
        "image/png",
        "image/jpeg",
        "image/tiff",
    },
)
_ZIP_CONTAINER_MIME_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
    },
)


class VersionContentSource(Protocol):
    """Load a private version's quarantined bytes and its declared MIME type."""

    async def read_version_bytes(self, version_id: uuid.UUID) -> bytes:
        """Return private quarantine bytes for one immutable version."""

    async def declared_mime_for(self, version_id: uuid.UUID) -> str:
        """Return the server-validated MIME type declared for the version."""


class ClamAVClient(Protocol):
    """Minimal clamd adapter; implementations must stream bytes, never paths."""

    async def scan(self, payload: bytes) -> str:
        """Return the clamd response, or raise an operational exception."""


class ScannerUnavailableError(RuntimeError):
    """ClamAV cannot be contacted or returned an operational failure."""


@dataclass(frozen=True)
class InspectionResult:
    """Safe inspection outcome suitable for persistence and workflow transport."""

    version_id: str
    safe: bool
    detected_mime: str | None
    safe_error_class: str | None = None
    safe_error_code: str | None = None
    safe_message: str | None = None
    archive_members: int = 0


class ClamAVTCPClient:
    """Small clamd INSTREAM client that keeps malware bytes out of logs."""

    def __init__(self, host: str, port: int, *, timeout_seconds: float = 30.0) -> None:
        self._host = host
        self._port = port
        self._timeout_seconds = timeout_seconds

    async def scan(self, payload: bytes) -> str:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._timeout_seconds,
            )
            try:
                writer.write(b"zINSTREAM\x00")
                for offset in range(0, len(payload), 64 * 1024):
                    chunk = payload[offset : offset + 64 * 1024]
                    writer.write(struct.pack("!I", len(chunk)))
                    writer.write(chunk)
                writer.write(struct.pack("!I", 0))
                await asyncio.wait_for(writer.drain(), timeout=self._timeout_seconds)
                response = await asyncio.wait_for(reader.read(4096), timeout=self._timeout_seconds)
            finally:
                writer.close()
                await writer.wait_closed()
        except (TimeoutError, OSError) as exc:
            raise ScannerUnavailableError("Malware scanner is unavailable.") from exc

        verdict = response.decode("utf-8", errors="replace").strip("\x00\r\n ")
        if not verdict or "ERROR" in verdict.upper():
            raise ScannerUnavailableError("Malware scanner did not return a usable verdict.")
        return verdict


class ScannerService:
    """Inspect untrusted version bytes before parsing can read them."""

    def __init__(
        self,
        *,
        content_source: VersionContentSource,
        clamav_client: ClamAVClient,
        mime_detector: Callable[[bytes], str] | None = None,
        max_archive_depth: int = _MAX_ARCHIVE_NESTING,
        max_archive_ratio: int = _MAX_ARCHIVE_RATIO,
        max_archive_uncompressed_bytes: int = _MAX_ARCHIVE_UNCOMPRESSED_BYTES,
    ) -> None:
        self._content_source = content_source
        self._clamav_client = clamav_client
        self._mime_detector = mime_detector or _detect_mime
        self._max_archive_depth = max_archive_depth
        self._max_archive_ratio = max_archive_ratio
        self._max_archive_uncompressed_bytes = max_archive_uncompressed_bytes

    async def inspect(self, version_id: uuid.UUID) -> InspectionResult:
        """Inspect one quarantine object and return only a safe, stable outcome."""
        payload = await self._content_source.read_version_bytes(version_id)
        declared_mime = await self._content_source.declared_mime_for(version_id)
        detected_mime = self._mime_detector(payload).lower().strip()

        if not _mime_is_allowed(detected_mime, declared_mime):
            return _rejected(
                version_id,
                detected_mime,
                error_class="unsupported_content",
                error_code="UNSUPPORTED_MIME",
                message="The uploaded format is not supported.",
            )

        archive_result = _inspect_zip_archive(
            payload,
            version_id=version_id,
            detected_mime=detected_mime,
            max_depth=self._max_archive_depth,
            max_ratio=self._max_archive_ratio,
            max_uncompressed_bytes=self._max_archive_uncompressed_bytes,
        )
        if archive_result is not None:
            return archive_result

        # T4-11: Format-specific preflight validation.
        preflight = _preflight_validate(payload, version_id, detected_mime)
        if preflight is not None:
            return preflight

        try:
            verdict = await self._clamav_client.scan(payload)
        except ScannerUnavailableError:
            raise
        except (TimeoutError, OSError) as exc:
            raise ScannerUnavailableError("Malware scanner is unavailable.") from exc

        if "OK" not in verdict.upper() or "FOUND" in verdict.upper():
            return _rejected(
                version_id,
                detected_mime,
                error_class="unsafe_content",
                error_code="MALWARE_DETECTED",
                message="The file was rejected by malware inspection.",
            )

        return InspectionResult(
            version_id=str(version_id),
            safe=True,
            detected_mime=detected_mime,
            archive_members=_archive_member_count(payload),
        )


def _detect_mime(payload: bytes) -> str:
    """Use libmagic lazily so importing the service needs no native call."""
    import magic

    return str(magic.from_buffer(payload, mime=True))


def _mime_is_allowed(detected_mime: str, declared_mime: str) -> bool:
    """Accept only an admitted MIME and its valid libmagic representation."""
    declared = declared_mime.lower().strip()
    if declared not in _ALLOWED_MIME_TYPES:
        return False
    if detected_mime == declared:
        return True
    if detected_mime == "application/zip" and declared in _ZIP_CONTAINER_MIME_TYPES:
        return True
    if {detected_mime, declared} <= {"application/xml", "text/xml"}:
        return True
    # T4-10: Markdown files are reported as text/plain by libmagic.
    if detected_mime == "text/plain" and declared == "text/markdown":
        return True
    return False


def _inspect_zip_archive(
    payload: bytes,
    *,
    version_id: uuid.UUID,
    detected_mime: str,
    max_depth: int,
    max_ratio: int,
    max_uncompressed_bytes: int,
) -> InspectionResult | None:
    """Apply ZIP expansion, zip-slip, duplicate, and nesting limits in memory."""
    is_zip = zipfile.is_zipfile(io.BytesIO(payload))
    if detected_mime == "application/zip" and not is_zip:
        return _unsafe_archive(version_id, detected_mime, "INVALID_ARCHIVE")
    if not is_zip:
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                return _unsafe_archive(version_id, detected_mime, "ARCHIVE_DUPLICATE_ENTRY")

            compressed = 0
            uncompressed = 0
            for member in members:
                path = PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts:
                    return _unsafe_archive(version_id, detected_mime, "ARCHIVE_PATH_TRAVERSAL")
                if max_depth < 1 and _looks_like_archive_member(archive, member):
                    return _unsafe_archive(version_id, detected_mime, "ARCHIVE_NESTING_LIMIT")
                if _looks_like_archive_member(archive, member):
                    return _unsafe_archive(version_id, detected_mime, "ARCHIVE_NESTING_LIMIT")
                compressed += member.compress_size
                uncompressed += member.file_size
                if uncompressed > max_uncompressed_bytes:
                    return _unsafe_archive(version_id, detected_mime, "ARCHIVE_SIZE_LIMIT")

            if uncompressed and (compressed == 0 or uncompressed > compressed * max_ratio):
                return _unsafe_archive(version_id, detected_mime, "ARCHIVE_EXPANSION_LIMIT")
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return _unsafe_archive(version_id, detected_mime, "INVALID_ARCHIVE")
    return None


def _looks_like_archive_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> bool:
    if member.is_dir() or member.file_size < 4:
        return False
    with archive.open(member) as file:
        signature = file.read(8)
    return signature.startswith((b"PK\x03\x04", b"\x1f\x8b", b"7z\xbc\xaf'\x1c"))


def _archive_member_count(payload: bytes) -> int:
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        return 0
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            return len(archive.infolist())
    except zipfile.BadZipFile:
        return 0


def _unsafe_archive(version_id: uuid.UUID, detected_mime: str, error_code: str) -> InspectionResult:
    return _rejected(
        version_id,
        detected_mime,
        error_class="unsafe_content",
        error_code=error_code,
        message="The uploaded archive violates safety limits.",
    )


def _rejected(
    version_id: uuid.UUID,
    detected_mime: str | None,
    *,
    error_class: str,
    error_code: str,
    message: str,
) -> InspectionResult:
    return InspectionResult(
        version_id=str(version_id),
        safe=False,
        detected_mime=detected_mime,
        safe_error_class=error_class,
        safe_error_code=error_code,
        safe_message=message,
    )


# ---------------------------------------------------------------------------
# T4-11: Format-specific preflight validators
# ---------------------------------------------------------------------------
_MAX_PDF_PAGES = 5_000
_MAX_PDF_OBJECTS = 100_000
_MAX_IMAGE_PIXELS = 200_000_000
_MAX_XML_ENTITY_EXPANSIONS = 10_000


def _preflight_validate(
    payload: bytes,
    version_id: uuid.UUID,
    detected_mime: str,
) -> InspectionResult | None:
    """Dispatch to format-specific preflight checks based on detected MIME."""
    if detected_mime == "application/pdf":
        return _preflight_pdf(payload, version_id, detected_mime)
    if detected_mime in {"image/png", "image/jpeg", "image/tiff"}:
        return _preflight_image(payload, version_id, detected_mime)
    if detected_mime in {"application/xml", "text/xml", "text/html"}:
        return _preflight_xml(payload, version_id, detected_mime)
    if detected_mime == "application/zip":
        return _preflight_ooxml(payload, version_id, detected_mime)
    return None


def _preflight_pdf(
    payload: bytes,
    version_id: uuid.UUID,
    detected_mime: str,
) -> InspectionResult | None:
    """Reject encrypted PDFs and those exceeding page/object safety limits."""
    try:
        # Quick header-level checks without a full parser dependency.
        header = payload[:1024].decode("latin-1", errors="replace")
        if "/Encrypt" in header:
            return _rejected(
                version_id, detected_mime,
                error_class="unsafe_content",
                error_code="PDF_ENCRYPTED",
                message="Encrypted PDF documents are not supported.",
            )
        # Estimate object count from cross-reference markers.
        obj_count = payload.count(b" obj")
        if obj_count > _MAX_PDF_OBJECTS:
            return _rejected(
                version_id, detected_mime,
                error_class="unsafe_content",
                error_code="PDF_OBJECT_LIMIT",
                message="The PDF exceeds the maximum object count.",
            )
        # Estimate page count from /Type /Page markers.
        page_markers = payload.count(b"/Type /Page") + payload.count(b"/Type/Page")
        if page_markers > _MAX_PDF_PAGES:
            return _rejected(
                version_id, detected_mime,
                error_class="unsafe_content",
                error_code="PDF_PAGE_LIMIT",
                message="The PDF exceeds the maximum page count.",
            )
    except Exception:
        pass
    return None


def _preflight_ooxml(
    payload: bytes,
    version_id: uuid.UUID,
    detected_mime: str,
) -> InspectionResult | None:
    """Reject OOXML documents containing macros or external relationships."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for name in archive.namelist():
                lower = name.lower()
                # Reject macro-enabled content.
                if lower.endswith((".vba", ".bin")) and "vbaproject" in lower:
                    return _rejected(
                        version_id, detected_mime,
                        error_class="unsafe_content",
                        error_code="OOXML_MACRO_DETECTED",
                        message="Office documents containing macros are not supported.",
                    )
            # Check for external relationships.
            for name in archive.namelist():
                if name.endswith(".rels"):
                    try:
                        rels_content = archive.read(name).decode("utf-8", errors="replace")
                        if 'TargetMode="External"' in rels_content:
                            return _rejected(
                                version_id, detected_mime,
                                error_class="unsafe_content",
                                error_code="OOXML_EXTERNAL_REFERENCE",
                                message="Office documents with external references are not supported.",
                            )
                    except Exception:
                        pass
    except (zipfile.BadZipFile, OSError):
        pass
    return None


def _preflight_xml(
    payload: bytes,
    version_id: uuid.UUID,
    detected_mime: str,
) -> InspectionResult | None:
    """Reject XML with entity expansion or remote directives."""
    try:
        text = payload[:8192].decode("utf-8", errors="replace")
        # Check for DOCTYPE declarations with entity definitions.
        if "<!DOCTYPE" in text.upper() and "<!ENTITY" in text.upper():
            return _rejected(
                version_id, detected_mime,
                error_class="unsafe_content",
                error_code="XML_ENTITY_EXPANSION",
                message="XML documents with entity declarations are not supported.",
            )
        # Check for external DTD references.
        if "SYSTEM" in text and "<!DOCTYPE" in text.upper():
            return _rejected(
                version_id, detected_mime,
                error_class="unsafe_content",
                error_code="XML_EXTERNAL_DTD",
                message="XML documents with external DTD references are not supported.",
            )
    except Exception:
        pass
    return None


def _preflight_image(
    payload: bytes,
    version_id: uuid.UUID,
    detected_mime: str,
) -> InspectionResult | None:
    """Reject images exceeding pixel budget without decoding."""
    try:
        import struct as _struct

        if detected_mime == "image/png" and len(payload) > 24:
            # PNG IHDR chunk: width at offset 16, height at offset 20 (big-endian).
            width = _struct.unpack(">I", payload[16:20])[0]
            height = _struct.unpack(">I", payload[20:24])[0]
            if width * height > _MAX_IMAGE_PIXELS:
                return _rejected(
                    version_id, detected_mime,
                    error_class="unsafe_content",
                    error_code="IMAGE_PIXEL_LIMIT",
                    message="The image exceeds the maximum pixel budget.",
                )
        elif detected_mime == "image/jpeg":
            # Scan for SOF markers to find dimensions.
            offset = 2
            while offset < len(payload) - 9:
                if payload[offset] != 0xFF:
                    break
                marker = payload[offset + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    height = _struct.unpack(">H", payload[offset + 5 : offset + 7])[0]
                    width = _struct.unpack(">H", payload[offset + 7 : offset + 9])[0]
                    if width * height > _MAX_IMAGE_PIXELS:
                        return _rejected(
                            version_id, detected_mime,
                            error_class="unsafe_content",
                            error_code="IMAGE_PIXEL_LIMIT",
                            message="The image exceeds the maximum pixel budget.",
                        )
                    break
                seg_len = _struct.unpack(">H", payload[offset + 2 : offset + 4])[0]
                offset += 2 + seg_len
    except Exception:
        pass
    return None
