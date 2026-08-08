"""Unit tests for the explicitly recorded Docling-to-RapidOCR fallback."""

from __future__ import annotations

import uuid

from documind.services.ocr_service import OCRService, ParserOutput


class _ContentSource:
    async def read_version_bytes(self, version_id: uuid.UUID) -> bytes:
        return b"image-derived document bytes"


class _Parser:
    def __init__(self, output: ParserOutput) -> None:
        self._output = output
        self.calls: list[bytes] = []

    async def parse(self, payload: bytes) -> ParserOutput:
        self.calls.append(payload)
        return self._output


async def test_parse_uses_rapidocr_when_docling_confidence_is_too_low() -> None:
    version_id = uuid.uuid4()
    docling = _Parser(
        ParserOutput(
            text="too little",
            pages=[],
            confidence=0.2,
            parser_version="docling-pinned",
        ),
    )
    rapidocr = _Parser(
        ParserOutput(
            text="Recovered text",
            pages=[{"page_number": 1, "text": "Recovered text", "blocks": []}],
            confidence=0.95,
            parser_version="rapidocr-pinned",
        ),
    )
    service = OCRService(
        content_source=_ContentSource(),
        docling_parser=docling,
        rapidocr_parser=rapidocr,
        confidence_threshold=0.8,
    )

    result = await service.parse(version_id)

    assert result.engine == "rapidocr"
    assert result.text == "Recovered text"
    assert result.confidence == 0.95
    assert [attempt.outcome for attempt in result.parser_attempts] == [
        "low_text_confidence",
        "completed",
    ]
    assert docling.calls == [b"image-derived document bytes"]
    assert rapidocr.calls == [b"image-derived document bytes"]
