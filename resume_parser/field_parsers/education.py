"""
Education parsing (blueprint §8, §10).

Degree segments are identified by a small closed set of degree keywords —
a far more reliable signal than the experience title/company problem.
Institutions are confirmed by "University"/"College"/"Institute"-style
words; whatever's left is the field of study. GPA is pulled by regex in any
of the common formats ("3.8", "3.8/4.0", "85%").
"""

from __future__ import annotations

import re
from typing import Optional

from ..normalizer import clean_text, parse_date_range
from ..schema import EducationEntry
from .common import (
    backfill_dates,
    group_entries,
    join_header,
    split_header_description,
    split_segments,
)

# Whole-word degree keywords (and dotted abbreviations handled separately).
_DEGREE_WORDS = {
    "bachelor", "bachelors", "bachelor's", "master", "masters", "master's",
    "associate", "associates", "diploma", "certificate", "doctorate",
    "doctor", "phd", "mba", "bsc", "bs", "ba", "msc", "ms", "ma", "beng",
    "btech", "bcom", "llb", "md", "meng", "bba", "dphil", "edd", "jd",
}
_DEGREE_ABBR_RE = re.compile(
    r"\b(?:b\.?sc|b\.?a|b\.?s|b\.?eng|b\.?tech|b\.?com|b\.?b\.?a|"
    r"m\.?sc|m\.?a|m\.?s|m\.?eng|m\.?b\.?a|ph\.?d|ll\.?b|ll\.?m|"
    r"d\.?phil|ed\.?d|j\.?d)\b",
    re.IGNORECASE,
)
_INSTITUTION_WORDS = {
    "university", "college", "institute", "institution", "school",
    "polytechnic", "academy", "seminary", "conservatory", "université",
}

_GPA_LABELLED = re.compile(
    r"GPA[:\s]*([0-4](?:\.\d{1,2})?)(?:\s*/\s*([0-5](?:\.\d{1,2})?))?", re.I
)
_GPA_RATIO = re.compile(r"\b([0-4]\.\d{1,2})\s*/\s*([0-5](?:\.0)?)\b")
_GPA_PERCENT = re.compile(r"\b(\d{2,3})\s*%")


def parse_education(blocks, baseline, warnings: list[str]) -> list[EducationEntry]:
    entries = []
    for entry in group_entries(blocks, baseline):
        header, _description = split_header_description(entry)
        header_text = join_header(header)
        entry_text = " ".join(filter(None, (clean_text(b.text) for b in entry)))

        dates, remainder = parse_date_range(header_text)
        dates = backfill_dates(dates, entry)
        gpa, remainder = _extract_gpa(remainder)
        if gpa is None:
            gpa, _ = _extract_gpa(entry_text)

        segments = split_segments(remainder)
        degree, institution, field = _assign_education(segments)

        # The institution is often on its own (indented) line rather than in
        # the degree line — scan the whole entry if we didn't find one.
        if institution is None:
            for b in entry:
                line = clean_text(b.text)
                if line and line not in (degree, field) and _has_institution(line):
                    institution = line
                    break

        entries.append(
            EducationEntry(
                institution=institution,
                degree=degree,
                field_of_study=field,
                dates=dates,
                gpa=gpa,
            )
        )
    return entries


def _has_degree(segment: str) -> bool:
    words = {w.strip(".,").lower() for w in segment.split()}
    if words & _DEGREE_WORDS:
        return True
    return bool(_DEGREE_ABBR_RE.search(segment))


def _has_institution(segment: str) -> bool:
    words = {w.strip(".,").lower() for w in segment.split()}
    return bool(words & _INSTITUTION_WORDS)


def _looks_acronym_institution(segment: str) -> bool:
    """Short all-caps token (MIT, SAIT, UCLA) — far more likely a school
    than a field of study, even without a 'University/College' keyword."""
    s = segment.strip()
    return s.isupper() and s.isalpha() and 2 <= len(s) <= 6


# In-progress markers that shouldn't be mistaken for a field of study.
_STATUS_WORDS = {"current", "present", "ongoing", "now", "expected", "anticipated", "in progress"}


def _assign_education(
    segments: list[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    segments = [s for s in segments if s.strip().lower() not in _STATUS_WORDS]
    degree_seg = next((s for s in segments if _has_degree(s)), None)
    degree = degree_seg
    field: Optional[str] = None

    # "Bachelor of Science in Computer Science" -> degree + field_of_study.
    if degree:
        m = re.search(r"\bin\b", degree, re.IGNORECASE)
        if m:
            field = degree[m.end():].strip(" ,")
            degree = degree[: m.start()].strip(" ,")

    institution = next(
        (s for s in segments if s != degree_seg and _has_institution(s)), None
    )

    # Assign leftover segments. An acronym leftover is treated as the
    # institution; otherwise the first leftover fills field_of_study (blueprint
    # "whatever's left is field_of_study"), then any further leftover the
    # institution.
    leftovers = [s for s in segments if s != degree_seg and s != institution]
    for s in leftovers:
        if institution is None and _looks_acronym_institution(s):
            institution = s
        elif field is None:
            field = s
        elif institution is None:
            institution = s

    return degree or None, institution or None, field or None


def _extract_gpa(text: str) -> tuple[Optional[str], str]:
    if not text:
        return None, text
    m = _GPA_LABELLED.search(text)
    if m:
        gpa = m.group(1) + (f"/{m.group(2)}" if m.group(2) else "")
        return gpa, _cut(text, m)
    m = _GPA_RATIO.search(text)
    if m:
        return f"{m.group(1)}/{m.group(2)}", _cut(text, m)
    m = _GPA_PERCENT.search(text)
    if m:
        return f"{m.group(1)}%", _cut(text, m)
    return None, text


def _cut(text: str, m: re.Match) -> str:
    return re.sub(r"\s+", " ", (text[: m.start()] + " " + text[m.end():])).strip(" ,-")
