"""
Projects parsing (blueprint §10).

Project entries often have a bare title (no date/company pattern), so entry
boundaries are detected by "any block at indentation level 0 after a bullet
run" (``force_indent0``) rather than the bold/date rule used for experience.
Lines like ``Technologies:`` / ``Tech stack:`` / ``Built with:`` feed
``technologies``; a URL anywhere in the entry feeds ``url``; the rest of the
bullets become the (single-string) ``description``.
"""

from __future__ import annotations

import re

from ..normalizer import clean_text, parse_date_range
from ..schema import Project
from .common import group_entries, split_delim, split_header_description
from .contact import URL_RE

_TECH_RE = re.compile(
    r"^(?:Technologies|Tech\s*stack|Tech|Built\s+with|Stack|Tools|Made\s+with)\b"
    r"\s*[:\-–—]\s*(.+)$",
    re.IGNORECASE,
)


def parse_projects(blocks, baseline, warnings: list[str]) -> list[Project]:
    projects: list[Project] = []
    for entry in group_entries(blocks, baseline, force_indent0=True):
        header, description_lines = split_header_description(entry)
        header_text = " ".join(filter(None, (clean_text(b.text) for b in header)))

        dates, remainder = parse_date_range(header_text)

        url = None
        um = URL_RE.search(remainder)
        if um:
            url = um.group(0).rstrip(".,;)")
            remainder = URL_RE.sub(" ", remainder)

        # Title = the leading header text (first segment before any separator).
        title = re.split(r"\s*[|—–]\s*", remainder.strip(), maxsplit=1)[0].strip()

        technologies: list[str] = []
        desc_parts: list[str] = []
        for line in description_lines:
            tm = _TECH_RE.match(line)
            if tm:
                technologies.extend(split_delim(tm.group(1)))
                continue
            lm = URL_RE.search(line)
            if lm and url is None:
                url = lm.group(0).rstrip(".,;)")
            stripped = URL_RE.sub("", line).strip(" -–—|")
            if stripped:
                desc_parts.append(stripped)

        # A "Technologies:" line sometimes rides along in the header tail.
        for extra in re.split(r"\s*[|]\s*", remainder)[1:]:
            tm = _TECH_RE.match(extra.strip())
            if tm:
                technologies.extend(split_delim(tm.group(1)))

        if not title:
            title = "Untitled Project"
        description = " ".join(desc_parts) or None

        projects.append(
            Project(
                title=title,
                description=description,
                technologies=technologies,
                url=url,
                dates=dates,
            )
        )
    return projects
