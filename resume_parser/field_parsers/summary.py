"""
Summary / objective parsing (blueprint §7.4, §10).

If an explicit Summary/Objective section was found, concatenate its prose
into one string. Otherwise fall back to the header zone: the first prose
block that isn't the name or a contact line becomes the summary.
"""

from __future__ import annotations

from typing import Optional

from ..normalizer import clean_text
from ..schema import ContactInfo
from .contact import EMAIL_RE, LOCATION_RE, PHONE_RE, URL_RE

_MIN_FALLBACK_WORDS = 8


def parse_summary(
    summary_blocks, header_zone, contact: ContactInfo, warnings: list[str]
) -> Optional[str]:
    if summary_blocks:
        text = " ".join(filter(None, (clean_text(b.text) for b in summary_blocks)))
        return text or None

    name = contact.name or ""
    for b in header_zone:
        t = clean_text(b.text)
        if not t or t == name:
            continue
        if EMAIL_RE.search(t) or PHONE_RE.search(t) or URL_RE.search(t):
            continue
        if LOCATION_RE.fullmatch(t):
            continue
        if not b.is_list_item and len(t.split()) >= _MIN_FALLBACK_WORDS:
            return t
    return None
