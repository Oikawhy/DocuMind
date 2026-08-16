"""Deterministic normalized-document construction after successful parsing."""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Protocol

from documind.services.ocr_service import ParseResult


class ParseResultSource(Protocol):
    """Load the parser result that the parse stage durably recorded."""

    async def get_parse_result(self, version_id: uuid.UUID) -> ParseResult:
        """Return the parse output for the immutable version."""


class NormalizedOutputSink(Protocol):
    """Persist the canonical normalized representation under a stable key."""

    async def write_normalized(
        self,
        version_id: uuid.UUID,
        normalization_revision: str,
        payload: dict[str, Any],
    ) -> str:
        """Write the payload and return its immutable object key."""


class NormalizedStorageReader(Protocol):
    """Read the canonical normalized artifact from private storage."""

    async def read_bytes(self, object_key: str) -> bytes:
        """Return the raw bytes of the stored normalized representation."""


class NormalizationIntegrityError(ValueError):
    """The persisted normalized artifact is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class NormalizedBlockRecord:
    """A reading-order block with document-global normalized start/end offsets."""

    block_id: str
    page_number: int
    section_path: list[str]
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class NormalizeResult:
    """Stable normalized representation persisted by the normalize activity."""

    version_id: str
    normalization_revision: str
    text: str
    pages: list[dict[str, Any]]
    offset_map: list[dict[str, int]]
    language_evidence: list[dict[str, float | str]]
    content_sha256: str
    parser_attempts: list[dict[str, str]]
    blocks: list[dict[str, Any]] = field(default_factory=list)
    normalized_object_key: str | None = None


class ProcessingService:
    """Normalize parsed text without changing its semantic or legal meaning."""

    def __init__(
        self,
        *,
        parse_result_source: ParseResultSource,
        normalization_revision: str = "norm-r2",
        normalized_output_sink: NormalizedOutputSink | None = None,
    ) -> None:
        self._parse_result_source = parse_result_source
        self._normalization_revision = normalization_revision
        self._normalized_output_sink = normalized_output_sink

    async def normalize(self, version_id: uuid.UUID) -> NormalizeResult:
        """Apply Unicode NFC and preserve page/block boundaries and provenance."""
        parse_result = await self._parse_result_source.get_parse_result(version_id)
        if not parse_result.success:
            raise ValueError("Cannot normalize a parser result without usable text.")

        text, offset_map = _normalize_text(parse_result.text)
        pages = [_normalize_page(page) for page in parse_result.pages]
        blocks = _derive_block_records(text, pages)
        attempts = [
            {"engine": attempt.engine, "version": attempt.version, "outcome": attempt.outcome}
            for attempt in parse_result.parser_attempts
        ]
        result = NormalizeResult(
            version_id=str(version_id),
            normalization_revision=self._normalization_revision,
            text=text,
            pages=pages,
            offset_map=offset_map,
            language_evidence=_language_evidence(text),
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            parser_attempts=attempts,
            blocks=blocks,
        )
        if self._normalized_output_sink is None:
            return result
        object_key = await self._normalized_output_sink.write_normalized(
            version_id,
            result.normalization_revision,
            asdict(result),
        )
        return replace(result, normalized_object_key=object_key)


class NormalizedDocumentSource:
    """Load and validate a persisted normalized artifact for chunking.

    Reads ``DocumentVersion.normalized_object_key`` from private storage,
    decodes the canonical JSON, verifies version ID/content hash/revision,
    and returns text, language evidence, pages, and validated block spans.
    Fails as a ``NormalizationIntegrityError`` when normalization is missing
    or malformed.
    """

    def __init__(self, *, storage: NormalizedStorageReader) -> None:
        self._storage = storage

    async def load(
        self,
        *,
        normalized_object_key: str | None,
        expected_version_id: uuid.UUID,
        expected_content_sha256: str,
        expected_normalization_revision: str | None = None,
    ) -> NormalizeResult:
        """Load, verify, and return the canonical normalized representation.

        T5.2-01: Cross-checks ``expected_normalization_revision`` when provided.
        T5.2-02: Requires ``text`` and ``blocks`` instead of defaulting to empty.
        T5.2-03: Validates structural types for pages, offset_map,
        language_evidence, and parser_attempts.
        """
        if not normalized_object_key:
            raise NormalizationIntegrityError("No normalized_object_key set on the document version.")

        try:
            raw = await self._storage.read_bytes(normalized_object_key)
        except Exception as exc:
            raise NormalizationIntegrityError(f"Cannot read normalized artifact at '{normalized_object_key}'.") from exc

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NormalizationIntegrityError("Normalized artifact contains invalid JSON.") from exc

        if not isinstance(payload, dict):
            raise NormalizationIntegrityError("Normalized artifact is not a JSON object.")

        # Verify version ID
        stored_version_id = payload.get("version_id")
        if stored_version_id != str(expected_version_id):
            raise NormalizationIntegrityError(
                f"Version ID mismatch: expected '{expected_version_id}', got '{stored_version_id}'."
            )

        # T5.2-02: Require text — reject missing or non-string values.
        text = payload.get("text")
        if text is None or not isinstance(text, str):
            raise NormalizationIntegrityError("Missing or invalid 'text' in normalized artifact.")
        if not text:
            raise NormalizationIntegrityError("Normalized artifact text is empty.")

        # Verify content hash
        actual_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        stored_sha256 = payload.get("content_sha256", "")
        if actual_sha256 != stored_sha256:
            raise NormalizationIntegrityError("Content SHA-256 does not match the stored text.")
        if stored_sha256 != expected_content_sha256:
            raise NormalizationIntegrityError("Content SHA-256 does not match the expected value from the version.")

        # Verify normalization revision
        normalization_revision = payload.get("normalization_revision")
        if not normalization_revision or not isinstance(normalization_revision, str):
            raise NormalizationIntegrityError("Missing or invalid normalization_revision.")

        # T5.2-01: Cross-check normalization revision against version metadata.
        if expected_normalization_revision is not None and normalization_revision != expected_normalization_revision:
            raise NormalizationIntegrityError(
                f"Normalization revision mismatch: expected '{expected_normalization_revision}', "
                f"got '{normalization_revision}'."
            )

        # T5.2-02: Require blocks — reject missing values.
        raw_blocks = payload.get("blocks")
        if raw_blocks is None:
            raise NormalizationIntegrityError("Missing 'blocks' in normalized artifact.")
        if not isinstance(raw_blocks, list):
            raise NormalizationIntegrityError("Blocks must be a list.")

        blocks = _validate_block_records(raw_blocks, len(text))

        # T5.2-03: Validate structural types for canonical fields.
        pages = payload.get("pages")
        if pages is None or not isinstance(pages, list):
            raise NormalizationIntegrityError("Missing or invalid 'pages' in normalized artifact.")
        offset_map = payload.get("offset_map")
        if offset_map is None or not isinstance(offset_map, list):
            raise NormalizationIntegrityError("Missing or invalid 'offset_map' in normalized artifact.")
        language_evidence = payload.get("language_evidence")
        if language_evidence is None or not isinstance(language_evidence, list):
            raise NormalizationIntegrityError("Missing or invalid 'language_evidence' in normalized artifact.")
        parser_attempts = payload.get("parser_attempts")
        if parser_attempts is None or not isinstance(parser_attempts, list):
            raise NormalizationIntegrityError("Missing or invalid 'parser_attempts' in normalized artifact.")

        return NormalizeResult(
            version_id=str(expected_version_id),
            normalization_revision=normalization_revision,
            text=text,
            pages=pages,
            offset_map=offset_map,
            language_evidence=language_evidence,
            content_sha256=stored_sha256,
            parser_attempts=parser_attempts,
            blocks=blocks,
            normalized_object_key=normalized_object_key,
        )


# ---------------------------------------------------------------------------
# Block derivation from normalized pages
# ---------------------------------------------------------------------------


def _derive_block_records(
    normalized_text: str,
    pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derive ordered block records with document-global normalized offsets.

    Walks each page's blocks in reading order.  For each block, locates the
    NFC-normalized block text within the global normalized text starting
    from the running cursor, then records its global start/end offsets.

    Raises ``ValueError`` for unresolvable or overlapping parser spans.
    """
    blocks: list[dict[str, Any]] = []
    cursor = 0
    block_counter = 0

    for page_index, page in enumerate(pages):
        page_number = page.get("page_number", page_index + 1)
        section_path = page.get("section_path", [])
        if isinstance(section_path, str):
            section_path = [section_path] if section_path else []

        for block in page.get("blocks", []):
            block_text = str(block.get("text", ""))
            if not block_text.strip():
                continue

            # Find block text in the global normalized text
            position = normalized_text.find(block_text, cursor)
            if position < 0:
                raise ValueError(
                    f"Block text not found in normalized text at or after offset {cursor}: '{block_text[:80]}...'"
                )
            if position < cursor:
                raise ValueError(f"Overlapping block span: block starts at {position} but cursor is at {cursor}.")

            block_id = block.get("block_id", f"b-{block_counter}")
            block_section = block.get("section_path", section_path)
            if isinstance(block_section, str):
                block_section = [block_section] if block_section else []

            blocks.append(
                {
                    "block_id": block_id,
                    "page_number": page_number,
                    "section_path": list(block_section),
                    "start_offset": position,
                    "end_offset": position + len(block_text),
                }
            )
            cursor = position + len(block_text)
            block_counter += 1

    return blocks


