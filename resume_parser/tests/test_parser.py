"""
Format detection + end-to-end integration tests (blueprint §5, §13).

Integration samples are synthesized into a temp dir (see generate_samples)
covering: single-column PDF, two-column/sidebar PDF, DOCX with Word heading
styles, DOCX with manual bold only, a layout-table sidebar DOCX, and a
no-headers DOCX (the "failed" path). We assert schema validity + spot-check
key fields — not exact output, since formatting varies.
"""

from __future__ import annotations

import pytest

from resume_parser import ParsedResume, parse_resume
from resume_parser.exceptions import CorruptFileError, UnsupportedFormatError
from resume_parser.parser import detect_format
from resume_parser.tests import generate_samples


@pytest.fixture(scope="module")
def samples(tmp_path_factory) -> dict[str, str]:
    out = tmp_path_factory.mktemp("sample_resumes")
    return generate_samples.generate_all(str(out))


# --------------------------------------------------------------------------
# format detection
# --------------------------------------------------------------------------
def test_detect_format_pdf_and_docx(samples):
    assert detect_format(samples["single_column.pdf"]) == "pdf"
    assert detect_format(samples["heading_styles.docx"]) == "docx"


def test_detect_format_rejects_unknown_extension(tmp_path):
    p = tmp_path / "resume.txt"
    p.write_text("hello world")
    with pytest.raises(UnsupportedFormatError):
        detect_format(str(p))


def test_detect_format_rejects_extension_magic_mismatch(tmp_path):
    # .pdf extension but no %PDF signature -> corrupt, not silently accepted.
    p = tmp_path / "fake.pdf"
    p.write_bytes(b"this is not a pdf at all")
    with pytest.raises(CorruptFileError):
        detect_format(str(p))


def test_detect_format_missing_file():
    with pytest.raises(CorruptFileError):
        detect_format("/no/such/file.pdf")


# --------------------------------------------------------------------------
# integration — every sample yields a schema-valid ParsedResume
# --------------------------------------------------------------------------
def test_all_samples_return_valid_parsed_resume(samples):
    for name, path in samples.items():
        resume = parse_resume(path)
        assert isinstance(resume, ParsedResume)
        # Always-present invariants (blueprint §3).
        assert resume.raw_text is not None
        assert resume.metadata.parser_version
        # Round-trips through JSON.
        assert ParsedResume.model_validate_json(resume.model_dump_json()) == resume


def test_single_column_pdf(samples):
    r = parse_resume(samples["single_column.pdf"])
    assert r.metadata.section_detection_status == "success"
    assert r.contact_info.name == "ALEX SMITH"
    assert r.contact_info.email == "alex.smith@example.com"
    assert r.contact_info.location == "Calgary, AB"
    assert len(r.experience) == 2
    assert r.experience[0].title == "Network Technician"
    assert r.experience[0].company == "Foothills IT Inc."
    assert r.education[0].gpa == "3.7/4.0"
    assert "TCP/IP" in r.skills.raw
    assert r.certifications[0].name == "CompTIA Network+"
    assert "AWARDS" in r.additional_sections


def test_two_column_pdf(samples):
    r = parse_resume(samples["two_column.pdf"])
    assert r.metadata.section_detection_status == "success"
    assert r.contact_info.name == "JORDAN LEE"
    assert r.contact_info.email == "jordan@example.com"   # from the left sidebar
    assert r.experience[0].title == "Software Developer"
    assert r.education[0].institution == "University of Lethbridge"
    assert "Python" in r.skills.raw                       # from the left sidebar


def test_heading_styles_docx(samples):
    r = parse_resume(samples["heading_styles.docx"])
    assert r.metadata.source_format == "docx"
    assert r.metadata.section_detection_status == "success"
    assert r.contact_info.name == "Priya Patel"
    assert r.summary and "data analyst" in r.summary.lower()
    assert r.experience[0].company == "Northern Health Ltd."
    assert r.education[0].institution == "University of Alberta"
    assert "Languages" in r.additional_sections           # unrecognized heading


def test_manual_bold_docx(samples):
    r = parse_resume(samples["manual_bold.docx"])
    assert r.metadata.section_detection_status == "success"
    assert r.contact_info.name == "MORGAN TAYLOR"
    assert r.experience[0].title == "Warehouse Supervisor"
    assert r.education[0].institution == "Camosun College"
    assert r.skills.raw  # "Inventory Management, Forklift Certified, ..."


def test_sidebar_docx(samples):
    r = parse_resume(samples["sidebar.docx"])
    assert r.metadata.section_detection_status == "success"
    assert r.contact_info.name == "SAM RIVERA"
    assert r.contact_info.email == "sam@example.com"      # sidebar cell
    assert r.experience[0].title == "Front-End Developer"
    assert "JavaScript" in r.skills.raw


def test_no_headers_docx_fails_gracefully(samples):
    r = parse_resume(samples["no_headers.docx"])
    assert r.metadata.section_detection_status == "failed"
    assert r.experience == [] and r.education == []
    # raw_text is still the guaranteed fallback.
    assert "customer service" in r.raw_text.lower()


def test_scanned_like_pdf_fails_gracefully(samples):
    r = parse_resume(samples["scanned_like.pdf"])
    assert r.metadata.section_detection_status == "failed"
    assert any("scanned" in w.lower() or "no extractable text" in w.lower()
               for w in r.metadata.warnings)


# --------------------------------------------------------------------------
# extraction confidence
# --------------------------------------------------------------------------
def test_confidence_high_for_well_parsed_resume(samples):
    r = parse_resume(samples["single_column.pdf"])
    # name + email + experience + education + skills all present on a "success"
    # parse -> top of the range.
    assert r.metadata.extraction_confidence >= 0.9


def test_confidence_zero_for_unparseable(samples):
    r = parse_resume(samples["scanned_like.pdf"])
    assert r.metadata.extraction_confidence == 0.0


def test_confidence_in_valid_range_for_all_samples(samples):
    for path in samples.values():
        conf = parse_resume(path).metadata.extraction_confidence
        assert 0.0 <= conf <= 1.0
