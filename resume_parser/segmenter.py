"""
Segmentation (blueprint §7) — group raw blocks into canonical sections.

Process, per column (columns are independent sequences):

1. Compute a body-text **baseline** font size (the most common size across
   blocks). Used to flag heading candidates by relative size.
2. Walk the column's blocks in reading order. A **heading candidate** is a
   block that is a DOCX heading/title style, OR meaningfully larger than
   baseline, OR bold + ALL-CAPS + short.
3. Exact-match the normalized candidate text against the section dictionary
   (``config/section_dictionary.yaml``). A match opens a canonical section
   that owns every following block until the next heading. A non-match,
   *once we're past the header zone*, opens an ``additional_sections``
   bucket keyed by the heading's own text.
4. The **header zone** is everything before the first dictionary-matched
   heading (across columns). The name / contact / fallback summary come
   from there. Keeping un-matched candidates (the name, a tagline) in the
   header zone — rather than turning them into spurious additional sections
   — is what makes name extraction work.

The result also carries a ``section_detection_status`` per the blueprint's
success / partial / failed definitions.
"""

from __future__ import annotations

import re
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from .intermediate_representation import ExtractedDocument, TextBlock
from .normalizer import clean_text

_CONFIG_PATH = Path(__file__).parent / "config" / "section_dictionary.yaml"

# Sections routed to dedicated field parsers rather than a list section.
_ROUTED = {"contact_info", "summary"}

_PUNCT = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")

# Heading heuristics.
#
# Real PDFs often render section headings only slightly larger than body
# text (e.g. 11.3pt vs 10.3pt) and don't expose a bold flag at all. So the
# size threshold is modest (~8%), and there's a separate ALL-CAPS rule that
# fires on a bold heading OR one that is merely larger than the body.
_HEADING_SIZE_RATIO = 1.08
_HEADING_MAX_WORDS_SIZE = 8   # size-based candidates must be short-ish
_HEADING_MAX_WORDS_CAPS = 5   # ALL-CAPS candidates


@dataclass
class SegmentationResult:
    sections: dict[str, list[TextBlock]]            # canonical -> blocks
    header_zone: list[TextBlock]                    # name / contact / fallback summary
    summary_blocks: list[TextBlock]                 # explicit Summary/Objective section
    additional_sections: "OrderedDict[str, list[TextBlock]]"  # heading text -> blocks
    status: str                                     # success | partial | failed
    baseline: Optional[float]


def normalize_heading(text: str) -> str:
    """Lowercase, strip punctuation/whitespace — the dictionary match key."""
    t = (text or "").lower().strip()
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


@lru_cache(maxsize=1)
def _reverse_map() -> dict[str, str]:
    """normalized synonym -> canonical section name (built once)."""
    data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    rev: dict[str, str] = {}
    for canonical, synonyms in data.items():
        for syn in synonyms or []:
            rev[normalize_heading(syn)] = canonical
        rev[normalize_heading(canonical)] = canonical
    return rev


def _compute_baseline(blocks: list[TextBlock]) -> Optional[float]:
    """Most common font size = body text. On a tie, prefer the smaller size,
    since body text is the smaller, more frequent size and headings are the
    larger, rarer one."""
    sizes = [b.font_size for b in blocks if b.font_size is not None]
    if not sizes:
        return None
    counts = Counter(round(s, 1) for s in sizes)
    max_count = max(counts.values())
    return min(size for size, c in counts.items() if c == max_count)


def _is_heading_candidate(b: TextBlock, baseline: Optional[float]) -> bool:
    if b.is_list_item:
        return False
    text = b.text.strip()
    if not text:
        return False

    style = (b.paragraph_style or "").lower()
    if style.startswith("heading") or style in ("title", "subtitle") or style.startswith("subtitle"):
        return True

    if baseline and b.font_size and b.font_size >= baseline * _HEADING_SIZE_RATIO:
        if len(text.split()) <= _HEADING_MAX_WORDS_SIZE:
            return True

    # ALL-CAPS heading: may be unbolded and only marginally larger than body.
    # Require either an explicit bold flag OR a size strictly above baseline,
    # so an all-caps *body* line (e.g. "AWS, SQL") at body size isn't flagged.
    if (
        text.isupper()
        and any(c.isalpha() for c in text)
        and 1 <= len(text.split()) <= _HEADING_MAX_WORDS_CAPS
    ):
        if b.is_bold or (baseline and b.font_size and b.font_size > baseline):
            return True

    return False


def _status(matched: set[str], any_candidate: bool) -> str:
    if not any_candidate:
        return "failed"
    has_core = bool(matched & {"experience", "education"})
    if has_core and len(matched) >= 2:
        return "success"
    return "partial"


def segment(doc: ExtractedDocument) -> SegmentationResult:
    baseline = _compute_baseline(doc.blocks)
    rev = _reverse_map()

    by_col: dict[int, list[TextBlock]] = defaultdict(list)
    for b in doc.blocks:
        by_col[b.column_index].append(b)
    for col in by_col:
        by_col[col].sort(key=lambda b: b.order_index)

    sections: dict[str, list[TextBlock]] = defaultdict(list)
    additional: "OrderedDict[str, list[TextBlock]]" = OrderedDict()
    header_zone: list[TextBlock] = []
    matched: set[str] = set()
    any_candidate = False

    for col in sorted(by_col):
        # current: None (header zone) | ("section", name) | ("additional", text)
        current: Optional[tuple[str, str]] = None
        first_matched = False
        for b in by_col[col]:
            if _is_heading_candidate(b, baseline):
                any_candidate = True
                canonical = rev.get(normalize_heading(b.text))
                if canonical:
                    matched.add(canonical)
                    first_matched = True
                    current = ("section", canonical)
                    continue
                # Unmatched candidate before the first real section heading
                # stays in the header zone (the name / a tagline).
                if not first_matched and current is None:
                    header_zone.append(b)
                    continue
                heading_text = clean_text(b.text)
                if not heading_text:
                    continue
                additional.setdefault(heading_text, [])
                current = ("additional", heading_text)
                continue

            # Non-heading block: assign to whatever we're inside.
            if current is None:
                header_zone.append(b)
            elif current[0] == "section":
                sections[current[1]].append(b)
            else:
                additional[current[1]].append(b)

    summary_blocks = sections.pop("summary", [])
    contact_blocks = sections.pop("contact_info", [])
    # An explicit Contact section feeds the same parser as the header zone.
    header_zone = header_zone + contact_blocks

    return SegmentationResult(
        sections=dict(sections),
        header_zone=header_zone,
        summary_blocks=summary_blocks,
        additional_sections=additional,
        status=_status(matched, any_candidate),
        baseline=baseline,
    )
