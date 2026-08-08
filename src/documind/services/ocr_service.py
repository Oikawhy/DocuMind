"""Docling-first parsing with an explicit, provenance-preserving RapidOCR fallback."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class ParserUnavailableError(RuntimeError):
    """A parser sandbox or local OCR runtime cannot serve a request."""


class VersionContentSource(Protocol):
    """Load a private version's inspected quarantine bytes."""

    async def read_version_bytes(self, version_id: uuid.UUID) -> bytes:
        """Return the version bytes after inspection passed."""


@dataclass(frozen=True)
class ParserOutput:
    """Engine output before normalization, preserving pages and confidence."""

    text: str
    pages: list[dict[str, Any]]
    confidence: float
    parser_version: str


class DocumentParser(Protocol):
    """A parser implementation that accepts bytes, never an untrusted path."""

    async def parse(self, payload: bytes) -> ParserOutput:
        """Parse the supplied private bytes in the implementation's sandbox."""


@dataclass(frozen=True)
class ParserAttempt:
    """Recorded decision trail for parser fallback and operator evidence."""

    engine: str
    version: str
    outcome: str


@dataclass(frozen=True)
class ParseResult:
    """The usable parse output and all engine attempts for a version."""

    version_id: str
    success: bool
    engine: str | None
    text: str
    pages: list[dict[str, Any]]
    confidence: float
    parser_attempts: list[ParserAttempt]
    safe_error_class: str | None = None
    safe_error_code: str | None = None


class DoclingSandboxParser:
    """Invoke the dedicated no-network Docling sandbox without a shell.

    The command is an injected, separately hardened container entrypoint.  It
    receives a read-only input path and returns only JSON on stdout; stderr is
    intentionally discarded so document contents and parser diagnostics cannot
    leak into worker logs.
    """

    def __init__(self, command: Sequence[str], *, timeout_seconds: float = 600.0) -> None:
        if not command:
            raise ValueError("Docling sandbox command is required")
        self._command = tuple(command)
        self._timeout_seconds = timeout_seconds

    async def parse(self, payload: bytes) -> ParserOutput:
        with tempfile.TemporaryDirectory(prefix="documind-docling-") as directory:
            input_path = Path(directory) / "input"
            input_path.write_bytes(payload)
            os.chmod(input_path, 0o400)
            try:
                process = await asyncio.create_subprocess_exec(
                    *self._command,
                    str(input_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self._timeout_seconds)
            except (TimeoutError, OSError) as exc:
                raise ParserUnavailableError("Docling sandbox is unavailable.") from exc
            if process.returncode != 0:
                raise ParserUnavailableError("Docling sandbox did not complete successfully.")

        try:
            data = json.loads(stdout)
            return ParserOutput(
                text=str(data.get("text", "")),
                pages=list(data.get("pages", [])),
                confidence=float(data.get("confidence", 0.0)),
                parser_version=str(data.get("version", "docling")),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ParserUnavailableError("Docling sandbox returned an invalid response.") from exc


class RapidOCRParser:
    """Use the local RapidOCR runtime only after Docling needs a fallback."""

    def __init__(self, engine: object | None = None) -> None:
        self._engine = engine

    async def parse(self, payload: bytes) -> ParserOutput:
        return await asyncio.to_thread(self._parse_sync, payload)

    def _parse_sync(self, payload: bytes) -> ParserOutput:
        try:
            engine = self._engine
            if engine is None:
                from rapidocr_onnxruntime import RapidOCR

                engine = RapidOCR()
                self._engine = engine
            rows, _ = engine(payload)  # type: ignore[operator]
        except (ImportError, OSError, RuntimeError, TypeError) as exc:
            raise ParserUnavailableError("RapidOCR runtime is unavailable.") from exc

        blocks: list[dict[str, Any]] = []
        confidences: list[float] = []
        for index, row in enumerate(rows or []):
            bbox, text, confidence = row
            value = str(text)
            blocks.append(
                {
                    "block_id": f"p1-b{index + 1}",
                    "kind": "text",
                    "text": value,
                    "bbox": list(bbox),
                    "reading_order": index + 1,
                },
            )
            confidences.append(float(confidence))
        text = "\n".join(block["text"] for block in blocks)
        confidence = sum(confidences) / len(confidences) if confidences else 0.0
        return ParserOutput(
            text=text,
            pages=[{"page_number": 1, "text": text, "blocks": blocks, "tables": []}],
            confidence=confidence,
            parser_version="rapidocr",
        )


class OCRService:
    """Run Docling first, recording exactly why RapidOCR was required."""

    def __init__(
        self,
        *,
        content_source: VersionContentSource,
        docling_parser: DocumentParser,
        rapidocr_parser: DocumentParser,
        confidence_threshold: float = 0.8,
    ) -> None:
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be in the range [0, 1]")
        self._content_source = content_source
        self._docling_parser = docling_parser
        self._rapidocr_parser = rapidocr_parser
        self._confidence_threshold = confidence_threshold

    async def parse(self, version_id: uuid.UUID) -> ParseResult:
        """Parse an inspected version; RapidOCR is never a silent replacement."""
        payload = await self._content_source.read_version_bytes(version_id)
        attempts: list[ParserAttempt] = []
        try:
            docling = await self._docling_parser.parse(payload)
        except Exception:
            attempts.append(ParserAttempt(engine="docling", version="unknown", outcome="failed"))
        else:
            outcome = _docling_outcome(docling, self._confidence_threshold)
            attempts.append(ParserAttempt(engine="docling", version=docling.parser_version, outcome=outcome))
            if outcome == "completed":
                return _successful_result(version_id, "docling", docling, attempts)

        try:
            rapidocr = await self._rapidocr_parser.parse(payload)
        except Exception:
            attempts.append(ParserAttempt(engine="rapidocr", version="unknown", outcome="failed"))
            return ParseResult(
                version_id=str(version_id),
                success=False,
                engine=None,
                text="",
                pages=[],
                confidence=0.0,
                parser_attempts=attempts,
                safe_error_class="unsupported_content",
                safe_error_code="PARSER_EXHAUSTED",
            )

        outcome = _docling_outcome(rapidocr, self._confidence_threshold)
        attempts.append(ParserAttempt(engine="rapidocr", version=rapidocr.parser_version, outcome=outcome))
        if outcome == "completed":
            return _successful_result(version_id, "rapidocr", rapidocr, attempts)
        return ParseResult(
            version_id=str(version_id),
            success=False,
            engine="rapidocr",
            text="",
            pages=[],
            confidence=rapidocr.confidence,
            parser_attempts=attempts,
            safe_error_class="unsupported_content",
            safe_error_code="PARSER_NO_USABLE_TEXT",
        )


def _docling_outcome(output: ParserOutput, threshold: float) -> str:
    if not output.text.strip():
        return "empty"
    if output.confidence < threshold:
        return "low_text_confidence"
    return "completed"


def _successful_result(
    version_id: uuid.UUID,
    engine: str,
    output: ParserOutput,
    attempts: list[ParserAttempt],
) -> ParseResult:
    return ParseResult(
        version_id=str(version_id),
        success=True,
        engine=engine,
        text=output.text,
        pages=output.pages,
        confidence=output.confidence,
        parser_attempts=attempts,
    )
