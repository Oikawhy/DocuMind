"""Tests for ProcessingService normalization and NormalizedDocumentSource.

Covers Task 5.2: repeated text, Unicode NFC changes, missing keys,
malformed JSON, page ranges, block-order violations, and the
NormalizedDocumentSource loader with integrity verification.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from documind.services.ocr_service import ParserAttempt, ParseResult
from documind.services.processing_service import (
    NormalizationIntegrityError,
    NormalizedDocumentSource,
    NormalizeResult,
    ProcessingService,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VERSION_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _parse_result(
    *,
    text: str = "Hello world",
    pages: list[dict[str, Any]] | None = None,
    success: bool = True,
) -> ParseResult:
    if pages is None:
        pages = [
            {
                "page_number": 1,
                "text": text,
                "blocks": [{"text": text, "block_id": "b-0"}],
            }
        ]
    return ParseResult(
        version_id=str(_VERSION_ID),
        success=success,
        engine="test",
        text=text,
        pages=pages,
        confidence=0.99,
        parser_attempts=[ParserAttempt(engine="test", version="1.0", outcome="success")],
    )


def _make_source(parse_result: ParseResult) -> ProcessingService:
    source = AsyncMock()
    source.get_parse_result.return_value = parse_result
    return ProcessingService(parse_result_source=source)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# ProcessingService.normalize() tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normalize_produces_block_records_with_global_offsets() -> None:
    """Blocks contain block_id, page_number, section_path, start/end offsets."""
    text = "First block. Second block."
    pages = [
        {
            "page_number": 1,
            "text": text,
            "blocks": [
                {"text": "First block.", "block_id": "b-0"},
                {"text": "Second block.", "block_id": "b-1"},
            ],
        }
    ]
    service = _make_source(_parse_result(text=text, pages=pages))
    result = await service.normalize(_VERSION_ID)

    assert len(result.blocks) == 2
    assert result.blocks[0] == {
        "block_id": "b-0",
        "page_number": 1,
        "section_path": [],
        "start_offset": 0,
        "end_offset": 12,
    }
    assert result.blocks[1] == {
        "block_id": "b-1",
        "page_number": 1,
        "section_path": [],
        "start_offset": 13,
        "end_offset": 26,
    }


@pytest.mark.asyncio
async def test_normalize_multi_page_blocks() -> None:
    """Blocks across multiple pages have correct global offsets."""
    text = "Page one. Page two."
    pages = [
        {
            "page_number": 1,
            "text": "Page one.",
            "blocks": [{"text": "Page one.", "block_id": "p1-b0"}],
        },
        {
            "page_number": 2,
            "text": "Page two.",
            "blocks": [{"text": "Page two.", "block_id": "p2-b0"}],
        },
    ]
    service = _make_source(_parse_result(text=text, pages=pages))
    result = await service.normalize(_VERSION_ID)

    assert len(result.blocks) == 2
    assert result.blocks[0]["page_number"] == 1
    assert result.blocks[0]["start_offset"] == 0
    assert result.blocks[1]["page_number"] == 2
    assert result.blocks[1]["start_offset"] == text.index("Page two.")


@pytest.mark.asyncio
async def test_normalize_section_path_preserved() -> None:
    """Section path from page or block level is propagated."""
    text = "Heading content"
    pages = [
        {
            "page_number": 1,
            "section_path": ["Chapter 1"],
            "text": text,
            "blocks": [{"text": text, "block_id": "b-0"}],
        }
    ]
    service = _make_source(_parse_result(text=text, pages=pages))
    result = await service.normalize(_VERSION_ID)
    assert result.blocks[0]["section_path"] == ["Chapter 1"]


@pytest.mark.asyncio
async def test_normalize_repeated_text() -> None:
    """Repeated text is resolved in reading order, not greedily."""
    text = "same same same"
    pages = [
        {
            "page_number": 1,
            "text": text,
            "blocks": [
                {"text": "same", "block_id": "b-0"},
                {"text": "same", "block_id": "b-1"},
                {"text": "same", "block_id": "b-2"},
            ],
        }
    ]
    service = _make_source(_parse_result(text=text, pages=pages))
    result = await service.normalize(_VERSION_ID)
    offsets = [(b["start_offset"], b["end_offset"]) for b in result.blocks]
    # All three occurrences found in order, non-overlapping
    assert len(offsets) == 3
    for i in range(1, len(offsets)):
        assert offsets[i][0] >= offsets[i - 1][1], "Blocks must not overlap"


@pytest.mark.asyncio
async def test_normalize_unicode_nfc() -> None:
    """Unicode NFC normalization produces deterministic output."""
    # U+00E9 (é) vs U+0065 + U+0301 (e + combining acute)
    decomposed = "caf\u0065\u0301"
    composed = "caf\u00e9"
    pages = [
        {
            "page_number": 1,
            "text": decomposed,
            "blocks": [{"text": decomposed, "block_id": "b-0"}],
        }
    ]
    service = _make_source(_parse_result(text=decomposed, pages=pages))
    result = await service.normalize(_VERSION_ID)
    # NFC normalizes decomposed to composed
    assert result.text == composed
    assert len(result.blocks) == 1
    assert result.blocks[0]["end_offset"] == len(composed)


@pytest.mark.asyncio
async def test_normalize_empty_blocks_skipped() -> None:
    """Blocks with only whitespace are skipped."""
    text = "Content"
    pages = [
        {
            "page_number": 1,
            "text": text,
            "blocks": [
                {"text": "   ", "block_id": "empty"},
                {"text": "Content", "block_id": "real"},
            ],
        }
    ]
    service = _make_source(_parse_result(text=text, pages=pages))
    result = await service.normalize(_VERSION_ID)
    assert len(result.blocks) == 1
    assert result.blocks[0]["block_id"] == "real"


@pytest.mark.asyncio
async def test_normalize_rejects_unresolvable_block() -> None:
    """Blocks whose text doesn't appear in normalized text are rejected."""
    text = "Only this text"
    pages = [
        {
            "page_number": 1,
            "text": text,
            "blocks": [{"text": "Missing text entirely", "block_id": "bad"}],
        }
    ]
    service = _make_source(_parse_result(text=text, pages=pages))
    with pytest.raises(ValueError, match="not found"):
        await service.normalize(_VERSION_ID)


