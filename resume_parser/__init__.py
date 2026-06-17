"""
Career Nexus Resume Parser
==========================

Parse PDF and DOCX resumes into a validated, JSON-serializable
``ParsedResume`` object — pure parsing logic, no AI/LLM dependency.

Quick start
-----------

    from resume_parser import parse_resume

    resume = parse_resume("alex_smith_resume.pdf")
    print(resume.model_dump_json(indent=2))
"""

from __future__ import annotations

from .exceptions import CorruptFileError, ResumeParserError, UnsupportedFormatError
from .parser import PARSER_VERSION, build_parsed_resume, detect_format, parse_resume
from .schema import (
    Certification,
    ContactInfo,
    DateRange,
    EducationEntry,
    ExperienceEntry,
    Links,
    ParsedResume,
    ParserMetadata,
    Project,
    Skills,
    VolunteerEntry,
)

__version__ = PARSER_VERSION

__all__ = [
    "parse_resume",
    "detect_format",
    "build_parsed_resume",
    "ParsedResume",
    "ParserMetadata",
    "ContactInfo",
    "Links",
    "DateRange",
    "ExperienceEntry",
    "EducationEntry",
    "Skills",
    "Certification",
    "Project",
    "VolunteerEntry",
    "ResumeParserError",
    "UnsupportedFormatError",
    "CorruptFileError",
    "PARSER_VERSION",
]
