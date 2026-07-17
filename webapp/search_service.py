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
    posting_dedup_key,
    resolve_depth,
    scrape_jobs_for_queries,
    write_results_json,
)
from job_scraper.output import build_payload
from job_scraper.scraper import COUNTRY_INDEED, _apply_remote_filter, _sample_jobs

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


def _blend_corpus(
    jobs: list[dict[str, Any]],
    search_terms: list[str],
    location: Optional[str],
    hours_old: int,
    remote_preference: str,
) -> int:
    """Append job-library postings that match the search and aren't already in
    ``jobs`` (mutated in place). Returns how many were added. Best-effort —
    a library problem never breaks the live search."""
    try:
        rows = db.search_corpus(
            search_terms, location, max_age_days=max(30, hours_old // 24)
        )
    except Exception as exc:
        log.warning("job library lookup failed: %s", exc)
        return 0

    rows = _apply_remote_filter(rows, remote_preference)
    seen = {
        posting_dedup_key(j.get("title"), j.get("company"), j.get("location"))
        for j in jobs
    }
    added = 0
    for row in rows:
        key = posting_dedup_key(row.get("title"), row.get("company"), row.get("location"))
        if key in seen:
            continue
        seen.add(key)
        job = dict(row)
        job.pop("search_id", None)
        if hasattr(job.get("date_posted"), "isoformat"):
            job["date_posted"] = job["date_posted"].isoformat()[:10]
        for k in ("salary_min", "salary_max"):
            if job.get(k) is not None:
                job[k] = float(job[k])  # DECIMAL → float, JSON-safe
        job["is_remote"] = bool(job.get("is_remote"))
        job["from_library"] = True
        jobs.append(job)
        added += 1
    return added


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

    depth = resolve_depth(params.get("depth"))
    remote_preference = params.get("work_type") or "any"
    result = scrape_jobs_for_queries(
        queries,
        sites=params.get("sites") or None,
        results_wanted=depth["results_wanted"],
        hours_old=depth["hours_old"],
        country_indeed=params.get("country") or COUNTRY_INDEED,
        remote_preference=remote_preference,
        allow_sample_fallback=False,
    )
    jobs = result["jobs"]
    search_terms = [q["search_term"] for q in queries]
    location = queries[0].get("location")

    # Blend in the internal job library: postings earlier scrapes already
    # collected that match this search but weren't in the live results. More
    # matches per search without extra calls to the external boards.
    live_count = len(jobs)
    if params.get("use_corpus", True):
        added = _blend_corpus(
            jobs, search_terms, location, depth["hours_old"], remote_preference
        )
        if added:
            result["errors"].append(
                f"Included {added} matching posting(s) from the Career Nexus "
                "job library (collected by earlier searches)."
            )

    # The scraper's own sample fallback is disabled above so the library gets
    # a chance to fill the gap first; only if there's still nothing do we show
    # the clearly-labelled offline placeholders.
    if jobs:
        result["source"] = "jobspy" if live_count else "library"
    else:
        result["errors"].append(
            "No live or library postings were found; showing sample data."
        )
        jobs = _apply_remote_filter(_sample_jobs(queries), remote_preference)
        result["source"] = "sample"

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
