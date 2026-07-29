"""
Tests for job_scraper.scraper — depth presets and the concurrent query loop.

JobSpy itself is faked via sys.modules so no network (or the real dependency)
is needed: ``scrape_jobs_for_queries`` imports it lazily by name.
"""

from __future__ import annotations

import sys
import threading
import types

from job_scraper.scraper import (
    DEFAULT_DEPTH,
    DEPTH_PRESETS,
    HOURS_OLD,
    RESULTS_PER_QUERY,
    resolve_depth,
    scrape_jobs_for_queries,
)


# ---------------------------------------------------------------------------
# Depth presets
# ---------------------------------------------------------------------------
def test_depth_presets_shape():
    assert set(DEPTH_PRESETS) == {"quick", "standard", "deep"}
    for preset in DEPTH_PRESETS.values():
        assert preset["results_wanted"] > 0
        assert preset["hours_old"] > 0
    # Quick mirrors the env-tunable legacy defaults.
    assert DEPTH_PRESETS["quick"]["results_wanted"] == RESULTS_PER_QUERY
    assert DEPTH_PRESETS["quick"]["hours_old"] == HOURS_OLD
    # Deeper presets strictly widen the net.
    assert (DEPTH_PRESETS["deep"]["results_wanted"]
            > DEPTH_PRESETS["standard"]["results_wanted"]
            > DEPTH_PRESETS["quick"]["results_wanted"])


def test_resolve_depth_known_and_fallback():
    assert resolve_depth("deep") is DEPTH_PRESETS["deep"]
    assert resolve_depth("Quick") is DEPTH_PRESETS["quick"]  # case-insensitive
    assert resolve_depth(None) is DEPTH_PRESETS[DEFAULT_DEPTH]
    assert resolve_depth("bogus") is DEPTH_PRESETS[DEFAULT_DEPTH]
    assert resolve_depth("") is DEPTH_PRESETS[DEFAULT_DEPTH]


# ---------------------------------------------------------------------------
# Concurrent query scraping (fake jobspy module)
# ---------------------------------------------------------------------------
class FakeDF:
    """The tiny slice of a pandas DataFrame the scraper touches."""

    def __init__(self, records):
        self._records = records

    def __len__(self):
        return len(self._records)

    def to_dict(self, orient):
        assert orient == "records"
        return self._records


def _install_fake_jobspy(monkeypatch, scrape_fn):
    mod = types.ModuleType("jobspy")
    mod.scrape_jobs = scrape_fn
    monkeypatch.setitem(sys.modules, "jobspy", mod)


def _record(i, term):
    return {
        "site": "indeed",
        "id": f"job-{term}-{i}",
        "title": f"{term} role {i}",
        "company": f"Co {i}",
        "location": "Calgary, AB",
        "job_url": f"https://example.com/{term}/{i}",
    }


def test_queries_run_concurrently_and_results_keep_query_order(monkeypatch):
    """The slow first query must not serialise the run, and output order must
    follow query order regardless of completion order."""
    started = []
    release = threading.Event()

    def fake_scrape(**kw):
        term = kw["search_term"]
        started.append(term)
        if term == "slow":
            # Wait until the fast query has been scheduled too — proves both
            # were in flight at once (would deadlock a sequential loop).
            assert release.wait(timeout=5), "queries did not run concurrently"
        else:
            release.set()
        return FakeDF([_record(0, term)])

    _install_fake_jobspy(monkeypatch, fake_scrape)
    result = scrape_jobs_for_queries(
        [{"search_term": "slow"}, {"search_term": "fast"}], sites=["indeed"]
    )
    assert result["source"] == "jobspy"
    assert [j["search_term"] for j in result["jobs"]] == ["slow", "fast"]
    assert result["errors"] == []


