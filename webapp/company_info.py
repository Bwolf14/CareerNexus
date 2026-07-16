"""
Company background lookup for the job-detail page — non-AI, structured.

Pipeline (all free, keyless public APIs, cached per company):

1. **Wikidata entity search** (``wbsearchentities``) — crucially, this matches
   *aliases*, so "RBC" finds *Royal Bank of Canada* even though no Wikipedia
   page is titled "RBC". Candidates are filtered to ones whose description
   looks like an organisation, so "Apple" the fruit never wins.
2. **Wikidata claims** for the chosen entity give structured facts: industry,
   headquarters, country, founding year, employee count, annual revenue, and
   official website — the reliable, non-AI company data shown on the page.
3. The entity's **Wikipedia sitelink** provides the prose summary paragraph.

The AI overview (ai_client.generate_company_overview) is layered on top only
as an async enhancement, grounded in this collected data plus the posting text.

When nothing trustworthy comes back we return None and the page shows less —
better than confidently wrong. Results (including negatives) are cached as
JSON files in the job-results dir (shared volume).
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

_WD_API = "https://www.wikidata.org/w/api.php"
_WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
_TIMEOUT = (3.0, 8.0)
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "CareerNexus/1.0 (job-matching demo; company lookups)",
}

# Wikidata property ids for the structured facts we surface.
_P_INDUSTRY = "P452"
_P_HQ = "P159"
_P_COUNTRY = "P17"
_P_FOUNDED = "P571"
_P_EMPLOYEES = "P1128"
_P_REVENUE = "P2139"
_P_WEBSITE = "P856"
_P_POINT_IN_TIME = "P585"

# Trailing legal suffixes that hurt name matching.
_SUFFIX_RE = re.compile(
    r"[\s,]+(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|"
    r"co|co\.|company|plc|gmbh|s\.a\.|group|holdings)\.?$",
    re.IGNORECASE,
)

# An entity only counts as "the company" if its description looks
# organisational — otherwise we assume a same-named thing (fruit, city, …).
_ORG_HINTS = (
    "company", "corporation", "conglomerate", "manufacturer", "retailer",
    "retail", "bank", "banking", "firm", "chain", "provider", "brand",
    "subsidiary", "operator", "airline", "agency", "organization",
    "organisation", "enterprise", "business", "employer", "utility",
    "contractor", "services", "producer", "supplier", "developer", "startup",
    "franchise", "multinational", "holding", "insurer", "insurance",
    "telecommunications", "financial",
)


# ---------------------------------------------------------------------------
# Cache (v2 prefix: v1 cached exact-title misses, e.g. a permanent None for
# RBC, which must not survive the switch to entity search)
# ---------------------------------------------------------------------------
def _cache_path(company: str) -> str:
    key = hashlib.sha1(company.strip().lower().encode("utf-8")).hexdigest()[:16]
    return os.path.join(results_dir(), f"company2_{key}.json")


def cached_company(company: str) -> dict[str, Any]:
    try:
        with open(_cache_path(company), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def update_cached_company(company: str, **fields: Any) -> None:
    try:
        data = cached_company(company)
        data.update(fields)
        os.makedirs(results_dir(), exist_ok=True)
        with open(_cache_path(company), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass  # cache is best-effort


# ---------------------------------------------------------------------------
# Low-level fetches (each returns None/[] on any failure)
# ---------------------------------------------------------------------------
def _get_json(url: str, params: Optional[dict] = None) -> Optional[dict]:
    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT, headers=_HEADERS)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def _wd_search(name: str) -> list[dict[str, Any]]:
    """Wikidata entity candidates for a name (matches labels AND aliases)."""
    data = _get_json(_WD_API, {
        "action": "wbsearchentities", "search": name, "language": "en",
        "uselang": "en", "type": "item", "limit": 8, "format": "json",
    })
    return (data or {}).get("search") or []


def _wd_entities(qids: list[str]) -> dict[str, dict[str, Any]]:
    """Batch-fetch full entities (claims + labels + all sitelinks) in one call."""
    qids = [q for q in dict.fromkeys(qids) if q]
    if not qids:
        return {}
    data = _get_json(_WD_API, {
        "action": "wbgetentities", "ids": "|".join(qids[:10]),
        "props": "claims|descriptions|labels|sitelinks",
        "languages": "en", "format": "json",
    })
    entities = (data or {}).get("entities") or {}
    return {q: e for q, e in entities.items() if isinstance(e, dict) and "missing" not in e}


def _wd_labels(qids: list[str]) -> dict[str, str]:
    """Batch-resolve entity ids to English labels (one request)."""
    qids = [q for q in dict.fromkeys(qids) if q]
    if not qids:
        return {}
    data = _get_json(_WD_API, {
        "action": "wbgetentities", "ids": "|".join(qids[:50]),
        "props": "labels", "languages": "en", "format": "json",
    })
    out = {}
    for qid, ent in ((data or {}).get("entities") or {}).items():
        label = ((ent.get("labels") or {}).get("en") or {}).get("value")
        if label:
            out[qid] = label
    return out


def _fetch_summary(title: str) -> Optional[dict[str, Any]]:
    data = _get_json(_WIKI_SUMMARY.format(title=quote(title, safe="")))
    if not data or data.get("type") == "disambiguation":
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


# ---------------------------------------------------------------------------
# Claim parsing
# ---------------------------------------------------------------------------
def _statements(entity: dict, prop: str) -> list[dict]:
    stmts = (entity.get("claims") or {}).get(prop) or []
    # Prefer statements Wikidata marks preferred (usually the latest figures).
    preferred = [s for s in stmts if s.get("rank") == "preferred"]
    return preferred or stmts


def _snak_value(stmt: dict) -> Any:
    return (((stmt.get("mainsnak") or {}).get("datavalue")) or {}).get("value")


def _stmt_year(stmt: dict) -> Optional[int]:
    quals = (stmt.get("qualifiers") or {}).get(_P_POINT_IN_TIME) or []
    for q in quals:
        time_str = ((q.get("datavalue") or {}).get("value") or {}).get("time") or ""
        m = re.match(r"[+-](\d{4})", time_str)
        if m:
            return int(m.group(1))
    return None


def _latest(stmts: list[dict]) -> Optional[dict]:
    if not stmts:
        return None
    return max(stmts, key=lambda s: _stmt_year(s) or -1)


def _item_ids(entity: dict, prop: str, limit: int = 3) -> list[str]:
    out = []
    for stmt in _statements(entity, prop)[:limit]:
        value = _snak_value(stmt)
        if isinstance(value, dict) and value.get("id"):
            out.append(value["id"])
    return out


def _humanize(amount: float) -> str:
    for cut, word in ((1e12, "trillion"), (1e9, "billion"), (1e6, "million")):
        if abs(amount) >= cut:
            return f"{amount / cut:.1f} {word}"
    return f"{amount:,.0f}"


def _quantity(stmt: Optional[dict]) -> tuple[Optional[float], Optional[str], Optional[int]]:
    """(amount, unit entity id, year) from a quantity statement."""
    if not stmt:
        return None, None, None
    value = _snak_value(stmt)
    if not isinstance(value, dict):
        return None, None, None
    try:
        amount = float(value.get("amount"))
    except (TypeError, ValueError):
        return None, None, None
    unit = value.get("unit") or ""
    unit_id = unit.rsplit("/", 1)[-1] if unit.startswith("http") else None
    return amount, unit_id, _stmt_year(stmt)


def _looks_like_org(text: str) -> bool:
    text = (text or "").lower()
    return any(hint in text for hint in _ORG_HINTS)


def _org_candidates(candidates: list[dict]) -> list[dict]:
    return [c for c in candidates
            if c.get("id") and _looks_like_org(c.get("description") or "")][:5]


def _most_prominent(entities: dict[str, dict], order: list[str]) -> Optional[str]:
    """The candidate with the most Wikipedia sitelinks wins.

    Short names collide ("RBC" is also the Rwanda Biomedical Center); sitelink
    count is a strong proxy for the organisation a job posting most likely
    means. Ties keep the search order.
    """
    best_qid, best_score = None, -1
    for pos, qid in enumerate(order):
        entity = entities.get(qid)
        if not entity:
            continue
        score = len(entity.get("sitelinks") or {}) * 100 - pos
        if score > best_score:
            best_qid, best_score = qid, score
    return best_qid


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _build_profile(entity: dict, cand: dict) -> dict[str, Any]:
    emp_amount, _, emp_year = _quantity(_latest(_statements(entity, _P_EMPLOYEES)))
    rev_amount, rev_unit, rev_year = _quantity(_latest(_statements(entity, _P_REVENUE)))

    industry_ids = _item_ids(entity, _P_INDUSTRY)
    hq_ids = _item_ids(entity, _P_HQ, limit=1)
    country_ids = _item_ids(entity, _P_COUNTRY, limit=1)
    labels = _wd_labels(industry_ids + hq_ids + country_ids + ([rev_unit] if rev_unit else []))

    founded = None
    founded_stmt = _latest(_statements(entity, _P_FOUNDED))
    if founded_stmt:
        value = _snak_value(founded_stmt)
        m = re.match(r"[+-](\d{4})", (value or {}).get("time") or "") if isinstance(value, dict) else None
        if m:
            founded = int(m.group(1))

    website = None
    site_stmt = _latest(_statements(entity, _P_WEBSITE))
    if site_stmt and isinstance(_snak_value(site_stmt), str):
        website = _snak_value(site_stmt)

    facts: dict[str, Any] = {}
    if industry_ids:
        names = [labels[i] for i in industry_ids if i in labels]
        if names:
            facts["industry"] = ", ".join(names)
    hq_bits = [labels.get(hq_ids[0])] if hq_ids else []
    if country_ids and labels.get(country_ids[0]) not in hq_bits:
        hq_bits.append(labels.get(country_ids[0]))
    hq_text = ", ".join(b for b in hq_bits if b)
    if hq_text:
        facts["headquarters"] = hq_text
    if founded:
        facts["founded"] = str(founded)
    if emp_amount:
        facts["employees"] = f"{emp_amount:,.0f}" + (f" ({emp_year})" if emp_year else "")
    if rev_amount:
        unit_label = labels.get(rev_unit, "") if rev_unit else ""
        facts["revenue"] = (
            f"{_humanize(rev_amount)}"
            + (f" {unit_label}" if unit_label else "")
            + (f" ({rev_year})" if rev_year else "")
        )
    if website:
        facts["website"] = website

    # Prose paragraph via the entity's English Wikipedia article, if any.
    wiki = None
    sitelink = ((entity.get("sitelinks") or {}).get("enwiki") or {}).get("title")
    if sitelink:
        wiki = _fetch_summary(sitelink)

    label = ((entity.get("labels") or {}).get("en") or {}).get("value") or cand.get("label")
    description = ((entity.get("descriptions") or {}).get("en") or {}).get("value") \
        or cand.get("description")
    return {
        "source": "wikidata",
        "qid": entity.get("id"),
        "name": label,
        "description": description,
        "extract": (wiki or {}).get("extract"),
        "url": (wiki or {}).get("url"),
        "facts": facts,
    }


def company_profile(company: str) -> Optional[dict[str, Any]]:
    """Structured, non-AI company profile — or None when nothing trustworthy.

    Cached per company (negatives too). Alias-aware: "RBC" resolves to Royal
    Bank of Canada via Wikidata's alias index.
    """
    company = (company or "").strip()
    if not company:
        return None

    cached = cached_company(company)
    if "profile" in cached:
        return cached["profile"]

    org_cands = _org_candidates(_wd_search(company))
    if not org_cands:
        cleaned = _SUFFIX_RE.sub("", company).strip()
        if cleaned and cleaned.lower() != company.lower():
            org_cands = _org_candidates(_wd_search(cleaned))

    profile = None
    if org_cands:
        order = [c["id"] for c in org_cands]
        entities = _wd_entities(order)
        winner = _most_prominent(entities, order)
        if winner:
            cand = next(c for c in org_cands if c["id"] == winner)
            profile = _build_profile(entities[winner], cand)

    update_cached_company(company, profile=profile)
    return profile


def wikipedia_summary(company: str) -> Optional[dict[str, Any]]:
    """Back-compat prose-only view of :func:`company_profile`."""
    profile = company_profile(company)
    if not profile or not profile.get("extract"):
        return None
    return {
        "title": profile.get("name"),
        "description": profile.get("description"),
        "extract": profile.get("extract"),
        "url": profile.get("url"),
    }
