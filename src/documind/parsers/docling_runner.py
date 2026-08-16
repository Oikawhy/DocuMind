"""Docling sandbox runner — fixed entrypoint for the no-network container.

Reads a single file path from the command line, runs Docling's DocumentConverter,
and writes ONLY structured JSON to stdout.  stderr is silenced to prevent
document contents from leaking into worker logs.

Exit codes:
    0  — success (JSON on stdout)
    1  — usage error or file not found
    2  — parser failure (no content leaked)
"""

from __future__ import annotations

import json
import os
import sys


def main() -> None:
    # Redirect stderr immediately so no diagnostic can leak document content.
    sys.stderr = open(os.devnull, "w")  # noqa: SIM115

    if len(sys.argv) != 2:
        sys.exit(1)

    input_path = sys.argv[1]
    if not os.path.isfile(input_path):
        sys.exit(1)

    try:
        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
        result = converter.convert(input_path)
        document = result.document

        # Build structured page output from Docling's internal representation.
        pages: list[dict] = []
        full_text_parts: list[str] = []

        for page_no, page in enumerate(getattr(document, "pages", []), start=1):
            page_blocks: list[dict] = []
            page_text_parts: list[str] = []

            for block_idx, block in enumerate(getattr(page, "blocks", [])):
                block_text = getattr(block, "text", "")
                if block_text:
                    page_blocks.append(
                        {
                            "block_id": f"p{page_no}-b{block_idx + 1}",
                            "kind": getattr(block, "kind", "text"),
                            "text": block_text,
                            "reading_order": block_idx + 1,
                        }
                    )
                    page_text_parts.append(block_text)

            page_text = "\n".join(page_text_parts)
            full_text_parts.append(page_text)
            pages.append(
                {
                    "page_number": page_no,
                    "text": page_text,
                    "blocks": page_blocks,
                    "tables": [],
                }
            )

        # Fallback: if no pages were extracted, use the full-text export.
        full_text = "\n\n".join(full_text_parts) if full_text_parts else getattr(document, "text", "")
        if not full_text and hasattr(result, "text"):
            full_text = str(result.text)

        output = {
            "text": full_text,
            "pages": pages,
            "confidence": 0.9 if full_text.strip() else 0.0,
            "version": "docling",
        }

        sys.stdout.write(json.dumps(output, ensure_ascii=False))
        sys.stdout.flush()
    except Exception:
        sys.exit(2)


if __name__ == "__main__":
    main()