def test_one_failing_query_does_not_sink_the_run(monkeypatch):
    def fake_scrape(**kw):
        if kw["search_term"] == "bad":
            raise RuntimeError("board said no")
        return FakeDF([_record(1, kw["search_term"])])

    _install_fake_jobspy(monkeypatch, fake_scrape)
    result = scrape_jobs_for_queries(
        [{"search_term": "bad"}, {"search_term": "good"}], sites=["indeed"]
    )
    assert result["source"] == "jobspy"
    assert [j["search_term"] for j in result["jobs"]] == ["good"]
    assert len(result["errors"]) == 1
    assert "bad" in result["errors"][0]
    assert "board said no" in result["errors"][0]


def test_depth_values_are_passed_to_jobspy(monkeypatch):
    seen = {}

    def fake_scrape(**kw):
        seen.update(kw)
        return FakeDF([_record(0, kw["search_term"])])

    _install_fake_jobspy(monkeypatch, fake_scrape)
    depth = resolve_depth("deep")
    scrape_jobs_for_queries(
        [{"search_term": "tech"}],
        results_wanted=depth["results_wanted"],
        hours_old=depth["hours_old"],
    )
    assert seen["results_wanted"] == depth["results_wanted"]
    assert seen["hours_old"] == depth["hours_old"]


def test_all_queries_failing_falls_back_to_sample(monkeypatch):
    def fake_scrape(**kw):
        raise RuntimeError("blocked")

    _install_fake_jobspy(monkeypatch, fake_scrape)
    result = scrape_jobs_for_queries([{"search_term": "tech"}])
    assert result["source"] == "sample"
    assert result["jobs"]  # placeholders present
    assert len(result["errors"]) == 1


def test_silently_empty_boards_are_named(monkeypatch):
    """A Cloudflare-blocked board doesn't raise — it just contributes zero
    rows — so the result must say which requested boards gave nothing."""
    def fake_scrape(**kw):
        return FakeDF([_record(0, kw["search_term"])])  # everything from indeed

    _install_fake_jobspy(monkeypatch, fake_scrape)
    result = scrape_jobs_for_queries(
        [{"search_term": "tech"}],
        sites=["indeed", "zip_recruiter", "glassdoor"],
    )
    assert result["source"] == "jobspy"
    note = next(e for e in result["errors"] if "returned no postings" in e)
    assert "zip_recruiter" in note and "glassdoor" in note
    assert "indeed" not in note.split(" returned")[0]


def test_no_silent_board_note_when_all_deliver(monkeypatch):
    def fake_scrape(**kw):
        return FakeDF([_record(0, kw["search_term"])])

    _install_fake_jobspy(monkeypatch, fake_scrape)
    result = scrape_jobs_for_queries([{"search_term": "tech"}], sites=["indeed"])
    assert not any("returned no postings" in e for e in result["errors"])


def test_proxies_env_passed_to_jobspy(monkeypatch):
    import job_scraper.scraper as scraper_mod
    seen = {}

    def fake_scrape(**kw):
        seen.update(kw)
        return FakeDF([_record(0, kw["search_term"])])

    _install_fake_jobspy(monkeypatch, fake_scrape)
    monkeypatch.setattr(scraper_mod, "PROXIES", ["user:pass@1.2.3.4:8080"])
    scrape_jobs_for_queries([{"search_term": "tech"}], sites=["indeed"])
    assert seen["proxies"] == ["user:pass@1.2.3.4:8080"]

    # Without proxies configured the kwarg is omitted entirely.
    monkeypatch.setattr(scraper_mod, "PROXIES", [])
    seen.clear()
    scrape_jobs_for_queries([{"search_term": "tech"}], sites=["indeed"])
    assert "proxies" not in seen


def test_default_sites_are_the_working_boards(monkeypatch):
    import importlib
    monkeypatch.delenv("JOB_SITES", raising=False)
    import job_scraper.scraper as scraper_mod
    reloaded = importlib.reload(scraper_mod)
    try:
        assert reloaded.DEFAULT_SITES == ["indeed", "linkedin"]
    finally:
        importlib.reload(scraper_mod)
