"""
Optional OCR fallback for scanned / image-only PDFs.

The normal PDF path (``pdf_extractor``) reads embedded text. A scanned resume
has none, so it would otherwise land as ``section_detection_status="failed"``.
When that happens we render each page to an image and run Tesseract over it,
producing the same per-line records the text path emits — so segmentation and
field parsing downstream are none the wiser.

Everything here is best-effort and dependency-optional: if ``pytesseract`` /
``Pillow`` aren't installed or the Tesseract binary isn't on the system, OCR is
simply skipped and the parser degrades to the pre-OCR behaviour. Coordinates are
scaled back from render pixels to PDF points so column detection keeps working.
"""

from __future__ import annotations

from typing import Any

# Render at 2x (~144 DPI): a good accuracy/speed trade-off for resume text.
_ZOOM = 2.0
# Ignore Tesseract words below this confidence (0-100); filters OCR noise.
_MIN_CONF = 40


def ocr_available() -> bool:
    """True if pytesseract, Pillow, and the Tesseract binary are all usable."""
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def ocr_document_lines(doc) -> list[dict[str, Any]]:
    """OCR every page of an open PyMuPDF document into line records.

    Returns a list of dicts matching ``PdfExtractor._collect_page_lines`` output
    (text, page, size, bold, italic, x0, y0, x1, y1). Returns ``[]`` on any
    failure so the caller can fall back cleanly.
    """
    try:
        import fitz
        import pytesseract
        from PIL import Image
        from pytesseract import Output
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    try:
        for pno in range(len(doc)):
            page = doc[pno]
            pix = page.get_pixmap(matrix=fitz.Matrix(_ZOOM, _ZOOM))
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            data = pytesseract.image_to_data(img, output_type=Output.DICT)
            out.extend(_lines_from_tsv(data, pno))
    except Exception:
        return []
    return out


def _lines_from_tsv(data: dict, pno: int) -> list[dict[str, Any]]:
    """Group Tesseract word boxes into line records (coords scaled to points)."""
    lines: dict[tuple, list[int]] = {}
    n = len(data.get("text", []))
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < _MIN_CONF:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(i)

    records: list[dict[str, Any]] = []
    for key, idxs in lines.items():
        words = [data["text"][i].strip() for i in idxs]
        text = " ".join(w for w in words if w)
        if not text.strip():
            continue
        x0 = min(data["left"][i] for i in idxs) / _ZOOM
        y0 = min(data["top"][i] for i in idxs) / _ZOOM
        x1 = max(data["left"][i] + data["width"][i] for i in idxs) / _ZOOM
        y1 = max(data["top"][i] + data["height"][i] for i in idxs) / _ZOOM
        records.append(
            {
                "text": text,
                "page": pno,
                "size": round(y1 - y0, 1) or 10.0,
                "bold": False,
                "italic": False,
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
            }
        )
    return records
