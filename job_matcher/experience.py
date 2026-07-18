"""
Career-experience estimation: how many years has this person worked?

Used to pre-fill the experience slider on the follow-up questions page. The
estimate merges the resume's work-experience date ranges (so overlapping or
back-to-back jobs aren't double-counted) and is always user-correctable — the
answer the user submits wins over anything inferred here.

Resume dates arrive as "YYYY" or "YYYY-MM" strings (see resume_parser.schema);
anything unparseable is skipped rather than guessed.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

MAX_YEARS = 40  # the slider tops out at "40+"

_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{1,2}))?")


def _to_months(value: Optional[str], *, default_month: int) -> Optional[int]:
    """"YYYY[-MM]" → months since year 0, or None if unparseable."""
    m = _DATE_RE.match(str(value or "").strip())
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else default_month
    if not (1900 <= year <= 2100 and 1 <= month <= 12):
        return None
    return year * 12 + (month - 1)


def _intervals(parsed: dict[str, Any]) -> list[tuple[int, int]]:
    today = date.today()
    now_months = today.year * 12 + (today.month - 1)
    out: list[tuple[int, int]] = []
    for exp in parsed.get("experience") or []:
        dates = exp.get("dates") or {}
        start = _to_months(dates.get("start_date"), default_month=1)
        if start is None:
            continue
        if dates.get("is_current"):
            end = now_months
        else:
            # A missing month on the end date means "through that year".
            end = _to_months(dates.get("end_date"), default_month=12)
            if end is None:
                end = start  # unknown duration: count the job, not its length
        end = min(end, now_months)
        if end >= start:
            out.append((start, end))
    return out


def estimate_experience_years(parsed: dict[str, Any]) -> Optional[int]:
    """Total years worked, from the union of the resume's experience ranges.

    Overlapping/adjacent jobs are merged so moonlighting doesn't double-count.
    Returns None when no experience entry has a usable start date, else an int
    clamped to 0–MAX_YEARS.
    """
    intervals = sorted(_intervals(parsed))
    if not intervals:
        return None
    total = 0
    cur_start, cur_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= cur_end + 1:  # overlapping or contiguous months
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start + 1
            cur_start, cur_end = start, end
    total += cur_end - cur_start + 1
    return max(0, min(MAX_YEARS, round(total / 12)))


def experience_level_label(years: int) -> str:
    """Human label for a years-of-experience figure (mirrored in the form JS)."""
    if years <= 1:
        return "Entry level"
    if years <= 4:
        return "Junior / early career"
    if years <= 9:
        return "Intermediate / senior"
    if years <= 14:
        return "Senior / lead"
    if years <= 24:
        return "Management / principal"
    return "Executive / senior leadership"
