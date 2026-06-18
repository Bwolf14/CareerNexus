"""
Write a job-search result to a JSON file on disk.

The file pairs the resume-derived search context with the scraped postings, so a
later AI matching step can be fed *this* file alongside the resume JSON
(downloadable from the web app) without touching the database.

Output directory defaults to ``<repo>/job_results`` and is overridable with the
``JOB_RESULTS_DIR`` environment variable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

_DEFAULT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "job_results"
)


def results_dir() -> str:
    return os.environ.get("JOB_RESULTS_DIR", _DEFAULT_DIR)


def build_payload(
    *,
    jobs: list[dict[str, Any]],
    queries: list[dict[str, Any]],
    source: str,
    sites: list[str],
    resume_id: Optional[int] = None,
    search_id: Optional[int] = None,
    settings: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble the JSON-serialisable result bundle (also reused for downloads).

    ``settings`` captures the user's search options (keywords, location,
    work type, country) so the downstream AI matcher has the full context.
    """
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "resume_id": resume_id,
        "search_id": search_id,
        "source": source,
        "sites_searched": sites,
        "settings": settings or {},
        "search_terms": [q.get("search_term") for q in queries],
        "queries": queries,
        "job_count": len(jobs),
        "jobs": jobs,
    }


def write_results_json(identifier: Any, payload: dict[str, Any]) -> str:
    """Write ``payload`` to ``<results_dir>/jobs_<identifier>.json``; return path."""
    directory = results_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"jobs_{identifier}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
    return path
