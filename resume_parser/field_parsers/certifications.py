"""
Certifications parsing (blueprint §10).

Typically one entry per line/block: ``Name — Issuer (Date)`` or
``Name, Issuer, Date``. We pull a single ``date_earned`` (not a range),
look for an explicit expiration phrase ("Expires", "Valid until", "Exp."),
and split the remainder into name / issuer. ``name`` is required by the
schema, so it always falls back to the original line.
"""

from __future__ import annotations

import re

from ..normalizer import clean_text, parse_single_date
from ..schema import Certification
from .common import split_segments

_EXPIRE_RE = re.compile(
    r"(?:Expires?|Valid\s+until|Valid\s+through|Exp\.?|Expiration|Expiry)\b[:\s]*",
    re.IGNORECASE,
)


def parse_certifications(blocks, warnings: list[str]) -> list[Certification]:
    certs: list[Certification] = []
    for b in blocks:
        line = clean_text(b.text)
        if not line:
            continue

        expiration = None
        em = _EXPIRE_RE.search(line)
        if em:
            exp_date, _ = parse_single_date(line[em.end():])
            expiration = exp_date
            line = line[: em.start()].strip(" ,;(–—-|")

        date_earned, remainder = parse_single_date(line)
        segments = split_segments(remainder)

        name = segments[0] if segments else (remainder.strip() or line.strip())
        issuer = segments[1] if len(segments) > 1 else None
        if not name:
            name = line.strip() or "Unnamed certification"

        certs.append(
            Certification(
                name=name,
                issuer=issuer,
                date_earned=date_earned,
                expiration_date=expiration,
            )
        )
    return certs
