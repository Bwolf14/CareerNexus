"""
Career Nexus job scraper.

A small, self-contained subsystem that turns a parsed resume into job-board
search queries, runs them through `JobSpy <https://github.com/speedyapply/JobSpy>`_,
and returns normalised job-posting dicts ready to store in the database or dump
to JSON for a downstream AI matching step.

It is deliberately kept separate from ``resume_parser``: the only thing the two
share is the parsed-resume dict, which is the input to
:func:`build_queries_from_resume`.

Typical use::

    from job_scraper import build_queries_from_resume, scrape_jobs_for_queries

    queries = build_queries_from_resume(parsed_resume)
    result = scrape_jobs_for_queries(queries)
    jobs = result["jobs"]          # list[dict], normalised to the jobs schema
"""

from __future__ import annotations

from .output import write_results_json
from .queries import build_queries_from_resume
from .scraper import (
    DEFAULT_SITES,
    dedupe_cross_board,
    posting_dedup_key,
    scrape_jobs_for_queries,
)

__all__ = [
    "build_queries_from_resume",
    "scrape_jobs_for_queries",
    "write_results_json",
    "dedupe_cross_board",
    "posting_dedup_key",
    "DEFAULT_SITES",
]
