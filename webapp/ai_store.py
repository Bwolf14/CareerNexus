"""
Per-search caches for AI-generated content.

Two kinds of artifacts are cached as JSON files in the job-results directory
(volume-mounted in Docker, same place as the plan-answer fallback files):

* ``ai_questions_<search_id>.json`` — the questionnaire actually shown for a
  search. Caching it matters for correctness, not just speed: the POST that
  collects answers must see the *same* question list (same ids) the GET
  rendered, and AI-generated questions aren't reproducible.
* ``ai_analysis_<search_id>.json`` — the career-plan analysis, keyed by a
  hash of the answers + shortlist so it regenerates when either changes.

Everything is best-effort: a missing/corrupt cache file just means the
content is regenerated (or falls back to templates).
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from job_scraper.output import results_dir

_KINDS = {"questions", "analysis"}


def _path(kind: str, search_id: int) -> str:
    assert kind in _KINDS, f"unknown ai_store kind: {kind}"
    return os.path.join(results_dir(), f"ai_{kind}_{search_id}.json")


def save(kind: str, search_id: int, data: dict[str, Any]) -> None:
    os.makedirs(results_dir(), exist_ok=True)
    with open(_path(kind, search_id), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def load(kind: str, search_id: int) -> Optional[dict[str, Any]]:
    try:
        with open(_path(kind, search_id), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def clear(kind: str, search_id: int) -> None:
    try:
        os.remove(_path(kind, search_id))
    except OSError:
        pass