@pytest.mark.asyncio
async def test_normalize_content_sha256() -> None:
    """Content SHA-256 is computed from the NFC-normalized text."""
    text = "Hello world"
    service = _make_source(_parse_result(text=text))
    result = await service.normalize(_VERSION_ID)
    assert result.content_sha256 == _sha256(text)


@pytest.mark.asyncio
async def test_normalize_auto_block_ids() -> None:
    """Blocks without explicit block_id get auto-generated IDs."""
    text = "First Second"
    pages = [
        {
            "page_number": 1,
            "text": text,
            "blocks": [
                {"text": "First"},
                {"text": "Second"},
            ],
        }
    ]
    service = _make_source(_parse_result(text=text, pages=pages))
    result = await service.normalize(_VERSION_ID)
    assert result.blocks[0]["block_id"] == "b-0"
    assert result.blocks[1]["block_id"] == "b-1"


@pytest.mark.asyncio
async def test_normalize_persists_with_sink() -> None:
    """When a sink is provided, the artifact is persisted and key returned."""
    source = AsyncMock()
    source.get_parse_result.return_value = _parse_result()
    sink = AsyncMock()
    sink.write_normalized.return_value = "derived/test/normalized/norm-r2.json"
    service = ProcessingService(
        parse_result_source=source,
        normalized_output_sink=sink,
    )
    result = await service.normalize(_VERSION_ID)
    assert result.normalized_object_key == "derived/test/normalized/norm-r2.json"
    sink.write_normalized.assert_called_once()


# ---------------------------------------------------------------------------
# NormalizedDocumentSource.load() tests
# ---------------------------------------------------------------------------


def _make_normalized_payload(
    *,
    text: str = "Hello world",
    blocks: list[dict[str, Any]] | None = None,
    version_id: str | None = None,
    content_sha256: str | None = None,
    normalization_revision: str = "norm-r2",
) -> dict[str, Any]:
    """Build a valid normalized JSON payload."""
    if blocks is None:
        blocks = [
            {
                "block_id": "b-0",
                "page_number": 1,
                "section_path": [],
                "start_offset": 0,
                "end_offset": len(text),
            }
        ]
    return {
        "version_id": version_id or str(_VERSION_ID),
        "normalization_revision": normalization_revision,
        "text": text,
        "pages": [{"page_number": 1, "text": text, "blocks": []}],
        "offset_map": [],
        "language_evidence": [{"language": "en", "confidence": 1.0}],
        "content_sha256": content_sha256 or _sha256(text),
        "parser_attempts": [],
        "blocks": blocks,
    }


