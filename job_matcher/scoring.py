"""
Heuristic job-fit scoring: rank scraped postings against a parsed resume.

This is the deterministic stand-in for the future AI matching step. Each
posting gets a transparent 0–100 score built from:

* **skill overlap** — how many of the resume's skills appear in the posting,
* **title alignment** — token overlap between the resume's job titles and the
  posting title,
* **preferences** — the follow-up answers (work style, pay range, skills the
  user *wants* to use) boost or penalise postings,
* **recency** — a small bonus for fresh postings.

Alongside the score, every pick carries human-readable ``reasons`` (why it was
selected) and ``concerns`` (known mismatches) so the recommendations page can
explain itself — exactly the slots the AI's richer reasoning will fill later.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Optional

# Words that carry no signal when comparing job titles.
_TITLE_STOPWORDS = {
    "a", "an", "and", "assistant", "associate", "chief", "co", "for", "head",
    "i", "ii", "iii", "in", "intern", "junior", "jr", "lead", "level", "of",
    "on", "principal", "senior", "sr", "staff", "the", "to", "trainee", "vp",
    "with",
}

# Roughly 40 h/week * 52 weeks: converts hourly rates to yearly for comparison.
_HOURS_PER_YEAR = 2080

DEFAULT_TOP_N = 10
MIN_TOP_N = 5


def _tokens(text: Optional[str]) -> set[str]:
    if not text:
        return set()
    return {
        t for t in re.findall(r"[a-z0-9+#./]+", text.lower())
        if t not in _TITLE_STOPWORDS and len(t) > 1
    }


def _skill_pattern(skill: str) -> re.Pattern[str]:
    # Non-alphanumeric boundaries so "C++", "C#", and ".NET" match cleanly.
    return re.compile(r"(?<![a-z0-9])" + re.escape(skill.lower()) + r"(?![a-z0-9])")


def _matched_skills(skills: list[str], job_text: str) -> list[str]:
    out: list[str] = []
    for skill in skills:
        s = skill.strip()
        if s and _skill_pattern(s).search(job_text):
            out.append(s)
    return out


def _yearly(amount: Optional[float], interval: Optional[str]) -> Optional[float]:
    """Normalise a salary figure to a yearly amount for comparisons."""
    if amount is None:
        return None
    interval = (interval or "yearly").lower()
    if interval.startswith("hour"):
        return amount * _HOURS_PER_YEAR
    if interval.startswith("week"):
        return amount * 52
    if interval.startswith("month"):
        return amount * 12
    if interval.startswith("day") or interval.startswith("dai"):
        return amount * 5 * 52
    return amount


def _parse_user_range(answers: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    """Pull the user's stated pay range (as yearly figures) from the answers."""
    salary = answers.get("salary") or {}
    if not isinstance(salary, dict):
        return None, None

    def num(key: str) -> Optional[float]:
        raw = salary.get(key)
        if raw in (None, ""):
            return None
        try:
            return float(str(raw).replace(",", "").replace("$", ""))
        except ValueError:
            return None

    interval = salary.get("interval") or "yearly"
    return _yearly(num("min"), interval), _yearly(num("max"), interval)


def _is_remote(job: dict[str, Any]) -> bool:
    if job.get("is_remote"):
        return True
    text = f"{job.get('title') or ''} {job.get('location') or ''}".lower()
    return "remote" in text


def _recent(job: dict[str, Any], days: int = 7) -> bool:
    posted = job.get("date_posted")
    if not posted:
        return False
    try:
        posted_date = date.fromisoformat(str(posted)[:10])
    except ValueError:
        return False
    return posted_date >= date.today() - timedelta(days=days)


def _fmt_money(n: float) -> str:
    return f"${n:,.0f}"


