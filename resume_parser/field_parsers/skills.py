"""
Skills parsing (blueprint §10).

Handles the three common layouts:

* One long delimiter-separated block -> split into ``raw``.
* ``Category: item, item, item`` lines -> populate ``categorized[Category]``
  *and* flatten into ``raw``.
* One-skill-per-bullet -> each line is a ``raw`` entry.

Each skill is canonicalized (``normalize_skill``) so common variants — "JS" /
"Javascript", "python 3" / "Python", "nodejs" / "node.js" — collapse to one
form, then ``raw`` is de-duplicated case-insensitively while preserving
first-seen order.
"""

from __future__ import annotations

from ..normalizer import clean_text, normalize_skill
from ..schema import Skills
from .common import split_delim

_MAX_CATEGORY_WORDS = 4


def parse_skills(blocks, warnings: list[str]) -> Skills:
    skills = Skills()
    seen: set[str] = set()

    def add_raw(item: str) -> str:
        """Canonicalize and add to ``raw`` (deduped); return the canonical form."""
        item = normalize_skill(item)
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            skills.raw.append(item)
        return item

    for b in blocks:
        line = clean_text(b.text)
        if not line:
            continue

        # "Category: a, b, c" — only when the label is short (avoids treating
        # an ordinary sentence containing a colon as a category).
        if ":" in line:
            label, _, rest = line.partition(":")
            label = label.strip()
            items = split_delim(rest.strip())
            if label and items and len(label.split()) <= _MAX_CATEGORY_WORDS:
                bucket = skills.categorized.setdefault(label, [])
                for it in items:
                    canonical = add_raw(it)
                    if canonical and canonical not in bucket:
                        bucket.append(canonical)
                continue

        items = split_delim(line)
        if len(items) > 1:
            for it in items:
                add_raw(it)
        else:
            add_raw(line)

    return skills
