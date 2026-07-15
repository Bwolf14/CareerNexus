"""
JobSpy wrapper: run search queries and return normalised job-posting dicts.

Responsibilities
----------------
* call ``jobspy.scrape_jobs`` for each query,
* normalise JobSpy's pandas rows into plain dicts matching the ``jobs`` table
  (NaN/NaT scrubbed to ``None``, salary/date coerced to clean types),
* de-duplicate within a run, and
* fall back to clearly-labelled **sample** postings if JobSpy is unavailable or
  every query comes back empty — so the demo UI always has something to show.

JobSpy is imported lazily inside :func:`scrape_jobs_for_queries` so the web app
still boots (and degrades to sample data) on a box where the dependency or the
network isn't available.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Any, Optional

# Boards to scrape. Indeed, ZipRecruiter, and Glassdoor all work without proxies
# and cover Canada/US well, so they're the default trio. LinkedIn needs rotating
# proxies and Google Jobs needs a separate query format, so neither is included
# by default — override with JOB_SITES (e.g. "indeed" alone for a faster demo).
DEFAULT_SITES = [
    s.strip()
    for s in os.environ.get("JOB_SITES", "indeed,zip_recruiter,glassdoor").split(",")
    if s.strip()
]

# Per-query knobs, env-overridable for tuning the demo.
RESULTS_PER_QUERY = int(os.environ.get("JOB_RESULTS_PER_QUERY", "15"))
HOURS_OLD = int(os.environ.get("JOB_HOURS_OLD", "168"))  # last 7 days

# Which country's Indeed/Glassdoor site to search. JobSpy defaults to the US
# site, so a "Calgary, Alberta" search on the US domain returns nothing — this
# project is Canada-first, hence the default. Override with JOB_COUNTRY.
COUNTRY_INDEED = os.environ.get("JOB_COUNTRY", "Canada")


def _is_missing(value: Any) -> bool:
    """True for None or a pandas NaN/NaT float (the common 'empty cell' cases)."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return False


def _clean(value: Any) -> Any:
    return None if _is_missing(value) else value


def _to_str(value: Any) -> Optional[str]:
    value = _clean(value)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_float(value: Any) -> Optional[float]:
    value = _clean(value)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_date_str(value: Any) -> Optional[str]:
    """Render a date/Timestamp as 'YYYY-MM-DD', or None if missing/unparseable."""
    value = _clean(value)
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()[:10]
        except Exception:
            return None
    return str(value)[:10] or None


def _salary_display(
    lo: Optional[float],
    hi: Optional[float],
    currency: Optional[str],
    interval: Optional[str],
) -> Optional[str]:
    if lo is None and hi is None:
        return None
    sym = {"USD": "$", "CAD": "$", "EUR": "€", "GBP": "£"}.get(
        (currency or "").upper(), ""
    )

    def fmt(n: float) -> str:
        return f"{sym}{n:,.0f}"

    if lo is not None and hi is not None and lo != hi:
        amount = f"{fmt(lo)}–{fmt(hi)}"
    else:
        amount = fmt(lo if lo is not None else hi)  # type: ignore[arg-type]
    return f"{amount} / {interval}" if interval else amount


