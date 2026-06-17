"""
Normalization utilities — dates and text cleanup (blueprint §6, §8, §10).

Two responsibilities:

1. **Text cleanup** applied to every string before it lands in the schema:
   collapse repeated whitespace, strip stray bullet glyphs left over from
   PDF extraction, and rejoin words split by line-break hyphenation
   ("compu-\\nter" -> "computer").

2. **Date parsing / normalization.** Resume dates are messy and partial.
   We accept month-name, numeric, and year-only forms, recognize a small
   set of range separators and "present"-style tokens, and emit the
   schema's canonical ``"YYYY-MM"`` / ``"YYYY"`` strings via a shared
   ``DateRange``.

Nothing here raises on bad input — unparseable dates become ``None`` and
let the caller record a warning, consistent with the schema's
"permissive, never block the whole output" principle.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from dateutil import parser as _dateutil_parser

from .schema import DateRange

# --------------------------------------------------------------------------
# Bullet glyphs (blueprint §6 is_list_item list). Two important nuances:
#
# * Dashes (-, –, —) are NOT bullets here — they double as date-range and
#   header separators that downstream parsing needs.
# * Glyphs like "•" and "·" double as *inline separators* ("Title · Company",
#   "email • phone"). So clean_text only strips bullet glyphs at the EDGES of
#   a string (decorative leftovers); internal ones survive so split_segments /
#   split_delim can use them as separators.
# --------------------------------------------------------------------------
_BULLET_CHARS = "•◦▪‣·●○■▶►➤➢❖✦"
BULLET_GLYPHS = _BULLET_CHARS + "*"

_LEADING_BULLET_UNICODE = re.compile(r"^\s*[" + re.escape(_BULLET_CHARS) + r"]\s*")
_LEADING_BULLET_ASCII = re.compile(r"^\s*[-*]\s+")
_EDGE_BULLETS = re.compile(
    r"^[\s" + re.escape(_BULLET_CHARS) + r"]+|[\s" + re.escape(_BULLET_CHARS) + r"]+$"
)
_HYPHEN_LINEBREAK = re.compile(r"(\w)[-­]\s*\n\s*(\w)")
_WS = re.compile(r"\s+")


def clean_text(text: str) -> str:
    """Collapse whitespace, rejoin hyphenation, trim edge bullet glyphs."""
    if not text:
        return ""
    # Rejoin words split across a line break by hyphenation, before we
    # flatten newlines to spaces.
    text = _HYPHEN_LINEBREAK.sub(r"\1\2", text)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = _WS.sub(" ", text).strip()
    text = _EDGE_BULLETS.sub("", text)
    return text.strip()


# --------------------------------------------------------------------------
# Skill canonicalization
#
# Resumes spell the same skill many ways ("JS" / "Javascript" / "java script",
# "python 3" / "Python", "nodejs" / "node.js"). Left alone these become
# separate `skills.raw` entries and separate rows in the DB, which wrecks any
# downstream deduplication, matching, or gap analysis. We canonicalize a
# curated set of common variants and otherwise pass the skill through with its
# original casing, so unknown/proper-cased skills (and compound names like
# "C++", "CI/CD", "TCP/IP") are never mangled.
# --------------------------------------------------------------------------

# Separators that vary between writings of the same skill are dropped when
# building the lookup key; "+", "#" and "/" are KEPT so "C++", "C#", "CI/CD"
# and "TCP/IP" stay distinct.
_SKILL_KEY_STRIP = re.compile(r"[ \t._\-]+")

# Keyed by canonical_skill_key(variant) -> preferred display form.
_SKILL_ALIASES = {
    "python": "Python", "python3": "Python", "py": "Python",
    "javascript": "JavaScript", "js": "JavaScript",
    "typescript": "TypeScript", "ts": "TypeScript",
    "nodejs": "Node.js", "node": "Node.js",
    "react": "React", "reactjs": "React",
    "angular": "Angular", "angularjs": "Angular",
    "vue": "Vue.js", "vuejs": "Vue.js",
    "postgresql": "PostgreSQL", "postgres": "PostgreSQL", "psql": "PostgreSQL",
    "mysql": "MySQL",
    "mongodb": "MongoDB", "mongo": "MongoDB",
    "kubernetes": "Kubernetes", "k8s": "Kubernetes",
    "docker": "Docker",
    "aws": "AWS", "amazonwebservices": "AWS",
    "gcp": "GCP", "googlecloud": "GCP", "googlecloudplatform": "GCP",
    "azure": "Azure", "microsoftazure": "Azure",
    "go": "Go", "golang": "Go",
    "c++": "C++", "cplusplus": "C++",
    "c#": "C#", "csharp": "C#",
    ".net": ".NET", "net": ".NET", "dotnet": ".NET",
    "html": "HTML", "html5": "HTML",
    "css": "CSS", "css3": "CSS",
    "sql": "SQL",
    "rest": "REST", "restapi": "REST", "restful": "REST",
    "ci/cd": "CI/CD", "cicd": "CI/CD",
    "linux": "Linux",
    "git": "Git",
    "bash": "Bash", "shell": "Bash",
    "sklearn": "scikit-learn", "scikitlearn": "scikit-learn",
    "tensorflow": "TensorFlow",
    "pytorch": "PyTorch",
    "powerbi": "Power BI",
    "tableau": "Tableau",
    "excel": "Excel", "microsoftexcel": "Excel", "msexcel": "Excel",
}


def canonical_skill_key(name: str) -> str:
    """Casefold and drop spacing/dot/hyphen variation for skill matching.

    "Node.js", "node js" and "nodejs" all collapse to ``"nodejs"``; "+", "#"
    and "/" are preserved so distinct compound skills don't merge.
    """
    return _SKILL_KEY_STRIP.sub("", name.strip().lower())


def normalize_skill(name: str) -> str:
    """Canonicalize one skill name; return "" if nothing usable remains.

    Known variants map to a preferred display form; anything else is returned
    cleaned (whitespace collapsed, trailing punctuation trimmed) with its
    original casing intact.
    """
    name = clean_text(name).rstrip(" .,;:")
    if not name:
        return ""
    return _SKILL_ALIASES.get(canonical_skill_key(name), name)


def strip_leading_bullet(text: str) -> tuple[str, bool]:
    """Return (text_without_leading_bullet, was_bulleted).

    Unicode bullet glyphs are stripped whether or not a space follows; the
    ASCII "-"/"*" forms require a trailing space so we don't mangle
    hyphenated words ("e-commerce") or expressions ("-5C").
    """
    if not text:
        return text, False
    m = _LEADING_BULLET_UNICODE.match(text)
    if m:
        return text[m.end():].lstrip(), True
    m = _LEADING_BULLET_ASCII.match(text)
    if m:
        return text[m.end():].lstrip(), True
    return text, False


# --------------------------------------------------------------------------
# Date parsing
# --------------------------------------------------------------------------
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)"
)

# A single date token, longest forms first so the alternation prefers the
# most specific match.
_DATE_TOKEN = (
    rf"(?:{_MONTH}\.?\s+\d{{4}}"      # Jan 2020 / January 2020
    r"|\d{1,2}/\d{1,2}/\d{4}"          # 01/15/2020
    r"|\d{1,2}/\d{4}"                  # 01/2020
    r"|\d{4})"                         # 2020
)

_PRESENT = r"(?:Present|Current|Now|Ongoing|Till\s+Date|To\s+Date|Date)"

_RANGE_SEP = r"(?:\s*(?:–|—|-|\bto\b|\buntil\b|\bthrough\b|\bthru\b)\s*)"

# Public so the field parsers can reuse it (blueprint §8 is_entry_start).
DATE_RANGE_PATTERN = re.compile(
    rf"(?P<start>{_DATE_TOKEN})(?:{_RANGE_SEP}(?P<end>{_DATE_TOKEN}|{_PRESENT}))?",
    re.IGNORECASE,
)

_SINGLE_DATE_PATTERN = re.compile(_DATE_TOKEN, re.IGNORECASE)
_PRESENT_FULL = re.compile(_PRESENT, re.IGNORECASE)


def normalize_date_token(token: str) -> Optional[str]:
    """Normalize one date token to ``"YYYY-MM"`` or ``"YYYY"``; None if unparseable."""
    if not token:
        return None
    token = token.strip().rstrip(".")

    if re.fullmatch(r"\d{4}", token):
        return token

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", token)
    if m:
        month, year = int(m.group(1)), m.group(3)
        return f"{year}-{month:02d}" if 1 <= month <= 12 else year

    m = re.fullmatch(r"(\d{1,2})/(\d{4})", token)
    if m:
        month, year = int(m.group(1)), m.group(2)
        return f"{year}-{month:02d}" if 1 <= month <= 12 else year

    m = re.fullmatch(rf"({_MONTH})\.?\s+(\d{{4}})", token, re.IGNORECASE)
    if m:
        try:
            dt = _dateutil_parser.parse(
                token.replace(".", ""), default=datetime(2000, 1, 1)
            )
            return f"{dt.year:04d}-{dt.month:02d}"
        except (ValueError, OverflowError):
            return m.group(2)  # fall back to the year component

    return None


def parse_date_range(text: str) -> tuple[DateRange, str]:
    """Find the first date range in *text*.

    Returns ``(DateRange, remainder)`` where *remainder* is *text* with the
    matched date span removed (so header parsing can work on what's left).
    A lone date becomes ``start_date`` only. "Present"/"Current"/etc. set
    ``is_current=True`` and leave ``end_date=None``.
    """
    if not text:
        return DateRange(), text
    m = DATE_RANGE_PATTERN.search(text)
    if not m:
        return DateRange(), text

    start = normalize_date_token(m.group("start"))
    end_raw = m.group("end")
    end: Optional[str] = None
    is_current = False
    if end_raw:
        if _PRESENT_FULL.fullmatch(end_raw):
            is_current = True
        else:
            end = normalize_date_token(end_raw)

    remainder = (text[: m.start()] + " " + text[m.end():]).strip()
    remainder = _WS.sub(" ", remainder)
    return DateRange(start_date=start, end_date=end, is_current=is_current), remainder


def parse_single_date(text: str) -> tuple[Optional[str], str]:
    """Find the first single date in *text*; return ``(normalized, remainder)``."""
    if not text:
        return None, text
    m = _SINGLE_DATE_PATTERN.search(text)
    if not m:
        return None, text
    value = normalize_date_token(m.group(0))
    remainder = (text[: m.start()] + " " + text[m.end():]).strip()
    remainder = _WS.sub(" ", remainder)
    return value, remainder