def _storage_mock(payload: dict[str, Any]) -> AsyncMock:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    storage = AsyncMock()
    storage.read_bytes.return_value = raw
    return storage


@pytest.mark.asyncio
async def test_load_valid_normalized_artifact() -> None:
    """A well-formed artifact loads successfully."""
    payload = _make_normalized_payload()
    source = NormalizedDocumentSource(storage=_storage_mock(payload))
    result = await source.load(
        normalized_object_key="derived/test/norm.json",
        expected_version_id=_VERSION_ID,
        expected_content_sha256=_sha256("Hello world"),
    )
    assert isinstance(result, NormalizeResult)
    assert result.text == "Hello world"
    assert len(result.blocks) == 1
    assert result.blocks[0]["block_id"] == "b-0"


@pytest.mark.asyncio
async def test_load_missing_object_key() -> None:
    """Null or empty object key raises integrity error."""
    source = NormalizedDocumentSource(storage=AsyncMock())
    with pytest.raises(NormalizationIntegrityError, match="normalized_object_key"):
        await source.load(
            normalized_object_key=None,
            expected_version_id=_VERSION_ID,
            expected_content_sha256="anything",
        )


@pytest.mark.asyncio
async def test_load_storage_read_failure() -> None:
    """Storage read failure raises integrity error."""
    storage = AsyncMock()
    storage.read_bytes.side_effect = OSError("Connection refused")
    source = NormalizedDocumentSource(storage=storage)
    with pytest.raises(NormalizationIntegrityError, match="Cannot read"):
        await source.load(
            normalized_object_key="derived/test/norm.json",
            expected_version_id=_VERSION_ID,
            expected_content_sha256="anything",
        )


@pytest.mark.asyncio
async def test_load_malformed_json() -> None:
    """Invalid JSON raises integrity error."""
    storage = AsyncMock()
    storage.read_bytes.return_value = b"not valid json{{"
    source = NormalizedDocumentSource(storage=storage)
    with pytest.raises(NormalizationIntegrityError, match="invalid JSON"):
        await source.load(
            normalized_object_key="derived/test/norm.json",
            expected_version_id=_VERSION_ID,
            expected_content_sha256="anything",
        )


