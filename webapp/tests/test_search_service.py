"""
Tests for webapp/search_service.py — depth resolution and job-library blending.

The scraper and DB are stubbed with monkeypatch; nothing touches the network or
a real database.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from webapp import search_service

PARSED = {"experience": [{"title": "Network Technician"}]}

LIVE_JOB = {
    "source_site": "indeed",
    "external_id": "live-1",
    "title": "Network Technician",
    "company": "Northwind",
    "location": "Calgary, AB",
    "job_type": "fulltime",
    "is_remote": False,
    "salary_min": 70000.0,
    "salary_max": 90000.0,
    "salary_currency": "CAD",
    "salary_interval": "yearly",
    "salary_display": None,
    "description": "Networking role.",
    "job_url": "https://example.com/live-1",
    "date_posted": "2026-07-10",
    "search_term": "Network Technician",
}

CORPUS_ROW = {
    "source_site": "glassdoor",
    "external_id": "corp-1",
    "title": "Network Analyst",
    "company": "Acme",
    "location": "Calgary, AB",
    "job_type": "fulltime",
    "is_remote": 0,
    "salary_min": Decimal("65000.00"),
    "salary_max": None,
    "salary_currency": "CAD",
    "salary_interval": "yearly",
    "description": "Analyst role.",
    "job_url": "https://example.com/corp-1",
    "search_term": "Network Technician",
    "date_posted": date(2026, 6, 20),
    "search_id": 3,
}


@pytest.fixture()
def stubs(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        search_service, "build_queries",
        lambda parsed, params: [{"search_term": "Network Technician",
                                 "location": params.get("location")}],
    )
    saved = {}

    def fake_save(resume_id, terms, location, sites, jobs, source):
        saved.update({"jobs": list(jobs), "source": source})
        return 7

    monkeypatch.setattr(search_service.db, "save_job_search", fake_save)
    return saved


def _scrape_stub(monkeypatch, jobs, capture=None):
    def fake(queries, **kw):
        if capture is not None:
            capture.update(kw)
        return {"jobs": list(jobs), "source": "jobspy",
                "sites": kw.get("sites") or ["indeed"], "errors": []}
    monkeypatch.setattr(search_service, "scrape_jobs_for_queries", fake)


def test_depth_preset_reaches_the_scraper(monkeypatch, stubs):
    seen = {}
    _scrape_stub(monkeypatch, [LIVE_JOB], capture=seen)
    monkeypatch.setattr(search_service.db, "search_corpus", lambda *a, **kw: [])
    search_service.run_scrape(3, PARSED, {"depth": "deep"})
    assert seen["results_wanted"] == 100
    assert seen["hours_old"] == 720
    assert seen["allow_sample_fallback"] is False


def test_corpus_blend_adds_new_postings(monkeypatch, stubs):
    _scrape_stub(monkeypatch, [LIVE_JOB])
    monkeypatch.setattr(search_service.db, "search_corpus",
                        lambda *a, **kw: [dict(CORPUS_ROW)])
    res = search_service.run_scrape(3, PARSED, {})
    assert res["source"] == "jobspy"
    assert res["job_count"] == 2
    added = [j for j in res["jobs"] if j.get("from_library")]
    assert len(added) == 1
    # DB types are normalised for downstream JSON/scoring use.
    assert added[0]["salary_min"] == 65000.0 and isinstance(added[0]["salary_min"], float)
    assert added[0]["date_posted"] == "2026-06-20"
    assert added[0]["is_remote"] is False
    assert "search_id" not in added[0]
    assert any("job library" in e for e in res["errors"])
    assert stubs["jobs"][-1]["from_library"] is True  # persisted with the search


def test_corpus_duplicate_of_live_posting_is_skipped(monkeypatch, stubs):
    dupe = dict(CORPUS_ROW, title=LIVE_JOB["title"], company=LIVE_JOB["company"],
                location=LIVE_JOB["location"])
    _scrape_stub(monkeypatch, [LIVE_JOB])
    monkeypatch.setattr(search_service.db, "search_corpus", lambda *a, **kw: [dupe])
    res = search_service.run_scrape(3, PARSED, {})
    assert res["job_count"] == 1
    assert not any(j.get("from_library") for j in res["jobs"])


def test_corpus_only_results_get_library_source(monkeypatch, stubs):
    _scrape_stub(monkeypatch, [])
    monkeypatch.setattr(search_service.db, "search_corpus",
                        lambda *a, **kw: [dict(CORPUS_ROW)])
    res = search_service.run_scrape(3, PARSED, {})
    assert res["source"] == "library"
    assert res["job_count"] == 1
    assert stubs["source"] == "library"


def test_use_corpus_false_skips_the_library(monkeypatch, stubs):
    _scrape_stub(monkeypatch, [LIVE_JOB])

    def boom(*a, **kw):
        raise AssertionError("library should not be queried")

    monkeypatch.setattr(search_service.db, "search_corpus", boom)
    res = search_service.run_scrape(3, PARSED, {"use_corpus": False})
    assert res["job_count"] == 1


def test_sample_fallback_when_nothing_found(monkeypatch, stubs):
    _scrape_stub(monkeypatch, [])
    monkeypatch.setattr(search_service.db, "search_corpus", lambda *a, **kw: [])
    res = search_service.run_scrape(3, PARSED, {})
    assert res["source"] == "sample"
    assert res["jobs"]
    assert any("sample" in e.lower() for e in res["errors"])


def test_library_failure_never_breaks_the_search(monkeypatch, stubs):
    _scrape_stub(monkeypatch, [LIVE_JOB])

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(search_service.db, "search_corpus", boom)
    res = search_service.run_scrape(3, PARSED, {})
    assert res["source"] == "jobspy"
    assert res["job_count"] == 1
