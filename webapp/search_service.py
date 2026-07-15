"""
Shared scrape execution — the pure core behind both the synchronous search
route and the background worker.

``run_scrape`` takes a resume + search params, runs the queries through JobSpy,
stores the resulting ``job_searches`` row and its postings, and writes the
results JSON. It has no Flask/request dependency, so the worker process can call
it directly. It raises on failure (no usable queries, or a DB write error) and
the caller decides how to surface that.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from job_scraper import (
    build_queries_from_resume,
    scrape_jobs_for_queries,
    write_results_json,
)
from job_scraper.output import build_payload
from job_scraper.scraper import COUNTRY_INDEED

from . import db

log = logging.getLogger("careernexus.search")


class NoQueriesError(ValueError):
    """Raised when no search terms can be derived from the resume + params."""


def build_queries(parsed: dict[str, Any], params: dict[str, Any]):
    return build_queries_from_resume(
        parsed,
        location_override=params.get("location"),
        extra_keywords=params.get("keywords") or [],
    )


def run_scrape(
    resume_id: int, parsed: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Run a full scrape and persist it; return a result bundle.

    Returns ``{"search_id", "source", "job_count", "jobs", "errors", "sites",
    "location", "search_terms"}``. Raises :class:`NoQueriesError` when the
    resume yields no search terms, and propagates DB errors from storing the
    run so callers can record/ënsurface them.
    """
    queries = build_queries(parsed, params)
    if not queries:
        raise NoQueriesError("No search terms could be derived from this resume.")

    result = scrape_jobs_for_queries(
        queries,
        sites=params.get("sites") or None,
        country_indeed=params.get("country") or COUNTRY_INDEED,
        remote_preference=params.get("work_type") or "any",
    )
    jobs = result["jobs"]
    search_terms = [q["search_term"] for q in queries]
    location = queries[0].get("location")

    search_id = db.save_job_search(
        resume_id, search_terms, location, result["sites"], jobs, result["source"]
    )

    try:
        payload = build_payload(
            jobs=jobs,
            queries=queries,
            source=result["source"],
            sites=result["sites"],
            resume_id=resume_id,
            search_id=search_id,
            settings=params,
        )
        write_results_json(search_id, payload)
    except Exception as exc:  # non-fatal: the DB copy is canonical
        log.warning("results JSON not written for search %s: %s", search_id, exc)

    return {
        "search_id": search_id,
        "source": result["source"],
        "job_count": len(jobs),
        "jobs": jobs,
        "errors": result.get("errors") or [],
        "sites": result["sites"],
        "location": location,
        "search_terms": search_terms,
    }