def _external_id(record: dict[str, Any], job_url: Optional[str]) -> str:
    """Stable id for de-duplication: the board's id, else a hash of the URL."""
    ext = _to_str(record.get("id"))
    if ext:
        return ext
    basis = job_url or repr(sorted(record.items()))
    return "url-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _normalise(record: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    """Map one JobSpy row onto the jobs-table shape used everywhere downstream."""
    job_url = _to_str(record.get("job_url")) or _to_str(record.get("job_url_direct"))
    lo = _to_float(record.get("min_amount"))
    hi = _to_float(record.get("max_amount"))
    currency = _to_str(record.get("currency"))
    interval = _to_str(record.get("interval"))

    return {
        "source_site": _to_str(record.get("site")),
        "external_id": _external_id(record, job_url),
        "title": _to_str(record.get("title")),
        "company": _to_str(record.get("company")),
        "location": _to_str(record.get("location")),
        "job_type": _to_str(record.get("job_type")),
        "is_remote": bool(_clean(record.get("is_remote")) or False),
        "salary_min": lo,
        "salary_max": hi,
        "salary_currency": currency,
        "salary_interval": interval,
        "salary_display": _salary_display(lo, hi, currency, interval),
        "description": _to_str(record.get("description")),
        "job_url": job_url,
        "date_posted": _to_date_str(record.get("date_posted")),
        "search_term": query.get("search_term"),
    }


def _dedup(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Optional[str], str]] = set()
    out: list[dict[str, Any]] = []
    for job in jobs:
        key = (job.get("source_site"), job.get("external_id") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out


_DEDUP_PUNCT = re.compile(r"[^a-z0-9 ]+")
_DEDUP_WS = re.compile(r"\s+")


def _norm_for_key(value: Optional[str]) -> str:
    text = _DEDUP_PUNCT.sub(" ", (value or "").lower())
    return _DEDUP_WS.sub(" ", text).strip()


def posting_dedup_key(
    title: Optional[str], company: Optional[str], location: Optional[str]
) -> str:
    """Stable content hash for a posting, independent of which board it came from.

    Same title + company + city → same key, so a job cross-posted to Indeed and
    Glassdoor collapses to one entry. Location is reduced to its first token
    (usually the city) so "Calgary, AB" and "Calgary, Alberta" still match.
    """
    city = _norm_for_key(location).split(",")[0].split(" ")[0]
    basis = f"{_norm_for_key(title)}|{_norm_for_key(company)}|{city}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def dedupe_cross_board(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the same posting appearing on multiple boards into one row.

    Keeps the first occurrence (optionally upgrading to one that has a URL) and
    annotates it with ``also_on`` — the other boards it was found on — so the UI
    can show "also on Glassdoor" without listing the posting twice.
    """
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for job in jobs:
        key = posting_dedup_key(
            job.get("title"), job.get("company"), job.get("location")
        )
        if key not in by_key:
            job = dict(job)
            job["dedup_key"] = key
            job["also_on"] = []
            by_key[key] = job
            order.append(key)
            continue
        kept = by_key[key]
        site = job.get("source_site")
        if site and site != kept.get("source_site") and site not in kept["also_on"]:
            kept["also_on"].append(site)
        # Prefer a version that actually has a link if the kept one doesn't.
        if not kept.get("job_url") and job.get("job_url"):
            also = kept["also_on"]
            src = kept.get("source_site")
            job = dict(job)
            job["dedup_key"] = key
            job["also_on"] = also + ([src] if src and src not in also else [])
            by_key[key] = job
    return [by_key[k] for k in order]