# ---------------------------------------------------------------------------
# Block validation for loaded artifacts
# ---------------------------------------------------------------------------


def _validate_block_records(
    raw_blocks: list[Any],
    text_length: int,
) -> list[dict[str, Any]]:
    """Validate block records from a loaded normalized artifact.

    Checks ordering, bounds, and non-overlapping constraints.
    """
    blocks: list[dict[str, Any]] = []
    previous_end = 0

    for index, raw in enumerate(raw_blocks):
        if not isinstance(raw, dict):
            raise NormalizationIntegrityError(f"Block {index} is not a dict.")

        block_id = raw.get("block_id")
        if not block_id:
            raise NormalizationIntegrityError(f"Block {index} missing block_id.")

        page_number = raw.get("page_number")
        if not isinstance(page_number, int) or page_number < 1:
            raise NormalizationIntegrityError(f"Block '{block_id}' has invalid page_number: {page_number}.")

        start_offset = raw.get("start_offset")
        end_offset = raw.get("end_offset")
        if not isinstance(start_offset, int) or not isinstance(end_offset, int):
            raise NormalizationIntegrityError(f"Block '{block_id}' has non-integer offsets.")
        if start_offset < 0 or end_offset < 0:
            raise NormalizationIntegrityError(f"Block '{block_id}' has negative offsets.")
        if start_offset >= end_offset:
            raise NormalizationIntegrityError(f"Block '{block_id}' has start_offset >= end_offset.")
        if end_offset > text_length:
            raise NormalizationIntegrityError(
                f"Block '{block_id}' end_offset ({end_offset}) exceeds text length ({text_length})."
            )
        if start_offset < previous_end:
            raise NormalizationIntegrityError(
                f"Block '{block_id}' overlaps with previous block (start={start_offset}, previous_end={previous_end})."
            )

        section_path = raw.get("section_path", [])
        if not isinstance(section_path, list):
            raise NormalizationIntegrityError(f"Block '{block_id}' has non-list section_path.")

        blocks.append(
            {
                "block_id": block_id,
                "page_number": page_number,
                "section_path": section_path,
                "start_offset": start_offset,
                "end_offset": end_offset,
            }
        )
        previous_end = end_offset

    return blocks


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _normalize_page(page: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(page)
    normalized["text"], _ = _normalize_text(str(page.get("text", "")))
    normalized_blocks: list[dict[str, Any]] = []
    for block in page.get("blocks", []):
        value = dict(block)
        value["text"], _ = _normalize_text(str(block.get("text", "")))
        normalized_blocks.append(value)
    normalized["blocks"] = normalized_blocks
    normalized.setdefault("tables", [])
    return normalized


def _normalize_text(value: str) -> tuple[str, list[dict[str, int]]]:
    """NFC-normalize while building a source-character-accurate offset map.

    Phase 1 filters control characters (except \\t, \\n, \\r), recording which
    source positions were removed.  Phase 2 applies NFC and maps filtered
    positions to their post-composition offsets so downstream chunks can
    prove exact source character provenance.
    """
    # Phase 1: Filter controls, tracking source→filtered position.
    filtered_chars: list[str] = []
    source_to_filtered: list[int] = []
    filtered_pos = 0
    for character in value:
        if unicodedata.category(character) == "Cc" and character not in "\t\n\r":
            source_to_filtered.append(-1)  # removed
        else:
            source_to_filtered.append(filtered_pos)
            filtered_chars.append(character)
            filtered_pos += 1
    source_to_filtered.append(filtered_pos)  # sentinel for end-of-string

    filtered = "".join(filtered_chars)
    normalized = unicodedata.normalize("NFC", filtered)

    # Phase 2: Map filtered code-point boundaries to NFC boundaries.
    # Expand both filtered and normalized through NFD to find a common
    # decomposed representation, then walk forward through both.
    filtered_nfd = unicodedata.normalize("NFD", filtered)
    normalized_nfd = unicodedata.normalize("NFD", normalized)

    # Build filtered code-point index → NFD code-point index.
    filtered_to_nfd: list[int] = []
    nfd_pos = 0
    for character in filtered:
        filtered_to_nfd.append(nfd_pos)
        nfd_len = len(unicodedata.normalize("NFD", character))
        nfd_pos += nfd_len
    filtered_to_nfd.append(nfd_pos)

    # Build NFD code-point index → NFC code-point index.
    nfc_to_nfd: list[int] = []
    nfd_pos = 0
    for character in normalized:
        nfc_to_nfd.append(nfd_pos)
        nfd_len = len(unicodedata.normalize("NFD", character))
        nfd_pos += nfd_len
    nfc_to_nfd.append(nfd_pos)

    def _nfd_to_nfc(nfd_idx: int) -> int:
        """Find the NFC position corresponding to an NFD index."""
        # Binary search for the largest NFC position whose NFD start <= nfd_idx.
        lo, hi = 0, len(nfc_to_nfd) - 1
        result = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if nfc_to_nfd[mid] <= nfd_idx:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return result

    # Build the full source→normalized offset map.
    offsets: list[dict[str, int]] = []
    for source_idx in range(len(value) + 1):
        filt_idx = source_to_filtered[source_idx]
        if filt_idx < 0:
            # Source character was removed; map to the same normalized
            # position as the next surviving source character.
            offsets.append({"source_offset": source_idx, "normalized_offset": -1})
        else:
            nfd_idx = filtered_to_nfd[min(filt_idx, len(filtered))]
            nfc_idx = _nfd_to_nfc(nfd_idx)
            offsets.append({"source_offset": source_idx, "normalized_offset": min(nfc_idx, len(normalized))})

    # Backfill removed positions: point to the next valid normalized offset.
    last_valid = len(normalized)
    for i in range(len(offsets) - 1, -1, -1):
        if offsets[i]["normalized_offset"] < 0:
            offsets[i]["normalized_offset"] = last_valid
        else:
            last_valid = offsets[i]["normalized_offset"]

    return normalized, offsets


def _language_evidence(text: str) -> list[dict[str, float | str]]:
    """Provide deterministic minimal language evidence without model inference."""
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return [{"language": "und", "confidence": 1.0}]
    latin = sum("LATIN" in unicodedata.name(character, "") for character in letters)
    if latin / len(letters) >= 0.8:
        return [{"language": "en", "confidence": round(latin / len(letters), 2)}]
    return [{"language": "und", "confidence": 1.0}]
