"""Deterministic chunk construction from canonical normalized text."""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from documind.models.chunk import DocumentChunk


class ChunkingError(ValueError):
    """Raised when a pinned chunk profile or normalized artifact is unsafe to use."""


class EmbeddingDependencyError(RuntimeError):
    """Signals that vector chunking produced no usable sentence embeddings."""


@dataclass(frozen=True)
class Token:
    """One tokenizer token and its immutable normalized-text character span."""

    token_id: int
    start_offset: int
    end_offset: int


class Tokenizer(Protocol):
    """The digest-pinned tokenizer interface required by a chunk profile."""

    digest: str

    def tokenize(self, text: str) -> list[Token]:
        """Return tokens in source order with non-overlapping text offsets."""


class SentenceSegmenter(Protocol):
    """Produce ordered sentence spans against the canonical normalized text."""

    def segment(self, text: str) -> list[tuple[int, int]]:
        """Return non-overlapping start/end offsets for each sentence."""


class SentenceEmbedder(Protocol):
    """Embed the exact sentence strings using the profile's pinned model path."""

    def embed(self, sentences: list[str]) -> list[Sequence[float]]:
        """Return one vector for every supplied sentence in the same order."""


@dataclass(frozen=True)
class NormalizedBlock:
    """A reading-order block persisted by normalization with global offsets."""

    block_id: str
    page_number: int
    section_path: tuple[str, ...]
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class NormalizedDocument:
    """Canonical text and block provenance loaded from the normalized artifact."""

    version_id: uuid.UUID
    text: str
    blocks: tuple[NormalizedBlock, ...]


@dataclass(frozen=True)
class ChunkProfile:
    """The immutable profile values needed before a chunk writer persists rows."""

    revision_id: uuid.UUID
    strategy: str
    tokenizer_digest: str
    target_tokens: int
    overlap_tokens: int
    embedding_model_digest: str
    active: bool = True
    min_tokens: int = 1
    max_tokens: int | None = None
    vector_similarity_threshold: float = 0.45
    recursive_fallback_revision_id: uuid.UUID | None = None