def _score_one(
    job: dict[str, Any],
    *,
    resume_skills: list[str],
    resume_title_tokens: set[str],
    preferred_skills: list[str],
    work_style: Optional[str],
    user_min: Optional[float],
    user_max: Optional[float],
) -> dict[str, Any]:
    reasons: list[str] = []
    concerns: list[str] = []
    score = 0.0

    job_text = f"{job.get('title') or ''} {job.get('description') or ''}".lower()

    # --- Skill overlap: up to 45 points --------------------------------------
    matched = _matched_skills(resume_skills, job_text)
    if resume_skills:
        denom = min(len(resume_skills), 12)
        score += 45 * min(len(matched) / denom, 1.0)
    if matched:
        shown = ", ".join(matched[:5])
        more = f" (+{len(matched) - 5} more)" if len(matched) > 5 else ""
        reasons.append(
            f"Matches {len(matched)} of your skills: {shown}{more}"
        )

    # --- Skills the user *wants* to use: up to 15 points ----------------------
    wanted = _matched_skills(preferred_skills, job_text)
    if wanted:
        score += min(5 * len(wanted), 15)
        reasons.append(
            "Uses skills you said you want to work with: " + ", ".join(wanted[:4])
        )

    # --- Title alignment: up to 20 points -------------------------------------
    job_title_tokens = _tokens(job.get("title"))
    overlap = resume_title_tokens & job_title_tokens
    if job_title_tokens and resume_title_tokens:
        ratio = len(overlap) / len(job_title_tokens)
        score += 20 * min(ratio, 1.0)
        if ratio >= 0.5:
            reasons.append("Job title closely matches your experience")

    # --- Work-style preference: ±10 points ------------------------------------
    if work_style in ("Remote", "On-site"):
        remote = _is_remote(job)
        if work_style == "Remote" and remote:
            score += 10
            reasons.append("Remote role — matches your work-style preference")
        elif work_style == "On-site" and not remote:
            score += 10
        elif work_style == "Remote" and not remote:
            score -= 10
            concerns.append("Doesn't look remote, and you preferred remote work")
        elif work_style == "On-site" and remote:
            score -= 5
            concerns.append("Remote role, and you preferred on-site work")
    elif work_style == "Hybrid" and "hybrid" in job_text:
        score += 8
        reasons.append("Mentions hybrid work — matches your preference")

    # --- Pay range: ±10 points -------------------------------------------------
    job_lo = _yearly(job.get("salary_min"), job.get("salary_interval"))
    job_hi = _yearly(job.get("salary_max"), job.get("salary_interval"))
    if (user_min or user_max) and (job_lo or job_hi):
        lo = job_lo or job_hi
        hi = job_hi or job_lo
        floor = user_min or 0
        ceiling = user_max or float("inf")
        if hi >= floor and lo <= ceiling:
            score += 10
            if job.get("salary_display"):
                reasons.append(
                    f"Advertised pay ({job['salary_display']}) fits your target range"
                )
        elif hi < floor:
            score -= 10
            concerns.append(
                f"Advertised pay tops out around {_fmt_money(hi)}/yr — "
                "below the range you gave"
            )

    # --- Freshness: up to 5 points ---------------------------------------------
    if _recent(job):
        score += 5
        reasons.append("Posted within the last week")

    return {
        "job": job,
        "score": max(0, min(100, round(score))),
        "reasons": reasons,
        "concerns": concerns,
        "matched_skills": matched,
    }


def score_jobs(
    parsed: dict[str, Any],
    jobs: list[dict[str, Any]],
    answers: Optional[dict[str, Any]] = None,
    top_n: int = DEFAULT_TOP_N,
) -> list[dict[str, Any]]:
    """Score and rank ``jobs`` against the resume + questionnaire answers.

    Returns the top ``top_n`` (at least :data:`MIN_TOP_N` when available)
    scored entries, best first. Every entry keeps the full job dict plus
    ``score`` / ``reasons`` / ``concerns`` / ``matched_skills``.
    """
    answers = answers or {}
    resume_skills = [
        s for s in (parsed.get("skills") or {}).get("raw") or [] if s and len(s) <= 40
    ]
    title_tokens: set[str] = set()
    for exp in parsed.get("experience") or []:
        title_tokens |= _tokens(exp.get("title"))

    preferred = answers.get("preferred_skills") or []
    if isinstance(preferred, str):
        preferred = [preferred]
    work_style = answers.get("work_style")
    user_min, user_max = _parse_user_range(answers)

    scored = [
        _score_one(
            job,
            resume_skills=resume_skills,
            resume_title_tokens=title_tokens,
            preferred_skills=[str(p) for p in preferred],
            work_style=work_style,
            user_min=user_min,
            user_max=user_max,
        )
        for job in jobs
    ]
    scored.sort(key=lambda s: s["score"], reverse=True)

    top_n = max(top_n, MIN_TOP_N)
    return scored[:top_n]
