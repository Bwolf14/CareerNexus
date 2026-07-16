"""
Company background lookup for the job-detail page.

Non-AI first: Wikipedia's public REST summary API gives a reliable, quotable
description for companies that have an article — no key, no scraping. The AI
overview (ai_client.generate_company_overview) is layered on top *only* as an
async enhancement, summarising the data we actually collected (the Wikipedia
extract + the job posting's own text) so it can't invent facts from nothing.

Wrong-page risk: "Apple" resolves to the fruit. We query "<name> (company)"
first, then the raw/cleaned name, and only accept a page whose description
looks like an organisation. When nothing trustworthy comes back we return None
and the page simply shows less — better than confidently wrong.

Results are cached as JSON files in the job-results dir (shared volume), keyed
by company name, so repeat visits don't re-hit Wikipedia or the model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import quote

import requests

from job_scraper.output import results_dir

log = logging.getLogger("careernexus.company")

_WIKI_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
_TIMEOUT = (3.0, 8.0)

# Trailing legal suffixes that hurt Wikipedia title matching.
_SUFFIX_RE = re.compile(
    r"[\s,]+(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|"
    r"co|co\.|company|plc|gmbh|s\.a\.|group|holdings)\.?$",
    re.IGNORECASE,
)

# A Wikipedia page only counts as "the company" if its short description looks
# organisational — otherwise we assume we hit a same-named thing (fruit, city…).
_ORG_HINTS = (
    "company", "corporation", "conglomerate", "manufacturer", "retailer",
    "retail", "bank", "firm", "chain", "provider", "brand", "subsidiary",
    "operator", "airline", "agency", "organization", "organisation",
    "enterprise", "business", "employer", "utility", "contractor",
    "services", "producer", "supplier", "developer", "startup", "franchise",
    "multinational", "holding",
)


def _cache_path(company: str) -> str:
    key = hashlib.sha1(company.strip().lower().encode("utf-8")).hexdigest()[:16]
    return os.path.join(results_dir(), f"company_{key}.json")


def _cache_load(company: str) -> Optional[dict[str, Any]]:
    try:
        with open(_cache_path(company), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _cache_save(company: str, data: dict[str, Any]) -> None:
    try:
        os.makedirs(results_dir(), exist_ok=True)
        with open(_cache_path(company), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass  # cache is best-effort


def cached_company(company: str) -> dict[str, Any]:
    """The cached record for a company (empty dict when none)."""
    return _cache_load(company) or {}


def update_cached_company(company: str, **fields: Any) -> None:
    data = cached_company(company)
    data.update(fields)
    _cache_save(company, data)


def _fetch_summary(title: str) -> Optional[dict[str, Any]]:
    try:
        resp = requests.get(
            _WIKI_URL.format(title=quote(title, safe="")),
            timeout=_TIMEOUT,
            headers={"Accept": "application/json"},
        )
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if data.get("type") == "disambiguation":
        return None
    extract = (data.get("extract") or "").strip()
    if not extract:
        return None
    return {
        "title": data.get("title") or title,
        "description": (data.get("description") or "").strip(),
        "extract": extract,
        "url": ((data.get("content_urls") or {}).get("desktop") or {}).get("page"),
    }


def _looks_like_org(summary: dict[str, Any]) -> bool:
    text = f"{summary.get('description', '')} {summary.get('extract', '')[:200]}".lower()
    return any(hint in text for hint in _ORG_HINTS)


def wikipedia_summary(company: str) -> Optional[dict[str, Any]]:
    """Best-effort Wikipedia summary for a company, or None.

    Cached per company (including negative results, stored as wiki=None, so a
    company without an article doesn't cost a network hit every page view).
    """
    company = (company or "").strip()
    if not company:
        return None

    cached = cached_company(company)
    if "wiki" in cached:
        return cached["wiki"]

    base = _SUFFIX_RE.sub("", company).strip() or company
    candidates = [f"{base} (company)", company]
    if base != company:
        candidates.append(base)

    result = None
    for title in candidates:
        summary = _fetch_summary(title)
        if summary and _looks_like_org(summary):
            result = summary
            break

    update_cached_company(company, wiki=result)
    return result