@pytest.mark.asyncio
async def test_load_version_id_mismatch() -> None:
    """Mismatched version ID raises integrity error."""
    payload = _make_normalized_payload(version_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    source = NormalizedDocumentSource(storage=_storage_mock(payload))
    with pytest.raises(NormalizationIntegrityError, match="Version ID mismatch"):
        await source.load(
            normalized_object_key="derived/test/norm.json",
            expected_version_id=_VERSION_ID,
            expected_content_sha256=_sha256("Hello world"),
        )


@pytest.mark.asyncio
async def test_load_content_sha256_mismatch() -> None:
    """Content SHA-256 mismatch between stored text and expected value raises error."""
    payload = _make_normalized_payload()
    source = NormalizedDocumentSource(storage=_storage_mock(payload))
    with pytest.raises(NormalizationIntegrityError, match="SHA-256"):
        await source.load(
            normalized_object_key="derived/test/norm.json",
            expected_version_id=_VERSION_ID,
            expected_content_sha256="wrong_hash_value",
        )


@pytest.mark.asyncio
async def test_load_tampered_text_sha256() -> None:
    """SHA-256 computed from actual text doesn't match stored sha256."""
    payload = _make_normalized_payload(content_sha256="tampered_hash")
    source = NormalizedDocumentSource(storage=_storage_mock(payload))
    with pytest.raises(NormalizationIntegrityError, match="SHA-256"):
        await source.load(
            normalized_object_key="derived/test/norm.json",
            expected_version_id=_VERSION_ID,
            expected_content_sha256="tampered_hash",
        )


@pytest.mark.asyncio
async def test_load_missing_normalization_revision() -> None:
    """Missing normalization_revision raises integrity error."""
    payload = _make_normalized_payload()
    del payload["normalization_revision"]
    source = NormalizedDocumentSource(storage=_storage_mock(payload))
    with pytest.raises(NormalizationIntegrityError, match="normalization_revision"):
        await source.load(
            normalized_object_key="derived/test/norm.json",
            expected_version_id=_VERSION_ID,
            expected_content_sha256=_sha256("Hello world"),
        )


@pytest.mark.asyncio
async def test_load_overlapping_blocks_rejected() -> None:
    """Overlapping block spans raise integrity error."""
    payload = _make_normalized_payload(
        text="Hello world",
        blocks=[
            {"block_id": "b-0", "page_number": 1, "section_path": [], "start_offset": 0, "end_offset": 8},
            {"block_id": "b-1", "page_number": 1, "section_path": [], "start_offset": 5, "end_offset": 11},
        ],
    )
    source = NormalizedDocumentSource(storage=_storage_mock(payload))
    with pytest.raises(NormalizationIntegrityError, match="overlaps"):
        await source.load(
            normalized_object_key="derived/test/norm.json",
            expected_version_id=_VERSION_ID,
            expected_content_sha256=_sha256("Hello world"),
        )


@pytest.mark.asyncio
async def test_load_block_exceeding_text_length() -> None:
    """Block end_offset exceeding text length raises integrity error."""
    payload = _make_normalized_payload(
        text="short",
        blocks=[
            {"block_id": "b-0", "page_number": 1, "section_path": [], "start_offset": 0, "end_offset": 999},
        ],
    )
    source = NormalizedDocumentSource(storage=_storage_mock(payload))
    with pytest.raises(NormalizationIntegrityError, match="exceeds text length"):
        await source.load(
            normalized_object_key="derived/test/norm.json",
            expected_version_id=_VERSION_ID,
            expected_content_sha256=_sha256("short"),
        )


@pytest.mark.asyncio
async def test_load_block_missing_block_id() -> None:
    """Block without block_id raises integrity error."""
    payload = _make_normalized_payload(
        blocks=[
            {"page_number": 1, "section_path": [], "start_offset": 0, "end_offset": 5},
        ],
    )
    source = NormalizedDocumentSource(storage=_storage_mock(payload))
    with pytest.raises(NormalizationIntegrityError, match="block_id"):
        await source.load(
            normalized_object_key="derived/test/norm.json",
            expected_version_id=_VERSION_ID,
            expected_content_sha256=_sha256("Hello world"),
        )


@pytest.mark.asyncio
async def test_load_invalid_page_number() -> None:
    """Block with non-positive page_number raises integrity error."""
    payload = _make_normalized_payload(
        blocks=[
            {"block_id": "b-0", "page_number": 0, "section_path": [], "start_offset": 0, "end_offset": 5},
        ],
    )
    source = NormalizedDocumentSource(storage=_storage_mock(payload))
    with pytest.raises(NormalizationIntegrityError, match="page_number"):
        await source.load(
            normalized_object_key="derived/test/norm.json",
            expected_version_id=_VERSION_ID,
            expected_content_sha256=_sha256("Hello world"),
        )


@pytest.mark.asyncio
async def test_load_non_dict_artifact() -> None:
    """A JSON array (not object) raises integrity error."""
    storage = AsyncMock()
    storage.read_bytes.return_value = b'["not", "an", "object"]'
    source = NormalizedDocumentSource(storage=storage)
    with pytest.raises(NormalizationIntegrityError, match="not a JSON object"):
        await source.load(
            normalized_object_key="derived/test/norm.json",
            expected_version_id=_VERSION_ID,
            expected_content_sha256="anything",
        )


@pytest.mark.asyncio
async def test_load_block_inverted_offsets() -> None:
    """Block with start_offset >= end_offset raises integrity error."""
    payload = _make_normalized_payload(
        blocks=[
            {"block_id": "b-0", "page_number": 1, "section_path": [], "start_offset": 5, "end_offset": 3},
        ],
    )
    source = NormalizedDocumentSource(storage=_storage_mock(payload))
    with pytest.raises(NormalizationIntegrityError, match="start_offset >= end_offset"):
        await source.load(
            normalized_object_key="derived/test/norm.json",
            expected_version_id=_VERSION_ID,
            expected_content_sha256=_sha256("Hello world"),
        )
