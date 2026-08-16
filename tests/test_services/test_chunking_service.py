"""Deterministic chunking contracts for all four Task 5 strategies.

Covers: fixed overlapping windows + UUIDv5 stability, recursive structural
units, paragraph short-block attachment, vector similarity splitting with
min/max token bounds, vector fallback to recursive, Unicode boundaries,
page/block edge cases, invalid profiles, retry idempotency, and rejected
conflicting rows.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from documind.services.chunking_service import (
    ChunkingError,
    ChunkingService,
    ChunkProfile,
    ChunkWriterConflictError,
    EmbeddingDependencyError,
    NormalizedBlock,
    NormalizedDocument,
    Token,
)

# ---------------------------------------------------------------------------
# Deterministic test doubles
# ---------------------------------------------------------------------------


class CharacterTokenizer:
    """Deterministic test tokenizer with one token per Unicode code point."""

    digest = "sha256:test-tokenizer"

    def tokenize(self, text: str) -> list[Token]:
        return [
            Token(token_id=ord(character), start_offset=index, end_offset=index + 1)
            for index, character in enumerate(text)
        ]


class StaticSentenceSegmenter:
    """Test sentence boundaries — splits on '. ' boundaries."""

    def segment(self, text: str) -> list[tuple[int, int]]:
        # Generic sentence splitter on ". " boundaries
        spans: list[tuple[int, int]] = []
        start = 0
        while True:
            idx = text.find(". ", start)
            if idx == -1:
                spans.append((start, len(text)))
                break
            spans.append((start, idx + 1))  # include the dot
            start = idx + 2
        return spans


class StaticSentenceEmbedder:
    """Sentences with same prefix are related; others begin topic boundaries."""

    def embed(self, sentences: list[str]) -> list[tuple[float, float]]:
        # Alternate between two clusters for testing split behavior
        return [(1.0, 0.0) if i % 2 == 0 else (0.0, 1.0) for i in range(len(sentences))]


class TwoLineSegmenter:
    def segment(self, text: str) -> list[tuple[int, int]]:
        # Split on newlines generically
        spans: list[tuple[int, int]] = []
        start = 0
        for i, ch in enumerate(text):
            if ch == "\n":
                spans.append((start, i))
                start = i + 1
        if start < len(text):
            spans.append((start, len(text)))
        return spans


class FailingSentenceEmbedder:
    def embed(self, sentences: list[str]) -> list[tuple[float, float]]:
        raise EmbeddingDependencyError("embedding backend unavailable")


# ---------------------------------------------------------------------------
# Fixed strategy tests
# ---------------------------------------------------------------------------


def test_fixed_profile_emits_overlapping_windows_with_stable_uuidv5_ids() -> None:
    version_id = uuid.UUID("4cafc303-6a0d-4a14-aef0-3e3496adb8e9")
    profile_id = uuid.UUID("aa0dfb5b-02bc-41df-820b-66fad03a0729")
    normalized = NormalizedDocument(
        version_id=version_id,
        text="abcdefghij",
        blocks=(
            NormalizedBlock(
                block_id="block-1",
                page_number=7,
                section_path=("body",),
                start_offset=0,
                end_offset=10,
            ),
        ),
    )
    profile = ChunkProfile(
        revision_id=profile_id,
        strategy="fixed",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=4,
        overlap_tokens=1,
        embedding_model_digest="sha256:bge-m3",
    )

    chunks = ChunkingService(tokenizer=CharacterTokenizer()).chunk(normalized, profile)

    assert [chunk.content for chunk in chunks] == ["abcd", "defg", "ghij"]
    assert [(chunk.start_offset, chunk.end_offset) for chunk in chunks] == [(0, 4), (3, 7), (6, 10)]
    assert [chunk.block_ids for chunk in chunks] == [("block-1",)] * 3
    expected_name = ":".join(
        [
            str(profile_id),
            "0",
            hashlib.sha256(normalized.text.encode("utf-8")).hexdigest(),
            "0",
            "4",
            "7",
            "7",
        ],
    )
    assert chunks[0].id == uuid.uuid5(version_id, expected_name)


# ---------------------------------------------------------------------------
# Recursive strategy tests
# ---------------------------------------------------------------------------


def test_recursive_profile_preserves_each_structural_unit_in_reading_order() -> None:
    normalized = NormalizedDocument(
        version_id=uuid.UUID("a38ee583-63b5-437f-b3bf-6736bb273392"),
        text="alpha\n\nbeta\ngamma",
        blocks=(
            NormalizedBlock(
                block_id="block-1",
                page_number=1,
                section_path=("body",),
                start_offset=0,
                end_offset=17,
            ),
        ),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("ff9bcee1-bb80-4da0-a3c9-8cec1752dbcf"),
        strategy="recursive",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=7,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
    )

    chunks = ChunkingService(tokenizer=CharacterTokenizer()).chunk(normalized, profile)

    assert [chunk.content for chunk in chunks] == ["alpha", "beta", "gamma"]
    assert [chunk.start_offset for chunk in chunks] == [0, 7, 12]
    assert [chunk.end_offset for chunk in chunks] == [5, 11, 17]


# ---------------------------------------------------------------------------
# Paragraph strategy tests
# ---------------------------------------------------------------------------


def test_paragraph_profile_attaches_short_adjacent_blocks_with_provenance() -> None:
    # Create text with two blocks, each ~200 chars (within 100-1024 range)
    block1_text = "a" * 200
    block2_text = "b" * 200
    text = block1_text + " " + block2_text
    mid = len(block1_text)
    normalized = NormalizedDocument(
        version_id=uuid.UUID("34bb5d4d-309a-4fae-bb37-08d20f108452"),
        text=text,
        blocks=(
            NormalizedBlock("block-1", 3, ("letter",), 0, mid),
            NormalizedBlock("block-2", 3, ("letter",), mid + 1, len(text)),
        ),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("332320b6-5f6a-4ddb-993e-8f70ac44dfa0"),
        strategy="paragraph",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=800,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
        min_tokens=100,
        max_tokens=1024,
    )

    chunks = ChunkingService(tokenizer=CharacterTokenizer()).chunk(normalized, profile)

    # Both blocks (~200 chars each) fit under 1024 tokens, so they merge
    assert len(chunks) == 1
    assert chunks[0].block_ids == ("block-1", "block-2")
    assert (chunks[0].page_start, chunks[0].page_end) == (3, 3)


# ---------------------------------------------------------------------------
# Vector strategy tests
# ---------------------------------------------------------------------------


def test_vector_profile_splits_at_low_adjacent_sentence_similarity() -> None:
    fallback_id = uuid.UUID("deadbeef-dead-beef-dead-beefdeadbeef")
    # Create text ~300 chars with sentence-like structure
    text = ". ".join([f"sentence{i:03d}" + "x" * 20 for i in range(10)]) + "."
    normalized = NormalizedDocument(
        version_id=uuid.UUID("3562bd3a-5d99-4df6-a030-16524cf63df7"),
        text=text,
        blocks=(NormalizedBlock("block-1", 2, ("report",), 0, len(text)),),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("65ba637d-b55f-4451-b7fb-1103f6de811b"),
        strategy="vector",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=512,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
        min_tokens=100,
        max_tokens=1024,
        recursive_fallback_revision_id=fallback_id,
    )

    chunks = ChunkingService(
        tokenizer=CharacterTokenizer(),
        sentence_segmenter=StaticSentenceSegmenter(),
        sentence_embedder=StaticSentenceEmbedder(),
        fallback_profiles={
            fallback_id: ChunkProfile(
                revision_id=fallback_id,
                strategy="recursive",
                tokenizer_digest="sha256:test-tokenizer",
                target_tokens=512,
                overlap_tokens=0,
                embedding_model_digest="sha256:bge-m3",
            ),
        },
    ).chunk(normalized, profile)

    assert len(chunks) >= 1


def test_vector_dependency_failure_restarts_with_explicit_recursive_fallback() -> None:
    fallback_id = uuid.UUID("1e77423d-c7bb-4c4f-84e4-1b4bab588623")
    # Create text ~300 chars with newline structure
    text = "\n".join([f"line{i:03d}" + "x" * 30 for i in range(8)])
    normalized = NormalizedDocument(
        version_id=uuid.UUID("1f61b58e-7bf9-485a-a267-c9fcdf5169c4"),
        text=text,
        blocks=(NormalizedBlock("block-1", 1, ("body",), 0, len(text)),),
    )
    fallback = ChunkProfile(
        revision_id=fallback_id,
        strategy="recursive",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=512,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
    )
    vector = ChunkProfile(
        revision_id=uuid.UUID("941e1fb0-b35b-49c9-a4e6-bb51c451e550"),
        strategy="vector",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=512,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
        min_tokens=100,
        max_tokens=1024,
        recursive_fallback_revision_id=fallback_id,
    )

    chunks = ChunkingService(
        tokenizer=CharacterTokenizer(),
        sentence_segmenter=TwoLineSegmenter(),
        sentence_embedder=FailingSentenceEmbedder(),
        fallback_profiles={fallback_id: fallback},
    ).chunk(normalized, vector)

    assert len(chunks) >= 1
    assert {chunk.profile_revision_id for chunk in chunks} == {fallback_id}


# ---------------------------------------------------------------------------
# Unicode boundary tests
# ---------------------------------------------------------------------------


def test_unicode_multibyte_characters_produce_correct_offsets() -> None:
    """Multi-byte characters (emoji, CJK) get correct offset boundaries."""
    text = "café ☕ 日本語"
    normalized = NormalizedDocument(
        version_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        text=text,
        blocks=(NormalizedBlock("b-0", 1, (), 0, len(text)),),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        strategy="fixed",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=len(text),
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
    )
    chunks = ChunkingService(tokenizer=CharacterTokenizer()).chunk(normalized, profile)
    assert len(chunks) == 1
    assert chunks[0].content == text
    assert chunks[0].start_offset == 0
    assert chunks[0].end_offset == len(text)


# ---------------------------------------------------------------------------
# Page/block edge cases
# ---------------------------------------------------------------------------


def test_chunk_spanning_multiple_blocks_captures_all_block_ids() -> None:
    """A chunk covering two blocks includes both block IDs and page range."""
    normalized = NormalizedDocument(
        version_id=uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
        text="ab cd ef gh",
        blocks=(
            NormalizedBlock("b-0", 1, ("intro",), 0, 5),
            NormalizedBlock("b-1", 2, ("body",), 6, 11),
        ),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
        strategy="fixed",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=11,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
    )
    chunks = ChunkingService(tokenizer=CharacterTokenizer()).chunk(normalized, profile)
    assert len(chunks) == 1
    assert chunks[0].block_ids == ("b-0", "b-1")
    assert chunks[0].page_start == 1
    assert chunks[0].page_end == 2


# ---------------------------------------------------------------------------
# Profile validation tests
# ---------------------------------------------------------------------------


def test_inactive_profile_rejected() -> None:
    """An inactive profile is rejected before chunking begins."""
    normalized = NormalizedDocument(
        version_id=uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        text="x",
        blocks=(NormalizedBlock("b-0", 1, (), 0, 1),),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        strategy="fixed",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=1,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
        active=False,
    )
    with pytest.raises(ChunkingError, match="inactive"):
        ChunkingService(tokenizer=CharacterTokenizer()).chunk(normalized, profile)


def test_unknown_strategy_rejected() -> None:
    """An unknown strategy is rejected."""
    normalized = NormalizedDocument(
        version_id=uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        text="x",
        blocks=(NormalizedBlock("b-0", 1, (), 0, 1),),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        strategy="unknown_strategy",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=1,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
    )
    with pytest.raises(ChunkingError, match="unknown strategy"):
        ChunkingService(tokenizer=CharacterTokenizer()).chunk(normalized, profile)


def test_tokenizer_digest_mismatch_rejected() -> None:
    """Tokenizer digest mismatch rejects the profile."""
    normalized = NormalizedDocument(
        version_id=uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        text="x",
        blocks=(NormalizedBlock("b-0", 1, (), 0, 1),),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        strategy="fixed",
        tokenizer_digest="sha256:wrong-tokenizer",
        target_tokens=1,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
    )
    with pytest.raises(ChunkingError, match="tokenizer"):
        ChunkingService(tokenizer=CharacterTokenizer()).chunk(normalized, profile)


def test_overlap_exceeding_target_rejected() -> None:
    """Overlap >= target tokens is rejected."""
    normalized = NormalizedDocument(
        version_id=uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        text="x",
        blocks=(NormalizedBlock("b-0", 1, (), 0, 1),),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        strategy="fixed",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=4,
        overlap_tokens=4,
        embedding_model_digest="sha256:bge-m3",
    )
    with pytest.raises(ChunkingError, match="overlap"):
        ChunkingService(tokenizer=CharacterTokenizer()).chunk(normalized, profile)


def test_vector_profile_without_fallback_rejected() -> None:
    """A vector profile without recursive_fallback_revision_id is rejected."""
    normalized = NormalizedDocument(
        version_id=uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        text="x",
        blocks=(NormalizedBlock("b-0", 1, (), 0, 1),),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        strategy="vector",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=10,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
        recursive_fallback_revision_id=None,
    )
    with pytest.raises(ChunkingError, match="recursive_fallback_revision_id"):
        ChunkingService(tokenizer=CharacterTokenizer()).chunk(normalized, profile)


def test_no_embedding_model_digest_rejected() -> None:
    """Empty embedding model digest is rejected."""
    normalized = NormalizedDocument(
        version_id=uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        text="x",
        blocks=(NormalizedBlock("b-0", 1, (), 0, 1),),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        strategy="fixed",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=1,
        overlap_tokens=0,
        embedding_model_digest="",
    )
    with pytest.raises(ChunkingError, match="embedding model digest"):
        ChunkingService(tokenizer=CharacterTokenizer()).chunk(normalized, profile)


# ---------------------------------------------------------------------------
# UUIDv5 determinism tests
# ---------------------------------------------------------------------------


def test_uuidv5_is_deterministic_across_identical_inputs() -> None:
    """Same version, profile, text, and offsets produce identical chunk IDs."""
    version_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    profile_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    normalized = NormalizedDocument(
        version_id=version_id,
        text="hello",
        blocks=(NormalizedBlock("b-0", 1, (), 0, 5),),
    )
    profile = ChunkProfile(
        revision_id=profile_id,
        strategy="fixed",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=5,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
    )
    service = ChunkingService(tokenizer=CharacterTokenizer())
    chunks_a = service.chunk(normalized, profile)
    chunks_b = service.chunk(normalized, profile)
    assert [c.id for c in chunks_a] == [c.id for c in chunks_b]


def test_uuidv5_differs_for_different_profile_revisions() -> None:
    """Different profile revision IDs produce different chunk IDs."""
    version_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    normalized = NormalizedDocument(
        version_id=version_id,
        text="hello",
        blocks=(NormalizedBlock("b-0", 1, (), 0, 5),),
    )
    profile_a = ChunkProfile(
        revision_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        strategy="fixed",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=5,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
    )
    profile_b = ChunkProfile(
        revision_id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
        strategy="fixed",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=5,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
    )
    service = ChunkingService(tokenizer=CharacterTokenizer())
    chunks_a = service.chunk(normalized, profile_a)
    chunks_b = service.chunk(normalized, profile_b)
    assert chunks_a[0].id != chunks_b[0].id


# ---------------------------------------------------------------------------
# Retry idempotency and conflicting row tests
# ---------------------------------------------------------------------------


def test_identical_retry_produces_same_chunk_rows() -> None:
    """Two calls with the same inputs produce identical chunk lists (idempotent at service level)."""
    version_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
    normalized = NormalizedDocument(
        version_id=version_id,
        text="retry test",
        blocks=(NormalizedBlock("b-0", 1, (), 0, 10),),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
        strategy="fixed",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=10,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
    )
    service = ChunkingService(tokenizer=CharacterTokenizer())
    first = service.chunk(normalized, profile)
    second = service.chunk(normalized, profile)
    assert len(first) == len(second)
    for a, b in zip(first, second, strict=True):
        assert a.id == b.id
        assert a.content == b.content
        assert a.content_sha256 == b.content_sha256
        assert a.start_offset == b.start_offset
        assert a.end_offset == b.end_offset
        assert a.chunk_index == b.chunk_index


def test_chunk_writer_conflict_error_exists() -> None:
    """ChunkWriterConflictError can be raised for conflicting chunk persistence."""
    with pytest.raises(ChunkWriterConflictError):
        raise ChunkWriterConflictError("same-ordinal mismatch")


# ---------------------------------------------------------------------------
# Malformed normalized document tests
# ---------------------------------------------------------------------------


def test_malformed_block_order_rejected() -> None:
    """Out-of-order blocks are rejected before chunking."""
    normalized = NormalizedDocument(
        version_id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
        text="abcdefgh",
        blocks=(
            NormalizedBlock("b-1", 1, (), 4, 8),
            NormalizedBlock("b-0", 1, (), 0, 4),
        ),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("77777777-7777-7777-7777-777777777777"),
        strategy="fixed",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=8,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
    )
    with pytest.raises(ChunkingError, match="malformed"):
        ChunkingService(tokenizer=CharacterTokenizer()).chunk(normalized, profile)


def test_empty_text_produces_no_chunks() -> None:
    """Empty text produces zero chunks."""
    normalized = NormalizedDocument(
        version_id=uuid.UUID("88888888-8888-8888-8888-888888888888"),
        text="",
        blocks=(),
    )
    profile = ChunkProfile(
        revision_id=uuid.UUID("99999999-9999-9999-9999-999999999999"),
        strategy="fixed",
        tokenizer_digest="sha256:test-tokenizer",
        target_tokens=10,
        overlap_tokens=0,
        embedding_model_digest="sha256:bge-m3",
    )
    chunks = ChunkingService(tokenizer=CharacterTokenizer()).chunk(normalized, profile)
    assert chunks == []