def _sample_jobs(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic stand-in postings so the demo works offline / when blocked.

    Clearly labelled (source_site='sample') so it's never mistaken for live data.
    """
    term = (queries[0]["search_term"] if queries else "your field").strip()
    location = (queries[0].get("location") if queries else None) or "Remote"
    blueprint = [
        ("Senior {t}", "Northwind Technologies", 110000, 140000, False),
        ("{t}", "Acme Software", 85000, 110000, True),
        ("Junior {t}", "BluePeak Solutions", 65000, 85000, False),
        ("{t} (Contract)", "Helix Consulting", 70000, 95000, True),
        ("Lead {t}", "Summit Digital", 130000, 165000, False),
        ("Associate {t}", "Cedar & Co.", 60000, 78000, True),
    ]
    jobs: list[dict[str, Any]] = []
    for i, (title_tpl, company, lo, hi, remote) in enumerate(blueprint):
        title = title_tpl.format(t=term.title())
        jobs.append(
            {
                "source_site": "sample",
                "external_id": f"sample-{i}",
                "title": title,
                "company": company,
                "location": "Remote" if remote else location,
                "job_type": "contract" if "Contract" in title_tpl else "fulltime",
                "is_remote": remote,
                "salary_min": float(lo),
                "salary_max": float(hi),
                "salary_currency": "USD",
                "salary_interval": "yearly",
                "salary_display": _salary_display(float(lo), float(hi), "USD", "yearly"),
                "description": (
                    f"Sample posting for a {title} role. This placeholder is shown "
                    "because no live results were available (JobSpy not installed, "
                    "no network access, or the search returned nothing). Replace by "
                    "running against live job boards."
                ),
                "job_url": "https://example.com/sample-posting",
                "date_posted": None,
                "search_term": term,
            }
        )
    return jobs


def _is_job_remote(job: dict[str, Any]) -> bool:
    """Best-effort 'is this posting remote?' from the normalised job dict.

    JobSpy's own ``is_remote`` flag is only set ~30% of the time on Indeed (and
    its ``is_remote=True`` search arg is effectively a no-op there), so we also
    look for "remote" in the title/location text, which Indeed populates far
    more reliably (e.g. location "Remote, US").
    """
    if job.get("is_remote"):
        return True
    text = f"{job.get('title') or ''} {job.get('location') or ''}".lower()
    return "remote" in text


def _apply_remote_filter(
    jobs: list[dict[str, Any]], remote_preference: str
) -> list[dict[str, Any]]:
    """Filter postings by work-type preference using :func:`_is_job_remote`.

    'remote' keeps only postings that look remote; 'local' drops those; 'any'
    is a no-op. Done client-side because the boards don't filter reliably.
    """
    if remote_preference == "remote":
        return [j for j in jobs if _is_job_remote(j)]
    if remote_preference == "local":
        return [j for j in jobs if not _is_job_remote(j)]
    return jobs


def scrape_jobs_for_queries(
    queries: list[dict[str, Any]],
    *,
    sites: Optional[list[str]] = None,
    results_wanted: int = RESULTS_PER_QUERY,
    hours_old: int = HOURS_OLD,
    country_indeed: str = COUNTRY_INDEED,
    remote_preference: str = "any",
    allow_sample_fallback: bool = True,
) -> dict[str, Any]:
    """Run every query through JobSpy and return a normalised result bundle.

    Returns ``{"jobs": [...], "source": "jobspy"|"sample", "sites": [...],
    "errors": [...]}``. ``source`` is ``"sample"`` when the returned jobs are the
    offline placeholders rather than live postings. ``remote_preference`` is one
    of "any", "remote", or "local".
    """
    sites = sites or DEFAULT_SITES
    errors: list[str] = []
    collected: list[dict[str, Any]] = []
    got_live = False

    # Remote/local filtering happens client-side and discards a chunk of each
    # query's results, so pull a larger pool when a work-type filter is active.
    fetch_wanted = results_wanted * 2 if remote_preference != "any" else results_wanted

    try:
        from jobspy import scrape_jobs as _scrape  # heavy import, done lazily
    except Exception as exc:  # pragma: no cover - depends on environment
        _scrape = None
        errors.append(f"JobSpy unavailable: {exc}")

    if _scrape is not None:
        for query in queries:
            try:
                df = _scrape(
                    site_name=sites,
                    search_term=query["search_term"],
                    location=query.get("location") or None,
                    results_wanted=fetch_wanted,
                    hours_old=hours_old,
                    country_indeed=country_indeed,
                )
                got_live = True
                if df is not None and len(df):
                    for record in df.to_dict("records"):
                        collected.append(_normalise(record, query))
            except Exception as exc:  # one bad query shouldn't sink the run
                errors.append(f"{query['search_term']!r}: {exc}")

    jobs = _apply_remote_filter(_dedup(collected), remote_preference)
    if jobs:
        return {"jobs": jobs, "source": "jobspy", "sites": sites, "errors": errors}

    if allow_sample_fallback:
        if got_live:
            errors.append("Live scrape returned no postings; showing sample data.")
        sample = _apply_remote_filter(_sample_jobs(queries), remote_preference)
        return {
            "jobs": sample,
            "source": "sample",
            "sites": sites,
            "errors": errors,
        }

    return {"jobs": [], "source": "jobspy", "sites": sites, "errors": errors}
