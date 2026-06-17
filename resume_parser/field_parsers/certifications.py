"""
Certifications parsing (blueprint §10).

A certification is either a single line (``Name — Issuer (Date)`` /
``Name, Issuer, Date``) or — common in styled resumes — a bold name line
followed by a separate issuer/date line. We first group the section's blocks
into per-certification runs, then pull a single ``date_earned`` (not a range),
an explicit expiration phrase ("Expires", "Valid until", "Exp."), and split
the remainder into name / issuer. ``name`` is required by the schema, so it
always falls back to the original text.
"""

from __future__ import annotations

import re

from ..normalizer import DATE_RANGE_PATTERN, clean_text, parse_single_date
from ..schema import Certification
from .common import split_segments

_EXPIRE_RE = re.compile(
    r"(?:Expires?|Valid\s+until|Valid\s+through|Exp\.?|Expiration|Expiry)\b[:\s]*",
    re.IGNORECASE,
)

# Tokens that mark a line as a name's *detail* (issuer / date) rather than the
# start of a new certification.
_ISSUED_RE = re.compile(r"\b(?:Issued|Earned|Completed|Awarded|Granted)\b", re.I)


def _has_date(text: str) -> bool:
    return bool(DATE_RANGE_PATTERN.search(text))


def _group_blocks(blocks) -> list[list[str]]:
    """Group blocks into per-certification line runs.

    A non-bold line continues the previous certification when that cert has no
    date yet and this line supplies one (or an issuer marker / pipe-separated
    detail). Otherwise every line starts a new certification — which keeps the
    common "one cert per line" layout working.
    """
    groups: list[list[str]] = []
    group_has_date: list[bool] = []
    for b in blocks:
        line = clean_text(b.text)
        if not line:
            continue
        this_has_date = _has_date(line)
        is_detail = (
            this_has_date
            or bool(_EXPIRE_RE.search(line))
            or bool(_ISSUED_RE.search(line))
            or "|" in line
        )
        if (
            groups
            and not getattr(b, "is_bold", False)
            and not group_has_date[-1]
            and is_detail
        ):
            groups[-1].append(line)
            group_has_date[-1] = group_has_date[-1] or this_has_date
        else:
            groups.append([line])
            group_has_date.append(this_has_date)
    return groups


def parse_certifications(blocks, warnings: list[str]) -> list[Certification]:
    certs: list[Certification] = []
    for lines in _group_blocks(blocks):
        name_line = lines[0]
        detail = " ".join(lines[1:])

        # Expiration can appear on either line.
        full = " ".join(lines)
        expiration = None
        em = _EXPIRE_RE.search(full)
        if em:
            expiration, _ = parse_single_date(full[em.end():])

        if detail:
            # Multi-line: the first line is the name as written; the rest is
            # issuer + date. Don't split the name on its internal dash/comma.
            name = name_line.strip(" ,;–—-|") or name_line
            text = detail
            dm = _EXPIRE_RE.search(text)
            if dm:
                text = text[: dm.start()]
            date_earned, remainder = parse_single_date(text)
            segments = split_segments(remainder)
            issuer = segments[0] if segments else (remainder.strip() or None)
        else:
            # Single line: split into name / issuer the old way.
            line = name_line
            dm = _EXPIRE_RE.search(line)
            if dm:
                line = line[: dm.start()].strip(" ,;(–—-|")
            date_earned, remainder = parse_single_date(line)
            segments = split_segments(remainder)
            name = segments[0] if segments else (remainder.strip() or line.strip())
            issuer = segments[1] if len(segments) > 1 else None

        if not name:
            name = name_line.strip() or "Unnamed certification"

        certs.append(
            Certification(
                name=name,
                issuer=issuer,
                date_earned=date_earned,
                expiration_date=expiration,
            )
        )
    return certs
