"""
Turn a parsed resume into job-board search queries.

The parser output (``ParsedResume.model_dump(mode="json")``) is the only input.
We pull the most useful, low-noise signals for a job search:

* the person's job titles (current role first, then other recent roles), and
* a fallback query built from their top skills,

each paired with the resume's location. Titles make far better search terms than
skill soup, so they come first; the skills query is a safety net for resumes
with no usable experience titles (students, career changers).

This is intentionally simple and deterministic. When the career-goals
conversation is built later, the goal's *target* titles can be appended to the
list returned here with no other changes to the scraper.
"""

from __future__ import annotations

from typing import Any, Optional

# Cap how many distinct queries we run per resume. Each query is a separate
# scrape (network round-trip + rate-limit exposure), so a small number keeps the
# demo fast and polite to the job boards.
MAX_QUERIES = 3
MAX_SKILLS_IN_QUERY = 3


def _clean(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


def _looks_like_skill(value: str) -> bool:
    """Filter out sentence-like noise the parser sometimes lands in skills.

    A real skill is short ("Python", "Active Directory", "AWS Lambda"); a
    misparsed bullet ("Analyzed datasets with SQL.") is long and prose-y and
    makes a terrible search term. Keep it tight: a few words, no trailing period.
    """
    if not value or len(value) > 40 or value.endswith("."):
        return False
    return len(value.split()) <= 4


def build_queries_from_resume(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Build an ordered, de-duplicated list of search queries from a resume.

    Each query is ``{"search_term": str, "location": str | None, "source": str}``
    where ``source`` records how the term was derived ("current_title",
    "past_title", or "skills") — handy for debugging and for the UI.
    """
    contact = parsed.get("contact_info") or {}
    location = _clean(contact.get("location"))

    queries: list[dict[str, Any]] = []
    seen_terms: set[str] = set()

    def add(term: Optional[str], source: str) -> None:
        term = _clean(term)
        if not term or len(queries) >= MAX_QUERIES:
            return
        key = term.lower()
        if key in seen_terms:
            return
        seen_terms.add(key)
        queries.append({"search_term": term, "location": location, "source": source})

    experience = parsed.get("experience") or []

    # 1) Current role(s) first — the strongest signal of what to search for.
    for exp in experience:
        dates = exp.get("dates") or {}
        if dates.get("is_current"):
            add(exp.get("title"), "current_title")

    # 2) Other recent titles, in resume order (usually newest first).
    for exp in experience:
        add(exp.get("title"), "past_title")

    # 3) Skills fallback — only really needed when there are no usable titles,
    #    but it's a cheap extra angle even when there are.
    skills = (parsed.get("skills") or {}).get("raw") or []
    top_skills = [
        s for s in (_clean(s) for s in skills) if s and _looks_like_skill(s)
    ][:MAX_SKILLS_IN_QUERY]
    if top_skills:
        add(" ".join(top_skills), "skills")

    return queries