class ChunkingService:
    """Create reproducible chunks; callers supply only pinned, validated inputs."""

    def __init__(
        self,
        *,
        tokenizer: Tokenizer,
        sentence_segmenter: SentenceSegmenter | None = None,
        sentence_embedder: SentenceEmbedder | None = None,
        fallback_profiles: Mapping[uuid.UUID, ChunkProfile] | None = None,
    ) -> None:
        self._tokenizer = tokenizer
        self._sentence_segmenter = sentence_segmenter
        self._sentence_embedder = sentence_embedder
        self._fallback_profiles = dict(fallback_profiles or {})

    def chunk(self, normalized: NormalizedDocument, profile: ChunkProfile) -> list[DocumentChunk]:
        """Split canonical text using a pinned strategy and preserve provenance."""
        self._validate_normalized_document(normalized)
        self._validate_profile(profile)
        tokens = self._tokenizer.tokenize(normalized.text)
        self._validate_tokens(tokens, normalized.text)
        if not tokens:
            return []

        effective_profile = profile
        if effective_profile.strategy == "fixed":
            spans = self._fixed_spans(tokens, effective_profile)
        elif effective_profile.strategy == "recursive":
            spans = self._recursive_spans(normalized.text, tokens, effective_profile)
        elif effective_profile.strategy == "paragraph":
            spans = self._paragraph_spans(normalized, tokens, effective_profile)
        elif effective_profile.strategy == "vector":
            try:
                spans = self._vector_spans(normalized.text, tokens, effective_profile)
            except EmbeddingDependencyError:
                effective_profile = self._resolve_vector_fallback(effective_profile)
                spans = self._recursive_spans(normalized.text, tokens, effective_profile)
        else:
            raise ChunkingError(f"Chunk strategy {effective_profile.strategy!r} is not implemented.")

        text_sha256 = hashlib.sha256(normalized.text.encode("utf-8")).hexdigest()
        chunks: list[DocumentChunk] = []
        for start_offset, end_offset, token_count in spans:
            start_offset, end_offset = self._trim_separator_offsets(
                normalized.text,
                start_offset,
                end_offset,
            )
            if start_offset < end_offset:
                chunks.append(
                    self._build_chunk(
                        normalized=normalized,
                        profile=effective_profile,
                        index=len(chunks),
                        text_sha256=text_sha256,
                        start_offset=start_offset,
                        end_offset=end_offset,
                        token_count=token_count,
                    ),
                )
        return chunks

    def _resolve_vector_fallback(self, requested_profile: ChunkProfile) -> ChunkProfile:
        revision_id = requested_profile.recursive_fallback_revision_id
        if revision_id is None:
            raise ChunkingError("The vector profile has no explicit recursive fallback revision.")
        fallback = self._fallback_profiles.get(revision_id)
        if fallback is None:
            raise ChunkingError("The configured recursive fallback revision is unavailable.")
        self._validate_profile(fallback)
        if fallback.strategy != "recursive":
            raise ChunkingError("The configured vector fallback must use the recursive strategy.")
        return fallback

    @staticmethod
    def _fixed_spans(tokens: list[Token], profile: ChunkProfile) -> list[tuple[int, int, int]]:
        spans: list[tuple[int, int, int]] = []
        cursor = 0
        while cursor < len(tokens):
            token_end = min(cursor + profile.target_tokens, len(tokens))
            spans.append(
                (
                    tokens[cursor].start_offset,
                    tokens[token_end - 1].end_offset,
                    token_end - cursor,
                ),
            )
            if token_end == len(tokens):
                break
            cursor = token_end - profile.overlap_tokens
        return spans

    def _recursive_spans(
        self,
        text: str,
        tokens: list[Token],
        profile: ChunkProfile,
    ) -> list[tuple[int, int, int]]:
        units = self._recursive_units(
            text,
            tokens,
            0,
            len(text),
            profile.target_tokens,
            ("\n\n", "\n", ". ", "! ", "? ", " "),
        )
        return self._pack_units(units, tokens, profile)

    def _paragraph_spans(
        self,
        normalized: NormalizedDocument,
        tokens: list[Token],
        profile: ChunkProfile,
    ) -> list[tuple[int, int, int]]:
        """Pack persisted reading-order blocks, recursively splitting only oversized blocks."""
        maximum = profile.max_tokens or profile.target_tokens
        spans: list[tuple[int, int, int]] = []
        buffered_start: int | None = None
        buffered_end: int | None = None

        def flush_buffer() -> None:
            nonlocal buffered_start, buffered_end
            if buffered_start is None or buffered_end is None:
                return
            spans.append(
                (
                    buffered_start,
                    buffered_end,
                    self._span_token_count(tokens, buffered_start, buffered_end),
                ),
            )
            buffered_start = None
            buffered_end = None

        for block in normalized.blocks:
            block_tokens = self._span_token_count(tokens, block.start_offset, block.end_offset)
            if block_tokens > maximum:
                flush_buffer()
                units = self._recursive_units(
                    normalized.text,
                    tokens,
                    block.start_offset,
                    block.end_offset,
                    maximum,
                    ("\n", ". ", "! ", "? ", " "),
                )
                spans.extend(
                    (
                        start_offset,
                        end_offset,
                        self._span_token_count(tokens, start_offset, end_offset),
                    )
                    for start_offset, end_offset in units
                )
                continue
            if buffered_start is None or buffered_end is None:
                buffered_start, buffered_end = block.start_offset, block.end_offset
                continue
            if self._span_token_count(tokens, buffered_start, block.end_offset) <= maximum:
                buffered_end = block.end_offset
                continue
            flush_buffer()
            buffered_start, buffered_end = block.start_offset, block.end_offset
        flush_buffer()
        return spans

    def _vector_spans(
        self,
        text: str,
        tokens: list[Token],
        profile: ChunkProfile,
    ) -> list[tuple[int, int, int]]:
        """Split at low adjacent-sentence similarity before bounded packing."""
        if self._sentence_segmenter is None or self._sentence_embedder is None:
            raise ChunkingError("Vector chunking requires pinned sentence segmentation and embedding dependencies.")
        sentence_spans = self._sentence_segmenter.segment(text)
        self._validate_sentence_spans(sentence_spans, text)
        if not sentence_spans:
            return []
        sentences = [text[start_offset:end_offset] for start_offset, end_offset in sentence_spans]
        embeddings = self._sentence_embedder.embed(sentences)
        if len(embeddings) != len(sentence_spans):
            raise ChunkingError("The sentence embedder did not return one vector per sentence.")

        semantic_units: list[tuple[int, int]] = []
        unit_start, unit_end = sentence_spans[0]
        for index in range(len(sentence_spans) - 1):
            similarity = self._cosine_similarity(embeddings[index], embeddings[index + 1])
            next_start, next_end = sentence_spans[index + 1]
            if similarity < profile.vector_similarity_threshold:
                semantic_units.append((unit_start, unit_end))
                unit_start, unit_end = next_start, next_end
            else:
                unit_end = next_end
        semantic_units.append((unit_start, unit_end))

        # Enforce min/max token bounds on semantic units
        min_tokens = profile.min_tokens
        max_tokens = profile.max_tokens or profile.target_tokens
        bounded_units = self._enforce_token_bounds(
            semantic_units,
            text,
            tokens,
            min_tokens,
            max_tokens,
            profile.target_tokens,
        )
        return self._pack_units(bounded_units, tokens, profile)

    def _recursive_units(
        self,
        text: str,
        tokens: list[Token],
        start_offset: int,
        end_offset: int,
        target_tokens: int,
        separators: tuple[str, ...],
    ) -> list[tuple[int, int]]:
        if self._span_token_count(tokens, start_offset, end_offset) <= target_tokens:
            return [(start_offset, end_offset)]
        if separators:
            separator = separators[0]
            pieces = self._split_on_separator(text, start_offset, end_offset, separator)
            if len(pieces) > 1:
                return [
                    nested
                    for piece_start, piece_end in pieces
                    for nested in self._recursive_units(
                        text,
                        tokens,
                        piece_start,
                        piece_end,
                        target_tokens,
                        separators[1:],
                    )
                ]
            return self._recursive_units(text, tokens, start_offset, end_offset, target_tokens, separators[1:])

        bounded_tokens = [
            token for token in tokens if token.start_offset >= start_offset and token.end_offset <= end_offset
        ]
        return [
            (
                bounded_tokens[index].start_offset,
                bounded_tokens[min(index + target_tokens, len(bounded_tokens)) - 1].end_offset,
            )
            for index in range(0, len(bounded_tokens), target_tokens)
        ]

    @staticmethod
    def _split_on_separator(text: str, start_offset: int, end_offset: int, separator: str) -> list[tuple[int, int]]:
        pieces: list[tuple[int, int]] = []
        cursor = start_offset
        while cursor < end_offset:
            boundary = text.find(separator, cursor, end_offset)
            if boundary == -1:
                if cursor < end_offset:
                    pieces.append((cursor, end_offset))
                break
            if cursor < boundary:
                pieces.append((cursor, boundary))
            cursor = boundary + len(separator)
        return pieces

    def _pack_units(
        self,
        units: list[tuple[int, int]],
        tokens: list[Token],
        profile: ChunkProfile,
    ) -> list[tuple[int, int, int]]:
        if not units:
            return []
        spans: list[tuple[int, int, int]] = []
        current_start, current_end = units[0]
        for unit_start, unit_end in units[1:]:
            if self._span_token_count(tokens, current_start, unit_end) <= profile.target_tokens:
                current_end = unit_end
                continue
            spans.append((current_start, current_end, self._span_token_count(tokens, current_start, current_end)))
            current_start, current_end = (
                self._overlap_start(
                    tokens,
                    current_end,
                    unit_start,
                    profile.overlap_tokens,
                ),
                unit_end,
            )
        spans.append((current_start, current_end, self._span_token_count(tokens, current_start, current_end)))
        return spans

    @staticmethod
    def _span_token_count(tokens: list[Token], start_offset: int, end_offset: int) -> int:
        return sum(token.start_offset >= start_offset and token.end_offset <= end_offset for token in tokens)

    @staticmethod
    def _validate_sentence_spans(sentence_spans: list[tuple[int, int]], text: str) -> None:
        previous_end = 0
        for start_offset, end_offset in sentence_spans:
            if start_offset < previous_end or end_offset <= start_offset or end_offset > len(text):
                raise ChunkingError("Sentence segmentation returned invalid canonical-text offsets.")
            previous_end = end_offset

    @staticmethod
    def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right) or not left:
            raise ChunkingError("Sentence embedding vectors must be non-empty and have matching dimensions.")
        numerator = sum(left_value * right_value for left_value, right_value in zip(left, right, strict=True))
        left_magnitude = math.sqrt(sum(value * value for value in left))
        right_magnitude = math.sqrt(sum(value * value for value in right))
        if left_magnitude == 0 or right_magnitude == 0:
            raise ChunkingError("Sentence embedding vectors must have non-zero magnitude.")
        return numerator / (left_magnitude * right_magnitude)

    @staticmethod
    def _overlap_start(
        tokens: list[Token],
        current_end: int,
        next_unit_start: int,
        overlap_tokens: int,
    ) -> int:
        if overlap_tokens == 0:
            return next_unit_start
        preceding = [token for token in tokens if token.end_offset <= current_end]
        return preceding[max(0, len(preceding) - overlap_tokens)].start_offset

    def _validate_profile(self, profile: ChunkProfile) -> None:
        if not profile.active:
            raise ChunkingError("The selected chunk profile is inactive.")
        if profile.strategy not in {"fixed", "recursive", "vector", "paragraph"}:
            raise ChunkingError("The selected chunk profile has an unknown strategy.")
        if profile.tokenizer_digest != self._tokenizer.digest:
            raise ChunkingError("The selected chunk profile does not match the pinned tokenizer.")
        if profile.target_tokens <= 0:
            raise ChunkingError("The selected chunk profile must have a positive target token count.")
        if profile.overlap_tokens < 0 or profile.overlap_tokens >= profile.target_tokens:
            raise ChunkingError("Chunk overlap must be non-negative and smaller than the target size.")
        max_tokens = profile.max_tokens if profile.max_tokens is not None else profile.target_tokens
        if profile.min_tokens <= 0 or profile.min_tokens > max_tokens:
            raise ChunkingError("The selected chunk profile has invalid token bounds.")
        if not profile.embedding_model_digest:
            raise ChunkingError("The selected chunk profile has no embedding model digest.")
        if profile.strategy == "vector" and profile.recursive_fallback_revision_id is None:
            raise ChunkingError("A vector profile must have a recursive_fallback_revision_id.")

    @staticmethod
    def _validate_normalized_document(normalized: NormalizedDocument) -> None:
        previous_end = 0
        for block in normalized.blocks:
            if (
                not block.block_id
                or block.page_number < 1
                or block.start_offset < previous_end
                or block.end_offset <= block.start_offset
                or block.end_offset > len(normalized.text)
            ):
                raise ChunkingError("Normalized block provenance is malformed or out of reading order.")
            previous_end = block.end_offset

    @staticmethod
    def _validate_tokens(tokens: list[Token], text: str) -> None:
        previous_end = 0
        for token in tokens:
            if (
                token.start_offset < previous_end
                or token.end_offset <= token.start_offset
                or token.end_offset > len(text)
            ):
                raise ChunkingError("Tokenizer offsets are malformed or out of source order.")
            previous_end = token.end_offset

    @staticmethod
    def _trim_separator_offsets(text: str, start_offset: int, end_offset: int) -> tuple[int, int]:
        while start_offset < end_offset and text[start_offset].isspace():
            start_offset += 1
        while end_offset > start_offset and text[end_offset - 1].isspace():
            end_offset -= 1
        return start_offset, end_offset

    @staticmethod
    def _build_chunk(
        *,
        normalized: NormalizedDocument,
        profile: ChunkProfile,
        index: int,
        text_sha256: str,
        start_offset: int,
        end_offset: int,
        token_count: int,
    ) -> DocumentChunk:
        covered_blocks = [
            block for block in normalized.blocks if block.start_offset < end_offset and block.end_offset > start_offset
        ]
        page_start = min((block.page_number for block in covered_blocks), default=None)
        page_end = max((block.page_number for block in covered_blocks), default=None)
        name = ":".join(
            [
                str(profile.revision_id),
                str(index),
                text_sha256,
                str(start_offset),
                str(end_offset),
                str(page_start or ""),
                str(page_end or ""),
            ],
        )
        content = normalized.text[start_offset:end_offset]
        return DocumentChunk(
            id=uuid.uuid5(normalized.version_id, name),
            version_id=normalized.version_id,
            chunk_index=index,
            content=content,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            start_offset=start_offset,
            end_offset=end_offset,
            page_start=page_start,
            page_end=page_end,
            section_path=list(covered_blocks[0].section_path) if covered_blocks else [],
            block_ids=tuple(block.block_id for block in covered_blocks),
            token_count=token_count,
            profile_revision_id=profile.revision_id,
            embedding_model_digest=profile.embedding_model_digest,
        )

    def _enforce_token_bounds(
        self,
        units: list[tuple[int, int]],
        text: str,
        tokens: list[Token],
        min_tokens: int,
        max_tokens: int,
        target_tokens: int,
    ) -> list[tuple[int, int]]:
        """Merge short and split long semantic units to enforce token bounds."""
        result: list[tuple[int, int]] = []
        for start_offset, end_offset in units:
            count = self._span_token_count(tokens, start_offset, end_offset)
            if count > max_tokens:
                # Recursively split oversized units
                sub_units = self._recursive_units(
                    text,
                    tokens,
                    start_offset,
                    end_offset,
                    max_tokens,
                    ("\n", ". ", "! ", "? ", " "),
                )
                result.extend(sub_units)
            elif count < min_tokens and result:
                # Attach short units to previous
                prev_start, _ = result[-1]
                result[-1] = (prev_start, end_offset)
            else:
                result.append((start_offset, end_offset))
        return result


# ---------------------------------------------------------------------------
# Chunk writer protocol and durable writer
# ---------------------------------------------------------------------------


class ChunkWriter(Protocol):
    """Persist immutable chunks idempotently within a transactional boundary."""

    async def write_chunks(
        self,
        version_id: uuid.UUID,
        profile_revision_id: uuid.UUID,
        chunks: list[DocumentChunk],
    ) -> dict[str, Any]:
        """Write chunks, returning stage output metadata.

        An identical retry returns the existing rows unchanged.
        A same-ordinal or same-span mismatch raises ``ChunkWriterConflictError``.
        """


class ChunkWriterConflictError(RuntimeError):
    """A retry produced chunks that conflict with previously persisted rows."""
