"""
PDF extraction (blueprint §6) — PyMuPDF for text/layout, pdfplumber for tables.

Pipeline:

1. Walk every page with ``page.get_text("dict")`` -> blocks -> lines ->
   spans, collecting one record per *line* (text + font size + bold/italic
   + bounding box + page).
2. Detect columns per page via a vertical-gutter scan (a band in the
   central region of the page that no line crosses, with content on both
   sides). This is the robust form of the blueprint's "cluster lines by x0
   with a significant gap" heuristic — it avoids treating ordinary bullet
   indentation as a second column.
3. Within each column, order by (page, y0, x0) and assign ``order_index``;
   each column's sequence continues across pages.
4. Bucket ``x0`` (relative to the column's left edge) into
   ``indentation_level``; detect/strip leading bullet glyphs; flag spans
   that fall inside a pdfplumber-detected table region as ``is_table_cell``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

import fitz  # PyMuPDF

from ..intermediate_representation import ExtractedDocument, TextBlock
from ..normalizer import strip_leading_bullet
from .base import INDENT_UNIT_PT, MAX_INDENT_LEVEL

# fitz span flag bits.
_FLAG_ITALIC = 1 << 1   # 2
_FLAG_BOLD = 1 << 4     # 16

_BOLD_NAME_HINTS = ("bold", "black", "heavy", "semibold", "demibold")
_ITALIC_NAME_HINTS = ("italic", "oblique")

_GUTTER_STEP = 3.0
_GUTTER_MIN_SIDE_LINES = 3

# Below this many extracted characters we treat the PDF as scanned/image-only
# and try the OCR fallback (if Tesseract is available). Kept low so a normal
# text PDF never triggers a needless OCR pass.
_OCR_TRIGGER_CHARS = 30


class PdfExtractor:
    def extract(self, filepath: str) -> ExtractedDocument:
        doc = fitz.open(filepath)
        ocr_used = False
        try:
            page_widths: dict[int, float] = {}
            lines: list[dict] = []
            for pno in range(len(doc)):
                page = doc[pno]
                page_widths[pno] = float(page.rect.width)
                self._collect_page_lines(page, pno, lines)

            # Scanned/image PDF: no embedded text -> try OCR before giving up.
            total_chars = sum(len(l["text"].strip()) for l in lines)
            if total_chars < _OCR_TRIGGER_CHARS:
                from .ocr import ocr_available, ocr_document_lines

                if ocr_available():
                    ocr_lines = ocr_document_lines(doc)
                    if ocr_lines:
                        lines = ocr_lines
                        ocr_used = True
        finally:
            doc.close()

        # OCR output has no table structure; skip pdfplumber in that case.
        table_regions = {} if ocr_used else self._table_regions(filepath)
        blocks, column_count = self._assemble(lines, page_widths, table_regions)
        return ExtractedDocument(
            source_format="pdf", blocks=blocks, column_count=column_count
        )

    # -- step 1: collect raw line records -------------------------------
    @staticmethod
    def _collect_page_lines(page, pno: int, out: list[dict]) -> None:
        data = page.get_text("dict")
        for block in data.get("blocks", []):
            if block.get("type", 0) != 0:
                continue  # image block, no text
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if s.get("text")]
                if not spans:
                    continue
                text = "".join(s["text"] for s in spans)
                if not text.strip():
                    continue
                out.append(
                    {
                        "text": text,
                        "page": pno,
                        "size": max(s["size"] for s in spans),
                        "bold": any(PdfExtractor._is_bold(s) for s in spans),
                        "italic": any(PdfExtractor._is_italic(s) for s in spans),
                        "x0": min(s["bbox"][0] for s in spans),
                        "y0": min(s["bbox"][1] for s in spans),
                        "x1": max(s["bbox"][2] for s in spans),
                        "y1": max(s["bbox"][3] for s in spans),
                    }
                )

    @staticmethod
    def _is_bold(span: dict) -> bool:
        if span.get("flags", 0) & _FLAG_BOLD:
            return True
        name = span.get("font", "").lower()
        return any(h in name for h in _BOLD_NAME_HINTS)

    @staticmethod
    def _is_italic(span: dict) -> bool:
        if span.get("flags", 0) & _FLAG_ITALIC:
            return True
        name = span.get("font", "").lower()
        return any(h in name for h in _ITALIC_NAME_HINTS)

    # -- step 2: column detection (per page) ----------------------------
    @staticmethod
    def _page_split_x(lines_on_page: list[dict], page_width: float) -> Optional[float]:
        """Return an x-coordinate splitting the page into two columns, or None.

        Scans the central 30%-70% band for a vertical gutter no line crosses,
        with enough lines wholly on each side. Picks the gutter that most
        evenly divides the lines.
        """
        if len(lines_on_page) < 2 * _GUTTER_MIN_SIDE_LINES:
            return None
        left_bound = page_width * 0.30
        right_bound = page_width * 0.70
        best_x: Optional[float] = None
        best_score = -1.0
        x = left_bound
        while x <= right_bound:
            crosses = any(l["x0"] < x < l["x1"] for l in lines_on_page)
            if not crosses:
                left_n = sum(1 for l in lines_on_page if l["x1"] <= x)
                right_n = sum(1 for l in lines_on_page if l["x0"] >= x)
                if left_n >= _GUTTER_MIN_SIDE_LINES and right_n >= _GUTTER_MIN_SIDE_LINES:
                    score = min(left_n, right_n)
                    if score > best_score:
                        best_score = score
                        best_x = x
            x += _GUTTER_STEP
        return best_x

    # -- step 3 & 4: assemble ordered, column-aware blocks --------------
    def _assemble(
        self,
        lines: list[dict],
        page_widths: dict[int, float],
        table_regions: dict[int, list[tuple]],
    ) -> tuple[list[TextBlock], int]:
        by_page: dict[int, list[dict]] = defaultdict(list)
        for rec in lines:
            by_page[rec["page"]].append(rec)

        multi_column = False
        for pno, page_lines in by_page.items():
            split_x = self._page_split_x(page_lines, page_widths.get(pno, 612.0))
            if split_x is not None:
                multi_column = True
                for rec in page_lines:
                    rec["col"] = 0 if rec["x0"] < split_x else 1
            else:
                for rec in page_lines:
                    rec["col"] = 0
        column_count = 2 if multi_column else 1

        by_col: dict[int, list[dict]] = defaultdict(list)
        for rec in lines:
            by_col[rec["col"]].append(rec)

        blocks: list[TextBlock] = []
        for col in sorted(by_col):
            items = sorted(
                by_col[col], key=lambda r: (r["page"], round(r["y0"], 1), r["x0"])
            )
            col_min_x = min(r["x0"] for r in items)
            order = 0
            for rec in items:
                text, is_list = strip_leading_bullet(rec["text"].strip())
                text = text.strip()
                if not text:
                    continue
                delta = rec["x0"] - col_min_x
                level = max(0, min(MAX_INDENT_LEVEL, int(round(delta / INDENT_UNIT_PT))))
                blocks.append(
                    TextBlock(
                        text=text,
                        order_index=order,
                        column_index=col,
                        page_number=rec["page"] + 1,
                        font_size=round(rec["size"], 1),
                        is_bold=rec["bold"],
                        is_italic=rec["italic"],
                        is_list_item=is_list,
                        indentation_level=level,
                        is_table_cell=self._in_table(rec, table_regions),
                    )
                )
                order += 1
        return blocks, column_count

    # -- pdfplumber table regions ---------------------------------------
    @staticmethod
    def _table_regions(filepath: str) -> dict[int, list[tuple]]:
        """Map page index -> list of table bounding boxes (x0, top, x1, bottom).

        Best-effort: any failure (including pdfplumber/pdfminer hiccups on
        odd PDFs) degrades to "no tables detected" rather than breaking the
        whole extraction.
        """
        regions: dict[int, list[tuple]] = defaultdict(list)
        try:
            import pdfplumber

            with pdfplumber.open(filepath) as pdf:
                for i, page in enumerate(pdf.pages):
                    try:
                        for table in page.find_tables():
                            regions[i].append(tuple(table.bbox))
                    except Exception:
                        continue
        except Exception:
            return {}
        return regions

    @staticmethod
    def _in_table(rec: dict, regions: dict[int, list[tuple]]) -> bool:
        page_regions = regions.get(rec["page"])
        if not page_regions:
            return False
        cx = (rec["x0"] + rec["x1"]) / 2.0
        cy = (rec["y0"] + rec["y1"]) / 2.0
        for x0, top, x1, bottom in page_regions:
            if x0 - 2 <= cx <= x1 + 2 and top - 2 <= cy <= bottom + 2:
                return True
        return False
