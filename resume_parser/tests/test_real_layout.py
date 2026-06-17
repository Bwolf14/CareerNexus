"""
Regression tests for "hard real-PDF" layouts (blueprint §6-§8).

Modelled on a real resume that broke the first cut: a single-column PDF that
exposes **no bold flag**, renders section headings only ~9% larger than body
text, and places each entry's **date on its own indented line** instead of in
the title/company line. These exercise the heading-size threshold, the
ALL-CAPS-without-bold rule, the return-to-margin entry split, the date
backfill, the institution scan, and the Title-Case location fix.
"""

from __future__ import annotations

from resume_parser.field_parsers.contact import LOCATION_RE
from resume_parser.field_parsers.experience import parse_experience
from resume_parser.intermediate_representation import ExtractedDocument
from resume_parser.parser import build_parsed_resume
from resume_parser.segmenter import _is_heading_candidate
from resume_parser.tests.helpers import block

BODY = 10.3
HEAD = 11.3  # only ~9.7% larger than body, and NOT bold


def _b(text, order, size=BODY, indent=0, **kw):
    return block(text, order, font_size=size, indentation_level=indent, **kw)


def _reconstructed_resume() -> ExtractedDocument:
    blocks = [
        _b("KADEN BIRCH", 0, 24.5),
        _b("INFORMATION TECHNOLOGY SERVICES", 1, 16.0),
        _b("Calgary, Alberta | (403) 317-1360 | kaden.d.birch@gmail.com", 2),
        _b("SUMMARY", 3, HEAD),
        _b("Information Technology student with extensive customer-facing experience,", 4),
        _b("working independently on-site with technical systems.", 5),
        _b("CORE STRENGTHS", 6, HEAD, indent=1),
        _b("Communicating with customers", 7, indent=1),
        _b("Problem solving and troubleshooting", 8, indent=6),
        _b("PROFESSIONAL EXPERIENCE", 9, HEAD),
        _b("City of Lethbridge - Machine Operator 1", 10),
        _b("May 2022 - August 2025", 11, indent=6),
        _b("Worked with heavy machinery and equipment.", 12, indent=2),
        _b("Liberty Security - System Install and Repair tech (Contractor)", 13),
        _b("September 2022 - January 2023", 14, indent=6),
        _b("Personalized installs to customer needs.", 15, indent=2),
        _b("Chief Mountain Gas - Gas line Maintenance and Repair Tech", 16),
        _b("May 2021 - August 2021", 17, indent=6),
        _b("Inspected and maintained gas lines.", 18, indent=2),
        _b("VOLUNTEER EXPERIENCE", 19, HEAD, indent=1),
        _b("Church of Jesus Christ of Latter-day Saints - Canada Montreal Mission", 20),
        _b("March 2019 - February 2021", 21, indent=6),
        _b("Engaged with individuals of diverse backgrounds.", 22, indent=2),
        _b("EDUCATION", 23, HEAD),
        _b("Information Technology Services Diploma - Current", 24),
        _b("January 2025 - August 2026", 25, indent=6),
        _b("Southern Alberta Institute of Technology (SAIT)", 26, indent=1),
        _b("Psychology and Sociology Diploma", 27),
        _b("September 2021 - May 2023", 28, indent=6),
        _b("Lethbridge Polytechnic", 29, indent=1),
    ]
    return ExtractedDocument(source_format="pdf", blocks=blocks)


def test_reconstructed_resume_parses_well():
    r = build_parsed_resume(_reconstructed_resume())

    assert r.metadata.section_detection_status == "success"
    assert r.contact_info.name == "KADEN BIRCH"
    assert r.contact_info.location == "Calgary, Alberta"   # not the ALL-CAPS tagline
    # Summary joins the wrapped prose lines, not just the first.
    assert r.summary and "customer-facing experience" in r.summary
    assert "working independently" in r.summary

    # Experience: 3 entries, correct title/company, dates backfilled from the
    # separate date lines.
    titles = [(e.title, e.company) for e in r.experience]
    assert titles == [
        ("Machine Operator 1", "City of Lethbridge"),
        ("System Install and Repair tech", "Liberty Security"),
        ("Gas line Maintenance and Repair Tech", "Chief Mountain Gas"),
    ]
    assert r.experience[0].dates.start_date == "2022-05"
    assert r.experience[0].dates.end_date == "2025-08"

    # Education: 2 entries with institutions pulled from their indented lines.
    assert len(r.education) == 2
    assert r.education[0].institution == "Southern Alberta Institute of Technology (SAIT)"
    assert r.education[0].dates.start_date == "2025-01"
    assert r.education[1].institution == "Lethbridge Polytechnic"

    # Volunteer org recognized.
    assert r.volunteer_experience[0].organization.startswith("Church of Jesus Christ")

    # Unrecognized heading captured rather than dropped.
    assert "CORE STRENGTHS" in r.additional_sections


def test_heading_detected_when_slightly_larger_and_not_bold():
    # ~9% larger than body, no bold flag — must still be a heading candidate.
    assert _is_heading_candidate(_b("EDUCATION", 0, HEAD), baseline=BODY)
    # An ALL-CAPS body-size acronym line must NOT be treated as a heading.
    assert not _is_heading_candidate(_b("AWS, SQL, HTTP", 0, BODY), baseline=BODY)


def test_location_ignores_preceding_all_caps_words():
    m = LOCATION_RE.search("INFORMATION TECHNOLOGY SERVICES Calgary, Alberta")
    assert m and m.group(1) == "Calgary" and m.group(2) == "Alberta"


def test_description_drops_date_lines_and_joins_wraps():
    blocks = [
        _b("Acme Co - Operator", 0),
        _b("May 2022 - August 2025", 1, indent=6),     # date on its own line
        _b("(Seasonal)", 2, indent=6),
        _b("Worked with the public to form a clear plan for work to be", 3, indent=2),
        _b("performed.", 4, indent=2),                 # wrapped continuation
    ]
    exp = parse_experience(blocks, BODY, [])
    assert len(exp) == 1
    # The bare date line is not a bullet (it's captured into dates instead).
    assert exp[0].dates.start_date == "2022-05"
    assert all("May 2022" not in d for d in exp[0].description)
    # The wrapped sentence is joined back together.
    assert any(d.endswith("work to be performed.") for d in exp[0].description)


def test_education_current_marker_not_treated_as_field():
    from resume_parser.field_parsers.education import parse_education

    blocks = [
        _b("Information Technology Diploma - Current", 0),
        _b("January 2025 - August 2026", 1, indent=6),
        _b("SAIT", 2, indent=1),
    ]
    edu = parse_education(blocks, BODY, [])
    assert edu[0].field_of_study is None       # "Current" is not a field
    assert edu[0].degree == "Information Technology Diploma"


def test_entry_splits_on_return_to_margin_without_bold():
    # No bold, dates on their own indented lines -> still splits into 2 jobs.
    blocks = [
        _b("Acme Co - Operator", 0),
        _b("2020 - 2021", 1, indent=6),
        _b("Did things.", 2, indent=2),
        _b("Globex - Technician", 3),
        _b("2018 - 2019", 4, indent=6),
        _b("Did other things.", 5, indent=2),
    ]
    exp = parse_experience(blocks, BODY, [])
    assert len(exp) == 2
    assert exp[0].dates.start_date == "2020"   # backfilled from the date line
    assert exp[1].dates.start_date == "2018"
