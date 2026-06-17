"""
Resume Parser Output Schema — Career Nexus
============================================

This module defines the data contract between the resume parser and every
downstream component (conversational profiling chatbot, compatibility
scoring, gap analysis, resume guidance, cover letter assistance).

Design principles
------------------

1. Structure only, no validation logic here. Field-quality checks (is this
   really an email, does this date make sense) belong to the parser's
   field-parser and normalizer modules. If a value can't be confidently
   extracted or normalized, the parser sets the field to None/empty and
   records a message in `metadata.warnings` — it does NOT raise. This keeps
   the schema itself permissive so one bad field never blocks the whole
   output.

2. All section lists (experience, education, skills, etc.) are always
   present, even when empty. Consumers never need to check for missing
   keys — only empty values.

3. `raw_text` is always populated, regardless of how well structured
   parsing went. It's the guaranteed fallback context for the AI profiling
   step, and the only reliable field when `section_detection_status` is
   "failed".

4. Dates are strings in "YYYY-MM" or "YYYY" format (partial dates are
   common on resumes), not native date objects. `is_current` is a separate
   boolean rather than overloading end_date with values like "Present", to
   keep date fields consistently machine-parseable.

5. `DateRange` is shared across experience, education, volunteer, and
   project entries to avoid duplicating the same three fields four times.

6. `additional_sections` captures content under headings that don't match
   any canonical section (Awards, Languages, Publications, etc.), keyed by
   the resume's own heading text. Because the segmenter uses exact-match
   dictionary lookups (no fuzzy matching), unrecognized headings land here
   instead of being merged into the wrong section or silently dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


class DateRange(BaseModel):
    start_date: Optional[str] = None  # "YYYY" or "YYYY-MM"
    end_date: Optional[str] = None    # "YYYY" or "YYYY-MM"; None if is_current
    is_current: bool = False


class Links(BaseModel):
    linkedin: Optional[str] = None
    github: Optional[str] = None
    portfolio: Optional[str] = None
    other: list[str] = Field(default_factory=list)


class ContactInfo(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    links: Links = Field(default_factory=Links)


class ExperienceEntry(BaseModel):
    company: Optional[str] = None
    title: Optional[str] = None
    location: Optional[str] = None
    dates: DateRange = Field(default_factory=DateRange)
    description: list[str] = Field(default_factory=list)


class EducationEntry(BaseModel):
    institution: Optional[str] = None
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    dates: DateRange = Field(default_factory=DateRange)
    gpa: Optional[str] = None  # string — formats vary ("3.8", "3.8/4.0", "85%")


class Skills(BaseModel):
    raw: list[str] = Field(default_factory=list)
    categorized: dict[str, list[str]] = Field(default_factory=dict)


class Certification(BaseModel):
    name: str
    issuer: Optional[str] = None
    date_earned: Optional[str] = None
    expiration_date: Optional[str] = None


class Project(BaseModel):
    title: str
    description: Optional[str] = None
    technologies: list[str] = Field(default_factory=list)
    url: Optional[str] = None
    dates: DateRange = Field(default_factory=DateRange)


class VolunteerEntry(BaseModel):
    organization: Optional[str] = None
    role: Optional[str] = None
    dates: DateRange = Field(default_factory=DateRange)
    description: list[str] = Field(default_factory=list)


class ParserMetadata(BaseModel):
    source_format: Literal["pdf", "docx"]
    parser_version: str
    parsed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # "success": all expected sections found and parsed.
    # "partial": some sections found, others missing or low-confidence.
    # "failed":  no recognizable section structure — raw_text is the only
    #            reliable field, all section lists will be empty.
    section_detection_status: Literal["success", "partial", "failed"] = "success"

    warnings: list[str] = Field(default_factory=list)


class ParsedResume(BaseModel):
    metadata: ParserMetadata
    contact_info: ContactInfo = Field(default_factory=ContactInfo)
    summary: Optional[str] = None
    experience: list[ExperienceEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    skills: Skills = Field(default_factory=Skills)
    certifications: list[Certification] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    volunteer_experience: list[VolunteerEntry] = Field(default_factory=list)

    # Keyed by the resume's own heading text (e.g. "Awards", "Languages").
    # Holds raw lines for headings that didn't match the section dictionary.
    additional_sections: dict[str, list[str]] = Field(default_factory=dict)

    raw_text: str


if __name__ == "__main__":
    # Minimal example showing the shape of a fully-populated record.
    example = ParsedResume(
        metadata=ParserMetadata(source_format="pdf", parser_version="0.1.0"),
        contact_info=ContactInfo(
            name="Jane Doe",
            email="jane@example.com",
            location="Lethbridge, AB",
            links=Links(linkedin="linkedin.com/in/janedoe"),
        ),
        summary="IT student with homelab and networking experience.",
        experience=[
            ExperienceEntry(
                company="Acme Corp",
                title="IT Support Technician",
                location="Lethbridge, AB",
                dates=DateRange(start_date="2023-05", is_current=True),
                description=[
                    "Resolved tier-1 helpdesk tickets",
                    "Maintained inventory of IT assets",
                ],
            )
        ],
        education=[
            EducationEntry(
                institution="SAIT",
                degree="Diploma",
                field_of_study="Information Technology",
                dates=DateRange(start_date="2025-09", is_current=True),
            )
        ],
        skills=Skills(
            raw=["Python", "Docker", "Linux", "Active Directory"],
            categorized={
                "Programming": ["Python"],
                "Infrastructure": ["Docker", "Linux", "Active Directory"],
            },
        ),
        additional_sections={
            "Languages": ["English (native)", "French (conversational)"],
        },
        raw_text="Jane Doe ... (full extracted resume text)",
    )

    print(example.model_dump_json(indent=2))
