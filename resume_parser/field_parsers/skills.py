"""
Skills parsing (blueprint §10).

Handles the common layouts:

* One long delimiter-separated block -> split into ``raw``.
* ``Category: item, item, item`` lines -> populate ``categorized[Category]``
  *and* flatten into ``raw``.
* A ``Category:`` label on its own line, with the items on the line(s) below
  (very common in two-column / styled resumes) -> same result.
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

    def add_to(bucket, item) -> None:
        canonical = add_raw(item)
        if bucket is not None and canonical and canonical not in bucket:
            bucket.append(canonical)

    def is_label(text: str) -> bool:
        return bool(text) and len(text.split()) <= _MAX_CATEGORY_WORDS

    # A "Category:" label on its own line applies to the line(s) that follow.
    pending_category: str | None = None

    for b in blocks:
        line = clean_text(b.text)
        if not line:
            continue

        # Label-only line: "Cloud & Infrastructure:" with nothing after the colon.
        if line.endswith(":"):
            label = line[:-1].strip()
            if is_label(label):
                pending_category = label
                skills.categorized.setdefault(label, [])
                continue

        # Inline "Category: a, b, c" — short label avoids treating an ordinary
        # sentence containing a colon as a category.
        if ":" in line:
            label, _, rest = line.partition(":")
            label = label.strip()
            items = split_delim(rest.strip())
            if items and is_label(label):
                bucket = skills.categorized.setdefault(label, [])
                for it in items:
                    add_to(bucket, it)
                pending_category = None
                continue

        items = split_delim(line)
        if pending_category is not None:
            bucket = skills.categorized.setdefault(pending_category, [])
            for it in items or [line]:
                add_to(bucket, it)
            continue
        if len(items) > 1:
            for it in items:
                add_raw(it)
        else:
            add_raw(line)

    # Drop any label that never received items.
    skills.categorized = {k: v for k, v in skills.categorized.items() if v}
    return skills
