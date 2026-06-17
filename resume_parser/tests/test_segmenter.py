"""Unit tests for segmentation (blueprint §7)."""

from __future__ import annotations

from resume_parser.intermediate_representation import ExtractedDocument
from resume_parser.segmenter import normalize_heading, segment
from resume_parser.tests.helpers import block


def _doc(blocks, column_count=1):
    return ExtractedDocument(source_format="pdf", blocks=blocks, column_count=column_count)


def test_normalize_heading():
    assert normalize_heading("Work Experience!") == "work experience"
    assert normalize_heading("  TECHNICAL  SKILLS ") == "technical skills"
    assert normalize_heading("Education & Training") == "education training"


def test_heading_detected_by_font_size():
    blocks = [
        block("ALEX SMITH", 0, font_size=20.0, is_bold=True),
        block("alex@example.com", 1, font_size=10.0),
        block("Experience", 2, font_size=14.0, is_bold=True),
        block("Engineer — Acme Inc. (2020 - 2022)", 3, font_size=11.0, is_bold=True),
        block("Education", 4, font_size=14.0, is_bold=True),
        block("BSc — MIT (2016 - 2020)", 5, font_size=11.0, is_bold=True),
    ]
    seg = segment(_doc(blocks))
    assert "experience" in seg.sections
    assert "education" in seg.sections
    # The large name block stays in the header zone, not a spurious section.
    assert any(b.text == "ALEX SMITH" for b in seg.header_zone)


def test_heading_detected_by_docx_style():
    blocks = [
        block("Priya Patel", 0, font_size=20.0, is_bold=True, paragraph_style="Title"),
        block("Experience", 1, paragraph_style="Heading 1"),
        block("Analyst — Globex (2020 - 2022)", 2, is_bold=True),
        block("Skills", 3, paragraph_style="Heading 1"),
        block("Python, SQL", 4, paragraph_style="Normal"),
    ]
    seg = segment(_doc(blocks))
    assert "experience" in seg.sections
    assert "skills" in seg.sections


def test_heading_detected_by_bold_allcaps():
    blocks = [
        block("MORGAN TAYLOR", 0, is_bold=True),
        block("WORK EXPERIENCE", 1, is_bold=True),
        block("Supervisor — Pacific Logistics (2019 - 2023)", 2, is_bold=True),
        block("EDUCATION", 3, is_bold=True),
        block("Diploma — Camosun College (2017 - 2019)", 4, is_bold=True),
    ]
    seg = segment(_doc(blocks))
    assert "experience" in seg.sections
    assert "education" in seg.sections


def test_status_success_partial_failed():
    # success: experience + education (>= 2 canonical, includes a core one)
    ok = segment(_doc([
        block("Experience", 0, font_size=14.0, is_bold=True),
        block("Engineer — Acme Inc. (2020 - 2022)", 1, font_size=11.0, is_bold=True),
        block("Education", 2, font_size=14.0, is_bold=True),
        block("BSc — MIT (2016 - 2020)", 3, font_size=11.0, is_bold=True),
    ]))
    assert ok.status == "success"

    # partial: only one section matched
    partial = segment(_doc([
        block("Skills", 0, font_size=14.0, is_bold=True),
        block("Python, SQL", 1, font_size=11.0),
    ]))
    assert partial.status == "partial"

    # failed: no heading candidates at all
    failed = segment(_doc([
        block("just some text", 0, font_size=11.0),
        block("more text with no structure", 1, font_size=11.0),
    ]))
    assert failed.status == "failed"
    assert failed.sections == {}


def test_unrecognized_heading_becomes_additional_section():
    blocks = [
        block("Experience", 0, font_size=14.0, is_bold=True),
        block("Engineer — Acme Inc. (2020 - 2022)", 1, font_size=11.0, is_bold=True),
        block("AWARDS", 2, font_size=14.0, is_bold=True),
        block("Dean's List 2019", 3, font_size=11.0),
        block("Hackathon Winner 2020", 4, font_size=11.0),
    ]
    seg = segment(_doc(blocks))
    assert "AWARDS" in seg.additional_sections
    texts = [b.text for b in seg.additional_sections["AWARDS"]]
    assert texts == ["Dean's List 2019", "Hackathon Winner 2020"]


def test_summary_and_contact_routed_out_of_sections():
    blocks = [
        block("Summary", 0, font_size=14.0, is_bold=True),
        block("Experienced analyst.", 1, font_size=11.0),
        block("Experience", 2, font_size=14.0, is_bold=True),
        block("Analyst — Globex (2020 - 2022)", 3, font_size=11.0, is_bold=True),
    ]
    seg = segment(_doc(blocks))
    assert "summary" not in seg.sections      # routed to summary_blocks
    assert seg.summary_blocks
    assert "experience" in seg.sections


def test_columns_processed_independently():
    # A heading in column 1 must not terminate a section in column 0.
    blocks = [
        block("Experience", 0, column_index=0, font_size=14.0, is_bold=True),
        block("Engineer — Acme Inc. (2020 - 2022)", 1, column_index=0, font_size=11.0, is_bold=True),
        block("Did work in col 0.", 2, column_index=0, font_size=11.0, is_list_item=True, indentation_level=1),
        block("Skills", 0, column_index=1, font_size=14.0, is_bold=True),
        block("Python, SQL", 1, column_index=1, font_size=11.0),
    ]
    seg = segment(_doc(blocks, column_count=2))
    assert "experience" in seg.sections
    assert "skills" in seg.sections
    # The col-0 experience description is intact despite the col-1 heading.
    exp_texts = [b.text for b in seg.sections["experience"]]
    assert "Did work in col 0." in exp_texts
