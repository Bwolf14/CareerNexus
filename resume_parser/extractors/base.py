"""
Shared extractor interface (blueprint §6, §11).

An extractor's entire job is to turn a file on disk into an
``ExtractedDocument`` — a flat, column-aware list of ``TextBlock``s
carrying *raw facts only* (text, font size, bold/italic, indentation,
list/table flags, column + reading order). All interpretation of those
facts happens later, in the segmenter and field parsers.
"""

from __future__ import annotations

from typing import Protocol

from ..intermediate_representation import ExtractedDocument


class Extractor(Protocol):
    """Structural type implemented by ``PdfExtractor`` and ``DocxExtractor``."""

    def extract(self, filepath: str) -> ExtractedDocument:
        ...


# A standard indent unit (~0.25") used by both extractors to bucket
# horizontal offsets into discrete ``indentation_level`` values. PDFs are
# measured in points (72pt = 1in); DOCX left-indents are measured in EMU
# (914400 EMU = 1in).
INDENT_UNIT_PT = 18.0
INDENT_UNIT_EMU = 228600
MAX_INDENT_LEVEL = 6
