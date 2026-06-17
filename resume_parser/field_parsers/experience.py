"""
Experience / work-history parsing (blueprint §8, §10).

Entry grouping -> header parsing -> bullet descriptions. Title-vs-company
assignment is the honestly-weakest part of a rule-based parser: ordering
varies by template with no universal convention. We bias with job-title
keywords and company-name suffixes, and fall back to the North-American
"title, then company" default — *always logging a warning when the
assignment was a guess* so the downstream resume-guidance feature can
surface it.
"""

from __future__ import annotations

from typing import Optional

from ..normalizer import clean_text, parse_date_range
from ..schema import ExperienceEntry
from .common import (
    backfill_dates,
    extract_location,
    group_entries,
    split_header_description,
    split_segments,
)

TITLE_KEYWORDS = {
    "technician", "engineer", "analyst", "manager", "specialist", "developer",
    "coordinator", "administrator", "labourer", "laborer", "intern", "consultant",
    "designer", "architect", "lead", "director", "officer", "assistant",
    "associate", "supervisor", "clerk", "representative", "agent", "accountant",
    "nurse", "teacher", "programmer", "scientist", "researcher", "operator",
    "technologist", "strategist", "planner", "advisor", "adviser", "cashier",
    "server", "driver", "writer", "editor", "recruiter", "trainer", "instructor",
    "president", "founder", "owner", "principal", "vp",
    "ceo", "cto", "cfo", "coo", "foreman", "apprentice", "steward",
    "tech", "technician", "operator", "mechanic", "electrician", "plumber",
    "carpenter", "welder", "machinist", "barista", "bartender", "stocker",
}

COMPANY_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "llp", "lp", "group", "holdings",
    "technologies", "solutions", "systems", "services", "industries",
    "enterprises", "partners", "associates", "consulting", "labs",
}
COMPANY_HINT_WORDS = COMPANY_SUFFIXES | {
    "university", "college", "institute", "hospital", "bank", "agency",
}


def parse_experience(blocks, baseline, warnings: list[str]) -> list[ExperienceEntry]:
    entries = []
    for idx, entry in enumerate(group_entries(blocks, baseline), start=1):
        header, description = split_header_description(entry)
        header_text = " ".join(filter(None, (clean_text(b.text) for b in header)))

        dates, remainder = parse_date_range(header_text)
        dates = backfill_dates(dates, entry)
        location, remainder = extract_location(remainder)
        segments = split_segments(remainder)
        title, company = _assign_title_company(segments, warnings, idx)

        entries.append(
            ExperienceEntry(
                title=title,
                company=company,
                location=location,
                dates=dates,
                description=description,
            )
        )
    return entries


def _looks_title(segment: str) -> bool:
    words = {w.strip(".,").lower() for w in segment.split()}
    return bool(words & TITLE_KEYWORDS)


def _looks_company(segment: str) -> bool:
    words = {w.strip(".,").lower() for w in segment.split()}
    return bool(words & COMPANY_HINT_WORDS)


def _assign_title_company(
    segments: list[str], warnings: list[str], idx: int
) -> tuple[Optional[str], Optional[str]]:
    if not segments:
        return None, None
    if len(segments) == 1:
        s = segments[0]
        if _looks_company(s) and not _looks_title(s):
            return None, s
        return s, None

    title = next((s for s in segments if _looks_title(s)), None)
    company = next(
        (s for s in segments if _looks_company(s) and not _looks_title(s)), None
    )

    if title and company and title != company:
        return title, company
    if title and not company:
        other = next((s for s in segments if s != title), None)
        return title, other
    if company and not title:
        other = next((s for s in segments if s != company), None)
        return other, company

    # Neither keyword heuristic fired (or both pointed at the same segment):
    # default to "title, company" and flag it as a low-confidence guess.
    warnings.append(
        f"Experience entry {idx}: could not confidently distinguish job title "
        f"from company; defaulted to 'title, company' order."
    )
    return segments[0], segments[1]
