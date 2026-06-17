"""
Intermediate Document Representation (IDR) — Career Nexus Resume Parser
=========================================================================

This is the internal contract between the format-specific extractors
(PDF, DOCX) and the segmenter. It is NOT part of the parser's public
output (see resume_schema.py for that) — it's a pipeline-internal
representation that lets segmentation and field-parsing logic be written
once, regardless of source format.

Design principles
------------------

1. Raw facts only. This layer records what is literally present in the
   source document (text, font size, bold/italic, indentation, list
   formatting, column placement) — it does NOT interpret meaning. Whether
   a block "is a heading" or "starts a new job entry" is derived later by
   the segmenter and field parsers, not decided here. "All caps" is not
   stored either, since it's trivially derivable from `text.isupper()`.

2. Column-aware. Multi-column resume layouts (common with sidebar-style
   templates) are resolved into separate column sequences during
   extraction. `column_index` + `order_index` together define reading
   order: order_index is reading order *within* a column. The segmenter
   processes each column as an independent sequence — a heading in one
   column does not terminate a section in another.

3. No computed baselines here. The "body text font size" used to detect
   headings is a derived value the segmenter computes from the blocks —
   keeping it out of the IDR avoids the extractor needing to know
   anything about segmentation logic.

4. Tables are flattened. DOCX tables and PDF table-like regions are
   flattened into blocks in row-major order with `is_table_cell=True`.
   Known limitation: resumes that rely heavily on tables for layout (not
   just contact info or skill grids) may not segment cleanly — revisit if
   testing against real resumes shows this is common.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class TextBlock(BaseModel):
    text: str

    # Reading order within this block's column (0-indexed, per column).
    order_index: int

    # 0 = single-column layout, or leftmost column in a multi-column layout.
    column_index: int = 0

    # PDF: 1-indexed page number. DOCX: always 0 — page layout isn't
    # computed without rendering, and sections can span pages anyway.
    page_number: int = 0

    # Absolute font size in points, where known. None if unavailable
    # (common for DOCX runs that inherit size from a style rather than
    # setting it explicitly).
    font_size: Optional[float] = None

    # DOCX paragraph style name (e.g. "Heading 1", "Normal", "List Bullet").
    # None for PDF, which has no equivalent concept.
    paragraph_style: Optional[str] = None

    is_bold: bool = False
    is_italic: bool = False

    # Literally formatted as a list item in the source (bullet/numbered).
    is_list_item: bool = False

    # 0 = no indentation. Higher = more indented. Used to distinguish,
    # e.g., a job title line (level 0) from its description bullets
    # (level 1).
    indentation_level: int = 0

    is_table_cell: bool = False


class ExtractedDocument(BaseModel):
    source_format: Literal["pdf", "docx"]
    blocks: list[TextBlock] = Field(default_factory=list)

    # Set by the extractor at construction time from its column-detection
    # pass. 1 = single-column layout.
    column_count: int = 1


if __name__ == "__main__":
    # A tiny excerpt — a section heading, a job-entry header line, and one
    # description bullet — illustrating what an extractor hands off.
    #
    # Note the heading (14pt, bold) stands out from the body baseline
    # (11pt). The job-entry header is also bold but AT body size — that's
    # the kind of distinction field parsers (next stage) need to make:
    # "new entry header" vs. "section heading" vs. "description bullet"
    # can't be told apart by font size alone; indentation_level and
    # is_list_item matter too.
    doc = ExtractedDocument(
        source_format="pdf",
        column_count=1,
        blocks=[
            TextBlock(
                text="WORK EXPERIENCE",
                order_index=0,
                font_size=14.0,
                is_bold=True,
            ),
            TextBlock(
                text="IT Support Technician — Acme Corp (2023 - Present)",
                order_index=1,
                font_size=11.0,
                is_bold=True,
            ),
            TextBlock(
                text="Resolved tier-1 helpdesk tickets and maintained IT asset inventory.",
                order_index=2,
                font_size=11.0,
                is_list_item=True,
                indentation_level=1,
            ),
        ],
    )

    print(doc.model_dump_json(indent=2))
