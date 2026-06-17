"""
Top-level entry point (blueprint §5, §11).

    parse_resume(filepath) -> ParsedResume

is the single function every CLI / API / import wrapper calls:

    detect_format -> extract -> segment -> build_parsed_resume

Format detection is the one place that fails loudly (an unreadable file has
no meaningful partial result). Everything after it degrades gracefully: a
resume that can't be segmented still returns a valid ``ParsedResume`` with
``raw_text`` populated and ``section_detection_status="failed"``.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Literal

from docx.opc.exceptions import PackageNotFoundError

from .exceptions import CorruptFileError, UnsupportedFormatError
from .extractors import extract
from .field_parsers import (
    parse_certifications,
    parse_contact,
    parse_education,
    parse_experience,
    parse_projects,
    parse_skills,
    parse_summary,
    parse_volunteer,
)
from .intermediate_representation import ExtractedDocument
from .normalizer import clean_text
from .schema import ParsedResume, ParserMetadata
from .segmenter import SegmentationResult, segment

PARSER_VERSION = "1.0.0"

# Below this many non-whitespace characters we treat the document as having
# no extractable text (scanned/image PDF) -> "failed" + warning, no OCR (v1).
_MIN_TEXT_CHARS = 20

_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"


def parse_resume(filepath: str) -> ParsedResume:
    """Parse a PDF or DOCX resume into a validated ``ParsedResume``."""
    fmt = detect_format(filepath)
    doc = extract(filepath, fmt)
    return build_parsed_resume(doc)


def detect_format(filepath: str) -> Literal["pdf", "docx"]:
    """Validate by extension *and* magic bytes (blueprint §5)."""
    path = Path(filepath)
    if not path.exists():
        raise CorruptFileError(f"File not found: {filepath}")
    if path.is_dir():
        raise UnsupportedFormatError(f"Path is a directory, not a file: {filepath}")

    ext = path.suffix.lower()
    with open(path, "rb") as fh:
        head = fh.read(8)

    if ext == ".pdf" or head.startswith(_PDF_MAGIC):
        if not head.startswith(_PDF_MAGIC):
            raise CorruptFileError(
                f"{filepath!r} has a .pdf extension but is missing the %PDF "
                f"signature; the file may be corrupt."
            )
        return "pdf"

    if ext == ".docx" or head.startswith(_ZIP_MAGIC):
        if not head.startswith(_ZIP_MAGIC):
            raise CorruptFileError(
                f"{filepath!r} has a .docx extension but is not a valid zip "
                f"package; the file may be corrupt."
            )
        # A zip on its own isn't necessarily a DOCX — confirm by opening it.
        try:
            from docx import Document

            Document(str(path))
        except (PackageNotFoundError, KeyError, ValueError, OSError) as exc:
            raise CorruptFileError(
                f"{filepath!r} is a zip but not a readable Word document: {exc}"
            ) from exc
        return "docx"

    raise UnsupportedFormatError(
        f"Unsupported file type {ext or '(no extension)'!r} for {filepath!r}; "
        f"only PDF and DOCX are supported."
    )


def build_parsed_resume(doc: ExtractedDocument) -> ParsedResume:
    """Stages 3-10: segment, field-parse, assemble the output object."""
    warnings: list[str] = []
    raw_text = _build_raw_text(doc)

    # Scanned / empty document: bail out with a valid, "failed" result.
    if len(raw_text.replace("\n", "").strip()) < _MIN_TEXT_CHARS:
        warnings.append(
            "No extractable text found; the document may be a scanned/image "
            "PDF (OCR is not supported in v1)."
        )
        return ParsedResume(
            metadata=ParserMetadata(
                source_format=doc.source_format,
                parser_version=PARSER_VERSION,
                section_detection_status="failed",
                warnings=warnings,
            ),
            raw_text=raw_text,
        )

    seg: SegmentationResult = segment(doc)

    contact = parse_contact(seg.header_zone, warnings)
    summary = parse_summary(seg.summary_blocks, seg.header_zone, contact, warnings)
    experience = parse_experience(seg.sections.get("experience", []), seg.baseline, warnings)
    education = parse_education(seg.sections.get("education", []), seg.baseline, warnings)
    skills = parse_skills(seg.sections.get("skills", []), warnings)
    certifications = parse_certifications(seg.sections.get("certifications", []), warnings)
    projects = parse_projects(seg.sections.get("projects", []), seg.baseline, warnings)
    volunteer = parse_volunteer(
        seg.sections.get("volunteer_experience", []), seg.baseline, warnings
    )

    additional = {}
    for heading, blocks in seg.additional_sections.items():
        lines = [clean_text(b.text) for b in blocks]
        lines = [ln for ln in lines if ln]
        if lines:
            additional[heading] = lines

    metadata = ParserMetadata(
        source_format=doc.source_format,
        parser_version=PARSER_VERSION,
        section_detection_status=seg.status,
        warnings=warnings,
    )

    return ParsedResume(
        metadata=metadata,
        contact_info=contact,
        summary=summary,
        experience=experience,
        education=education,
        skills=skills,
        certifications=certifications,
        projects=projects,
        volunteer_experience=volunteer,
        additional_sections=additional,
        raw_text=raw_text,
    )


def _build_raw_text(doc: ExtractedDocument) -> str:
    """Full extracted text, column by column in reading order."""
    by_col: dict[int, list] = defaultdict(list)
    for b in doc.blocks:
        by_col[b.column_index].append(b)
    lines: list[str] = []
    for col in sorted(by_col):
        for b in sorted(by_col[col], key=lambda x: x.order_index):
            t = b.text.strip()
            if t:
                lines.append(t)
    return "\n".join(lines)
