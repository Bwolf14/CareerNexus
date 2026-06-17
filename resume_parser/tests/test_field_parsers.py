"""Unit tests for the field parsers, driven by synthetic TextBlock lists."""

from __future__ import annotations

from resume_parser.field_parsers import (
    parse_certifications,
    parse_contact,
    parse_education,
    parse_experience,
    parse_projects,
    parse_skills,
    parse_volunteer,
)
from resume_parser.tests.helpers import block

BASELINE = 11.0


# --------------------------------------------------------------------------
# contact
# --------------------------------------------------------------------------
def test_contact_extracts_all_fields():
    blocks = [
        block("Jane Doe", 0, font_size=18.0, is_bold=True),
        block("jane.doe@example.com | (403) 555-0123 | Lethbridge, AB", 1, font_size=10.0),
        block("linkedin.com/in/janedoe github.com/janedoe myportfolio.dev", 2, font_size=10.0),
    ]
    warnings: list[str] = []
    info = parse_contact(blocks, warnings)
    assert info.name == "Jane Doe"
    assert info.email == "jane.doe@example.com"
    assert "555-0123" in info.phone
    assert info.location == "Lethbridge, AB"
    assert info.links.linkedin == "linkedin.com/in/janedoe"
    assert info.links.github == "github.com/janedoe"
    assert warnings == []  # everything found


def test_contact_phone_format_variants():
    for raw in ["(403) 555-0123", "403-555-0123", "+1 403 555 0123", "403.555.0123"]:
        info = parse_contact([block(f"a@b.com {raw}", 0)], [])
        assert info.phone is not None and "555" in info.phone


def test_contact_missing_fields_warn():
    warnings: list[str] = []
    info = parse_contact([block("Just A Name", 0, font_size=16.0)], warnings)
    assert info.email is None
    assert any("email" in w.lower() for w in warnings)
    assert any("location" in w.lower() for w in warnings)


# --------------------------------------------------------------------------
# experience — title/company assignment both ways + default warning
# --------------------------------------------------------------------------
def test_experience_keyword_title_then_company():
    blocks = [
        block("Network Technician — Foothills IT Inc. (Jan 2022 - Present)", 0, font_size=11.0, is_bold=True),
        block("Maintained switching for 12 sites.", 1, font_size=11.0, is_list_item=True, indentation_level=1),
    ]
    warnings: list[str] = []
    exp = parse_experience(blocks, BASELINE, warnings)
    assert len(exp) == 1
    assert exp[0].title == "Network Technician"
    assert exp[0].company == "Foothills IT Inc."
    assert exp[0].dates.start_date == "2022-01"
    assert exp[0].dates.is_current is True
    assert exp[0].description == ["Maintained switching for 12 sites."]
    assert warnings == []


def test_experience_company_first_resolved_by_keywords():
    # Company appears first; keyword bias should still place it correctly.
    blocks = [
        block("Globex LLC — Software Engineer (2019 - 2021)", 0, font_size=11.0, is_bold=True),
    ]
    warnings: list[str] = []
    exp = parse_experience(blocks, BASELINE, warnings)
    assert exp[0].company == "Globex LLC"
    assert exp[0].title == "Software Engineer"
    assert warnings == []


def test_experience_default_guess_logs_warning():
    # Neither segment carries a title/company keyword -> default + warning.
    blocks = [
        block("Riverside Bakery — Sunny Acres (2018 - 2019)", 0, font_size=11.0, is_bold=True),
    ]
    warnings: list[str] = []
    exp = parse_experience(blocks, BASELINE, warnings)
    assert exp[0].title == "Riverside Bakery"   # default: first = title
    assert exp[0].company == "Sunny Acres"
    assert any("title" in w.lower() and "company" in w.lower() for w in warnings)


def test_experience_multiple_entries_split():
    blocks = [
        block("Engineer — Acme Inc. (2020 - 2022)", 0, font_size=11.0, is_bold=True),
        block("Did things.", 1, font_size=11.0, is_list_item=True, indentation_level=1),
        block("Analyst — Globex Corp (2018 - 2020)", 2, font_size=11.0, is_bold=True),
        block("Did other things.", 3, font_size=11.0, is_list_item=True, indentation_level=1),
    ]
    exp = parse_experience(blocks, BASELINE, [])
    assert len(exp) == 2
    assert exp[0].company == "Acme Inc."
    assert exp[1].company == "Globex Corp"


def test_experience_extracts_location():
    blocks = [
        block("Engineer — Acme Inc. — Calgary, AB (2020 - 2022)", 0, font_size=11.0, is_bold=True),
    ]
    exp = parse_experience(blocks, BASELINE, [])
    assert exp[0].location == "Calgary, AB"
    assert exp[0].title == "Engineer"
    assert exp[0].company == "Acme Inc."


