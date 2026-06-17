"""
Shared field-parsing helpers (blueprint §8, §10).

* **Entry-boundary detection** for the repeated-entry sections
  (experience / education / projects / volunteer): group a section's blocks
  into entries, then split each entry into a header run and description
  bullets.
* **Header splitting**: break a header string into 2-3 candidate segments
  on the usual separators, after the date and location have been pulled out.
* **Delimiter splitting** for skills / technologies lists.
"""

from __future__ import annotations

import re

from ..intermediate_representation import TextBlock
from ..normalizer import DATE_RANGE_PATTERN, clean_text, parse_date_range
from ..schema import DateRange
from .contact import LOCATION_RE

# Tolerance (pt) for "at body size" in entry-start detection.
_SIZE_TOLERANCE = 1.5


def is_entry_start(b: TextBlock, baseline, force_indent0: bool = False) -> bool:
    """Does *b* begin a new entry? (blueprint §8)

    Default rule: a left-margin, non-list, body-size block that is bold or
    contains a date range. ``force_indent0`` (projects) relaxes this to "any
    left-margin, non-list block", since project titles often have neither
    bold nor a date.
    """
    if b.is_list_item or b.indentation_level != 0:
        return False
    if force_indent0:
        return True
    return _at_body(b, baseline) and (
        b.is_bold or bool(DATE_RANGE_PATTERN.search(b.text))
    )


def _at_body(b: TextBlock, baseline) -> bool:
    return (
        b.font_size is None
        or baseline is None
        or abs(b.font_size - baseline) <= _SIZE_TOLERANCE
    )


def group_entries(blocks, baseline, force_indent0: bool = False) -> list[list[TextBlock]]:
    """Group a section's blocks into entries.

    Besides the §8 rule (bold / date at the left margin), a block also starts
    a new entry when text **returns to the left margin after indented or
    bulleted content** — the reliable boundary signal for resumes that use no
    bold and put dates on their own indented lines.
    """
    entries: list[list[TextBlock]] = []
    current: list[TextBlock] = []
    seen_indented = False
    for b in blocks:
        start = is_entry_start(b, baseline, force_indent0)
        if (
            not start
            and current
            and seen_indented
            and not b.is_list_item
            and b.indentation_level == 0
            and _at_body(b, baseline)
        ):
            start = True
        if start and current:
            entries.append(current)
            current = []
            seen_indented = False
        current.append(b)
        if b.is_list_item or b.indentation_level > 0:
            seen_indented = True
    if current:
        entries.append(current)
    return entries


def backfill_dates(dates: DateRange, entry: list[TextBlock]) -> DateRange:
    """If the entry header carried no date, scan the whole entry for one.

    Common in resumes that place the date on its own (often right-aligned)
    line instead of in the title/company line.
    """
    if dates.start_date or dates.is_current:
        return dates
    text = " ".join(filter(None, (clean_text(b.text) for b in entry)))
    scanned, _ = parse_date_range(text)
    if scanned.start_date or scanned.is_current:
        return scanned
    return dates


# A previous description line ending in one of these is almost certainly
# mid-sentence and continues onto the next line.
_CONTINUATION_END = re.compile(
    r"(?:[,&]|\b(?:and|or|of|the|to|for|with|in|on|at|a|an|as|by)\b)$", re.I
)


def _is_date_only(text: str) -> bool:
    """A line that is just a date range (optionally with stray punctuation)."""
    dates, remainder = parse_date_range(text)
    if dates.start_date is None and not dates.is_current:
        return False
    return not re.sub(r"[^A-Za-z]", "", remainder)


def split_header_description(entry: list[TextBlock]) -> tuple[list[TextBlock], list[str]]:
    """Leading non-list/level-0 run = header; the rest = description lines.

    Description lines are cleaned up: pure date lines (common when the date
    sits on its own row) are dropped, and lines that are clearly wrapped
    continuations — a non-list line starting lowercase, or following a line
    that ended mid-clause — are joined back onto the previous line.
    """
    header: list[TextBlock] = []
    raw: list[tuple[str, bool]] = []  # (text, is_list_item)
    in_desc = False
    for b in entry:
        if not in_desc and (b.is_list_item or b.indentation_level > 0):
            in_desc = True
        if in_desc:
            t = clean_text(b.text)
            if t:
                raw.append((t, b.is_list_item))
        else:
            header.append(b)

    description: list[str] = []
    for text, is_list in raw:
        if _is_date_only(text):
            continue
        if (
            description
            and not is_list
            and (text[:1].islower() or _CONTINUATION_END.search(description[-1]))
        ):
            description[-1] = f"{description[-1]} {text}".strip()
        else:
            description.append(text)
    return header, description


_SEGMENT_SPLIT_RE = re.compile(
    r"\s*[—–]\s*|\s+-\s+|\s*\|\s*|\s*[•·]\s*|\s*,\s*|\s*\(\s*|\s*\)\s*|\s+\bat\b\s+",
    re.IGNORECASE,
)


def split_segments(text: str) -> list[str]:
    """Break a header remainder into candidate segments (title/company/etc.)."""
    if not text:
        return []
    return [p.strip() for p in _SEGMENT_SPLIT_RE.split(text) if p and p.strip()]


def extract_location(text: str) -> tuple[str | None, str]:
    """Pull a 'City, ST' style location out of *text*; return (loc, remainder).

    Done *before* segment splitting so the comma inside a location doesn't
    get torn apart.
    """
    if not text:
        return None, text
    m = LOCATION_RE.search(text)
    if not m:
        return None, text
    loc = f"{m.group(1)}, {m.group(2)}"
    remainder = (text[: m.start()] + " " + text[m.end():]).strip()
    remainder = re.sub(r"\s+", " ", remainder).strip(" -–—|,")
    return loc, remainder


_DELIM_RE = re.compile(r"\s*[,;|]\s*|\s*[•·●▪‣]\s*")


def split_delim(text: str) -> list[str]:
    """Split a skills/technologies list on commas/semicolons/pipes/bullets.

    Deliberately does NOT split on '+' or '/', so compound skill names like
    'C++', 'CI/CD', and 'TCP/IP' survive intact.
    """
    if not text:
        return []
    return [p.strip() for p in _DELIM_RE.split(text) if p and p.strip()]
