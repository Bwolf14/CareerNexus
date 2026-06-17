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


def _at_body(b: TextBlock, baseline) -> bool:
    return (
        b.font_size is None
        or baseline is None
        or abs(b.font_size - baseline) <= _SIZE_TOLERANCE
    )


# A header line is a short noun phrase, usually with structural separators
# ("Title | Company | City"); a description paragraph is markedly longer and
# carries none. We avoid sentence-punctuation heuristics because resume headers
# are riddled with abbreviation dots ("Inc.", "Ltd.", "No. 36").
_PROSE_MIN_WORDS = 14
_HEADER_SEPARATORS = "|—–·•"


def _looks_like_prose(text: str) -> bool:
    """A no-bullet description paragraph, vs. a short title/company header."""
    t = text.strip()
    if not t or any(sep in t for sep in _HEADER_SEPARATORS):
        return False
    return len(t.split()) >= _PROSE_MIN_WORDS


def _is_parenthetical(text: str) -> bool:
    """A line that is an aside wrapped in parentheses (not a new entry)."""
    return clean_text(text).startswith("(")


def _is_description_line(b: TextBlock) -> bool:
    """Does this block carry description content rather than header fields?"""
    if b.is_list_item or b.indentation_level > 0:
        return True
    return _looks_like_prose(b.text) or _is_parenthetical(b.text)


def is_entry_start(b: TextBlock, baseline, force_indent0: bool = False) -> bool:
    """A *strong*, unconditional entry start: a bold or dated line at the
    left margin (blueprint §8). ``force_indent0`` (projects) relaxes this to
    "any left-margin, non-list block", since project titles often have neither
    bold nor a date. Multi-line headers and continuation lines are handled by
    :func:`group_entries`, which has the surrounding context this lacks.
    """
    if b.is_list_item or b.indentation_level != 0:
        return False
    if force_indent0:
        return True
    return _at_body(b, baseline) and (
        b.is_bold or bool(DATE_RANGE_PATTERN.search(b.text))
    )


def _is_entry_boundary(
    b: TextBlock, baseline, seen_content: bool, seen_date: bool, force_indent0: bool
) -> bool:
    """Decide whether *b* begins a new entry, given what the current entry has
    already absorbed.

    The key idea is that an entry is a short *header block* (title / company /
    date / location, possibly spread over several lines) followed by a
    description. A new entry only begins on a strong signal:

    * a **bold** body-size line at the margin (always), or
    * once the current entry's header is *complete* — a date was captured, or
      we're already into the description — a return to a margin, body-size line
      that isn't itself prose/parenthetical description.

    While the header is still open (no date and no description seen yet),
    non-bold continuation lines — including the date line — *attach* to the
    current entry instead of splitting it. (Projects are the exception: with
    ``force_indent0`` a fresh, non-date line still starts a new entry, since
    project titles carry no other signal.)
    """
    if b.is_list_item or b.indentation_level != 0:
        return False
    at_body = _at_body(b, baseline)
    if at_body and b.is_bold:
        return True

    header_open = not seen_content and not seen_date
    if header_open:
        if force_indent0 and not _is_date_only(clean_text(b.text)):
            return True
        return False

    if not at_body:
        return False
    if _looks_like_prose(b.text) or _is_parenthetical(b.text):
        return False
    return True


def group_entries(blocks, baseline, force_indent0: bool = False) -> list[list[TextBlock]]:
    """Group a section's blocks into entries (blueprint §8).

    Tracks two facts about the entry under construction: whether a date range
    has been captured (``seen_date``, which "completes" a header) and whether
    description content has begun (``seen_content``). A header may therefore
    span several lines — e.g. company, then title, then a separate date line —
    without being torn into separate entries, while genuinely back-to-back
    entries (title line, date line, next title line) still split correctly.
    See :func:`_is_entry_boundary` for the rule.
    """
    entries: list[list[TextBlock]] = []
    current: list[TextBlock] = []
    seen_content = False
    seen_date = False
    for b in blocks:
        if current and _is_entry_boundary(
            b, baseline, seen_content, seen_date, force_indent0
        ):
            entries.append(current)
            current = []
            seen_content = False
            seen_date = False
        current.append(b)
        if _is_description_line(b):
            seen_content = True
        elif b.indentation_level == 0 and DATE_RANGE_PATTERN.search(b.text):
            seen_date = True
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
        # Description begins at the first bullet/indented line, or — for resumes
        # that write descriptions as plain paragraphs — the first prose line.
        if not in_desc and header and _is_description_line(b):
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


def join_header(header_blocks: list[TextBlock]) -> str:
    """Join header lines with a separator so each line stays a distinct segment.

    A multi-line header ("Company" / "Title" / "Dates") would otherwise be
    space-joined and lose its boundaries, merging e.g. a field-of-study line
    into the institution line. The "|" delimiter is one ``split_segments``
    already recognises.
    """
    return " | ".join(filter(None, (clean_text(b.text) for b in header_blocks)))


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


_DELIM_CHARS = ",;|•·●▪‣"


def split_delim(text: str) -> list[str]:
    """Split a skills/technologies list on commas/semicolons/pipes/bullets.

    Splitting is *parenthesis-aware*: delimiters inside brackets don't split, so
    "AWS (EC2, RDS, S3)" stays one item instead of fragmenting. Deliberately
    does NOT split on '+' or '/' either, so compound skill names like 'C++',
    'CI/CD', and 'TCP/IP' survive intact.
    """
    if not text:
        return []
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif depth == 0 and ch in _DELIM_CHARS:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]
