"""Format-specific extractors -> ExtractedDocument (IDR)."""

from __future__ import annotations

from ..intermediate_representation import ExtractedDocument
from .base import Extractor
from .docx_extractor import DocxExtractor
from .pdf_extractor import PdfExtractor

__all__ = ["Extractor", "PdfExtractor", "DocxExtractor", "extract"]


def extract(filepath: str, fmt: str) -> ExtractedDocument:
    """Dispatch to the right extractor for a validated format."""
    if fmt == "pdf":
        return PdfExtractor().extract(filepath)
    if fmt == "docx":
        return DocxExtractor().extract(filepath)
    raise ValueError(f"unsupported format: {fmt!r}")
