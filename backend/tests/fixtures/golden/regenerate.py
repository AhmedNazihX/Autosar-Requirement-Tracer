"""Rebuild the ``*.blocks.json`` halves of the golden fixtures.

    cd backend && uv run python tests/fixtures/golden/regenerate.py

Reads the frozen page list from ``fixtures.json`` and re-extracts exactly
those pages from the PDFs in ``data/docs/`` (fetch them first with
``uv run python -m ingestion.fetcher projects/autosar-can/project.yaml``).

This script touches **only** ``*.blocks.json``. The ``*.expected.json``
files are hand-labeled ground truth and are never generated from code — see
``README.md``. Run this after a deliberate parser or PyMuPDF upgrade and
review the diff; a change in the blocks output is a real change in what
every downstream stage sees.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent
BACKEND_DIR = GOLDEN_DIR.parents[2]
REPO_ROOT = GOLDEN_DIR.parents[3]

sys.path.insert(0, str(BACKEND_DIR))

from core.manifest import load_manifest  # noqa: E402
from ingestion.fetcher import docs_dir, sha256_file  # noqa: E402
from ingestion.pdf_loader import EXTRACTION_MODE, parse_pdf  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "projects" / "autosar-can" / "project.yaml"
INDEX_PATH = GOLDEN_DIR / "fixtures.json"


def main() -> int:
    import pymupdf

    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    manifest = load_manifest(MANIFEST_PATH)
    documents = {doc.key: doc for doc in manifest.documents}
    pdf_dir = docs_dir(manifest)

    wanted: dict[str, list[dict]] = {}
    for entry in index["pages"]:
        wanted.setdefault(entry["document_key"], []).append(entry)

    for document_key, entries in wanted.items():
        document = documents.get(document_key)
        if document is None:
            raise SystemExit(f"{INDEX_PATH}: unknown document key {document_key!r}")
        pdf_path = pdf_dir / document.filename
        if not pdf_path.is_file():
            raise SystemExit(
                f"{pdf_path} is missing — run "
                "'uv run python -m ingestion.fetcher projects/autosar-can/project.yaml' first"
            )
        parsed = parse_pdf(pdf_path)
        digest = sha256_file(pdf_path)

        for entry in entries:
            page = parsed.page(entry["page"])
            payload = {
                "fixture": entry["fixture"],
                "document_key": document_key,
                "source_document": document.filename,
                "source_sha256": digest,
                "corpus_version": manifest.version,
                "page": page.page,
                "page_count": parsed.page_count,
                "width": round(page.width, 3),
                "height": round(page.height, 3),
                "char_start": page.char_start,
                "char_end": page.char_end,
                "parser": {
                    "library": "pymupdf",
                    "version": pymupdf.__version__,
                    "extraction_mode": EXTRACTION_MODE,
                    "module": "ingestion.pdf_loader.parse_pdf",
                },
                "blocks": [
                    {
                        "block_no": block.block_no,
                        "bbox": [round(v, 3) for v in block.bbox],
                        "char_start": block.char_start,
                        "char_end": block.char_end,
                        "text": block.text,
                    }
                    for block in page.blocks
                ],
            }
            out = GOLDEN_DIR / f"{entry['fixture']}.blocks.json"
            out.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(f"wrote {out.relative_to(REPO_ROOT)} ({len(page.blocks)} blocks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
