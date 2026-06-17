"""
Contact-info parsing (blueprint §10) — regex over the header-zone text.

Also the home for the shared contact/location regexes that experience,
education, volunteer, and project parsing reuse.

Everything here is low-to-medium confidence by nature (especially
location, which is pattern-based with no gazetteer). Missing email or
location is recorded as a warning rather than raised.
"""

from __future__ import annotations

import re
from typing import Optional

from ..intermediate_representation import TextBlock
from ..normalizer import clean_text
from ..schema import ContactInfo, Links

EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")

# (403) 555-0123 / 403-555-0123 / +1 403 555 0123 / 403.555.0123
PHONE_RE = re.compile(
    r"(?<![\d\w])(\+?\d{1,3}[\s.\-]?)?(\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}(?!\d)"
)

LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/(?:in|pub)/[A-Za-z0-9_%\-./]+", re.I
)
GITHUB_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9_\-./]+", re.I
)
URL_RE = re.compile(r"(?:https?://|www\.)[^\s,;)\]]+", re.I)

# Location: "City, ST" or "City, Province/State [, Country]". Two-letter
# abbreviations cover Canadian provinces + US states; a curated full-name
# list catches spelled-out forms.
_PROV_STATE_FULL = (
    r"(?:Alberta|British Columbia|Manitoba|New Brunswick|"
    r"Newfoundland(?: and Labrador)?|Nova Scotia|Ontario|"
    r"Prince Edward Island|Quebec|Québec|Saskatchewan|Yukon|"
    r"Northwest Territories|Nunavut|"
    r"Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|"
    r"Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|"
    r"Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|"
    r"Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|"
    r"New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|"
    r"Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|"
    r"Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming)"
)
# City words must be Title-Case (initial capital followed by a lowercase
# letter), so an adjacent ALL-CAPS heading/tagline (e.g. "INFORMATION
# TECHNOLOGY SERVICES") isn't pulled into the city name.
_CITY_WORD = r"[A-Z][a-z][A-Za-z.'\-]*"
LOCATION_RE = re.compile(
    rf"({_CITY_WORD}(?:[ \-]{_CITY_WORD}){{0,3}}),\s*"
    rf"({_PROV_STATE_FULL}|[A-Z]{{2}})\b"
)


def parse_contact(blocks: list[TextBlock], warnings: list[str]) -> ContactInfo:
    full = " ".join(filter(None, (clean_text(b.text) for b in blocks)))
    info = ContactInfo()

    m = EMAIL_RE.search(full)
    if m:
        info.email = m.group(0).rstrip(".")

    m = PHONE_RE.search(full)
    if m:
        info.phone = re.sub(r"\s+", " ", m.group(0)).strip()

    info.links = _parse_links(full)

    m = LOCATION_RE.search(full)
    if m:
        info.location = f"{m.group(1)}, {m.group(2)}"

    info.name = _detect_name(blocks)

    if not info.email:
        warnings.append("Contact: email address not found.")
    if not info.location:
        warnings.append("Contact: location not found (low-confidence field).")
    if not info.name:
        warnings.append("Contact: name not confidently identified.")
    return info


def _parse_links(full: str) -> Links:
    links = Links()
    lm = LINKEDIN_RE.search(full)
    if lm:
        links.linkedin = lm.group(0).rstrip(".,;")
    gm = GITHUB_RE.search(full)
    if gm:
        links.github = gm.group(0).rstrip(".,;")

    used = {x for x in (links.linkedin, links.github) if x}
    others: list[str] = []
    for um in URL_RE.finditer(full):
        url = um.group(0).rstrip(".,;)")
        low = url.lower()
        if "linkedin.com" in low or "github.com" in low:
            continue
        if url in used:
            continue
        used.add(url)
        if links.portfolio is None:
            links.portfolio = url
        else:
            others.append(url)
    links.other = others
    return links


def _detect_name(blocks: list[TextBlock]) -> Optional[str]:
    """Largest-font header line that isn't an email/phone/url/location."""
    candidates: list[TextBlock] = []
    for b in blocks:
        t = clean_text(b.text)
        if not t:
            continue
        if EMAIL_RE.search(t) or URL_RE.search(t) or PHONE_RE.search(t):
            continue
        if LOCATION_RE.fullmatch(t):
            continue
        if len(t.split()) > 6:
            continue
        alpha_ish = sum(c.isalpha() or c.isspace() for c in t)
        if alpha_ish < len(t) * 0.6:
            continue
        candidates.append(b)
    if not candidates:
        return None
    best = max(candidates, key=lambda b: (b.font_size or 0.0))
    return clean_text(best.text) or None