# --------------------------------------------------------------------------
# education — degree keywords, institution, field, GPA formats
# --------------------------------------------------------------------------
def test_education_degree_field_institution():
    blocks = [
        block("Bachelor of Science in Computer Science — University of Calgary (2017 - 2021)", 0, font_size=11.0, is_bold=True),
    ]
    edu = parse_education(blocks, BASELINE, [])
    assert edu[0].degree == "Bachelor of Science"
    assert edu[0].field_of_study == "Computer Science"
    assert edu[0].institution == "University of Calgary"
    assert edu[0].dates.start_date == "2017"


def test_education_acronym_institution():
    blocks = [block("Diploma in IT — SAIT (2018 - 2020)", 0, font_size=11.0, is_bold=True)]
    edu = parse_education(blocks, BASELINE, [])
    assert edu[0].degree == "Diploma"
    assert edu[0].institution == "SAIT"


def test_education_gpa_formats():
    cases = {
        "BSc — UCLA (2020), GPA: 3.8/4.0": "3.8/4.0",
        "BSc — UCLA (2020), GPA 3.8": "3.8",
        "BSc — UCLA (2020), 85%": "85%",
    }
    for text, expected in cases.items():
        edu = parse_education([block(text, 0, font_size=11.0, is_bold=True)], BASELINE, [])
        assert edu[0].gpa == expected


# --------------------------------------------------------------------------
# skills — three layouts + dedupe
# --------------------------------------------------------------------------
def test_skills_single_delimited_block():
    skills = parse_skills([block("Python, Docker, Linux, SQL", 0)], [])
    assert skills.raw == ["Python", "Docker", "Linux", "SQL"]
    assert skills.categorized == {}


def test_skills_categorized():
    blocks = [
        block("Programming: Python, Bash, C++", 0),
        block("Infrastructure: Docker, Linux", 1),
    ]
    skills = parse_skills(blocks, [])
    assert skills.categorized["Programming"] == ["Python", "Bash", "C++"]
    assert skills.categorized["Infrastructure"] == ["Docker", "Linux"]
    # Flattened into raw too.
    assert "Python" in skills.raw and "Docker" in skills.raw


def test_skills_one_per_bullet_and_dedupe():
    blocks = [
        block("Python", 0, is_list_item=True),
        block("Docker", 1, is_list_item=True),
        block("python", 2, is_list_item=True),  # case-insensitive duplicate
    ]
    skills = parse_skills(blocks, [])
    assert skills.raw == ["Python", "Docker"]


def test_skills_preserve_compound_names():
    skills = parse_skills([block("C++, CI/CD, TCP/IP", 0)], [])
    assert skills.raw == ["C++", "CI/CD", "TCP/IP"]


# --------------------------------------------------------------------------
# certifications
# --------------------------------------------------------------------------
def test_certifications_name_issuer_date():
    certs = parse_certifications([block("CompTIA A+ — CompTIA (2024)", 0)], [])
    assert certs[0].name == "CompTIA A+"
    assert certs[0].issuer == "CompTIA"
    assert certs[0].date_earned == "2024"


def test_certifications_expiration():
    certs = parse_certifications(
        [block("CPR Certification — Red Cross, Issued 2023, Expires 2025", 0)], []
    )
    assert certs[0].date_earned == "2023"
    assert certs[0].expiration_date == "2025"


def test_certification_name_always_present():
    certs = parse_certifications([block("Some Random Credential", 0)], [])
    assert certs[0].name == "Some Random Credential"


# --------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------
def test_projects_title_tech_url_description():
    blocks = [
        block("Home Lab Setup", 0, font_size=11.0, is_bold=True),
        block("Built a Proxmox cluster with pfSense routing.", 1, is_list_item=True, indentation_level=1),
        block("Technologies: Proxmox, pfSense, Docker", 2, is_list_item=True, indentation_level=1),
        block("https://github.com/me/homelab", 3, is_list_item=True, indentation_level=1),
    ]
    projects = parse_projects(blocks, BASELINE, [])
    assert projects[0].title == "Home Lab Setup"
    assert projects[0].technologies == ["Proxmox", "pfSense", "Docker"]
    assert projects[0].url == "https://github.com/me/homelab"
    assert "Proxmox cluster" in projects[0].description


def test_projects_untitled_entries_split_on_indent():
    blocks = [
        block("First Project", 0, font_size=11.0),  # not bold, but indent 0
        block("Detail one.", 1, is_list_item=True, indentation_level=1),
        block("Second Project", 2, font_size=11.0),
        block("Detail two.", 3, is_list_item=True, indentation_level=1),
    ]
    projects = parse_projects(blocks, BASELINE, [])
    assert [p.title for p in projects] == ["First Project", "Second Project"]


# --------------------------------------------------------------------------
# volunteer
# --------------------------------------------------------------------------
def test_volunteer_role_and_organization():
    blocks = [
        block("Volunteer Tutor — Public Library (2021 - 2022)", 0, font_size=11.0, is_bold=True),
        block("Helped students with reading.", 1, is_list_item=True, indentation_level=1),
    ]
    vol = parse_volunteer(blocks, BASELINE, [])
    assert vol[0].role == "Volunteer Tutor"
    assert vol[0].organization == "Public Library"
    assert vol[0].description == ["Helped students with reading."]
