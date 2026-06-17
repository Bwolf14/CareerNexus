"""
Volunteer-experience parsing (blueprint §8, §10).

Structurally identical to experience parsing, but the two header fields are
``organization`` and ``role``. As with title/company, the ordering is
ambiguous, so we bias with role keywords and organization-name hints and
fall back to a "role, organization" default — logging a warning whenever the
assignment was a guess.
"""

from __future__ import annotations

from typing import Optional

from ..normalizer import parse_date_range
from ..schema import VolunteerEntry
from .common import (
    backfill_dates,
    extract_location,
    group_entries,
    join_header,
    split_header_description,
    split_segments,
)

ROLE_KEYWORDS = {
    "volunteer", "coordinator", "mentor", "tutor", "assistant", "organizer",
    "leader", "ambassador", "helper", "counselor", "counsellor", "captain",
    "president", "secretary", "treasurer", "member", "fundraiser", "instructor",
    "chair", "chairperson", "facilitator", "steward", "docent", "aide",
}
ORG_KEYWORDS = {
    "foundation", "society", "association", "club", "center", "centre",
    "charity", "organization", "ngo", "council", "league", "church", "shelter",
    "hospital", "library", "bank", "team", "committee", "ministry", "mission",
    "corps", "alliance", "network", "coalition", "institute",
}


def parse_volunteer(blocks, baseline, warnings: list[str]) -> list[VolunteerEntry]:
    entries = []
    for idx, entry in enumerate(group_entries(blocks, baseline), start=1):
        header, description = split_header_description(entry)
        header_text = join_header(header)

        dates, remainder = parse_date_range(header_text)
        dates = backfill_dates(dates, entry)
        _location, remainder = extract_location(remainder)
        segments = split_segments(remainder)
        role, organization = _assign_role_org(segments, warnings, idx)

        entries.append(
            VolunteerEntry(
                organization=organization,
                role=role,
                dates=dates,
                description=description,
            )
        )
    return entries


def _looks_role(segment: str) -> bool:
    words = {w.strip(".,").lower() for w in segment.split()}
    return bool(words & ROLE_KEYWORDS)


def _looks_org(segment: str) -> bool:
    words = {w.strip(".,").lower() for w in segment.split()}
    return bool(words & ORG_KEYWORDS)


def _assign_role_org(
    segments: list[str], warnings: list[str], idx: int
) -> tuple[Optional[str], Optional[str]]:
    if not segments:
        return None, None
    if len(segments) == 1:
        s = segments[0]
        if _looks_org(s) and not _looks_role(s):
            return None, s
        return s, None

    role = next((s for s in segments if _looks_role(s)), None)
    org = next((s for s in segments if _looks_org(s) and not _looks_role(s)), None)

    if role and org and role != org:
        return role, org
    if role and not org:
        other = next((s for s in segments if s != role), None)
        return role, other
    if org and not role:
        other = next((s for s in segments if s != org), None)
        return other, org

    warnings.append(
        f"Volunteer entry {idx}: could not confidently distinguish role from "
        f"organization; defaulted to 'role, organization' order."
    )
    return segments[0], segments[1]
