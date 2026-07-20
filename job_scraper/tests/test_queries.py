"""Tests for resume → search-term derivation, incl. the review/exclude path."""

from __future__ import annotations

from job_scraper.queries import build_queries_from_resume, resume_search_terms

RESUME = {
    "contact_info": {"location": "Calgary, AB"},
    "experience": [
        {"title": "Network Technician", "dates": {"is_current": True}},
        {"title": "Help Desk Analyst", "dates": {"is_current": False}},
    ],
    "skills": {"raw": ["Networking", "Cisco IOS", "Python"]},
}


def _terms(queries):
    return [q["search_term"] for q in queries]


def test_resume_search_terms_lists_inferred_terms():
    terms = resume_search_terms(RESUME)
    words = [t["term"] for t in terms]
    assert "Network Technician" in words
    assert "Help Desk Analyst" in words
    # Sources are labelled for the UI.
    assert {t["source"] for t in terms} <= {"current_title", "past_title", "skills"}
    # No user keyword or location leaks into the reviewable list.
    assert all("keyword" != t["source"] for t in terms)


def test_exclude_terms_drops_inferred_only():
    base = _terms(build_queries_from_resume(RESUME))
    assert "Help Desk Analyst" in base

    filtered = _terms(build_queries_from_resume(
        RESUME, exclude_terms=["help desk analyst"]))  # case-insensitive
    assert "Help Desk Analyst" not in filtered
    assert "Network Technician" in filtered


def test_excluded_term_still_kept_if_typed_as_keyword():
    # The user dropped it from the resume list but then typed it explicitly —
    # explicit intent wins.
    out = _terms(build_queries_from_resume(
        RESUME,
        extra_keywords=["Help Desk Analyst"],
        exclude_terms=["help desk analyst"],
    ))
    assert "Help Desk Analyst" in out


def test_exclude_terms_empty_is_noop():
    assert _terms(build_queries_from_resume(RESUME, exclude_terms=[])) == \
        _terms(build_queries_from_resume(RESUME))
