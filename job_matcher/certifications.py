"""
Certification-demand analysis across a set of job postings.

Scans every matched posting's title + description for mentions of known
certifications (dictionary below — IT-heavy, but covering trades, healthcare,
finance, PM, food service, and more), then compares what the market is asking
for against what the resume already lists. The output drives the
"Certifications to consider" section of the recommendations page:

    78% of your matched jobs mention Cisco CCNA — consider earning it.

Purely deterministic string matching; no AI involved. Adding a certification
is a one-entry change to ``CERTIFICATIONS``.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Each entry: canonical display name, a category label for the UI, and the
# lowercase aliases matched (with non-alphanumeric boundaries) in job text and
# in the resume's own certifications/skills. Short, ambiguous abbreviations
# ("RN", "PE") are deliberately omitted from aliases — only unambiguous forms
# are matched, so a stray two-letter hit can't produce a bogus recommendation.
CERTIFICATIONS: list[dict[str, Any]] = [
    # --- IT: networking / systems ---
    {"name": "Cisco CCNA", "category": "IT — Networking",
     "aliases": ["ccna", "cisco certified network associate"]},
    {"name": "Cisco CCNP", "category": "IT — Networking",
     "aliases": ["ccnp", "cisco certified network professional"]},
    {"name": "CompTIA A+", "category": "IT — Support",
     "aliases": ["comptia a+", "a+ certification", "a+ certified"]},
    {"name": "CompTIA Network+", "category": "IT — Networking",
     "aliases": ["network+", "comptia network+"]},
    {"name": "CompTIA Security+", "category": "IT — Security",
     "aliases": ["security+", "comptia security+"]},
    {"name": "CompTIA Linux+", "category": "IT — Systems",
     "aliases": ["linux+", "comptia linux+"]},
    {"name": "Microsoft Certified: Azure Fundamentals", "category": "IT — Cloud",
     "aliases": ["az-900", "azure fundamentals"]},
    {"name": "Microsoft Certified: Azure Administrator", "category": "IT — Cloud",
     "aliases": ["az-104", "azure administrator"]},
    {"name": "AWS Certified Cloud Practitioner", "category": "IT — Cloud",
     "aliases": ["aws certified cloud practitioner", "aws cloud practitioner"]},
    {"name": "AWS Certified Solutions Architect", "category": "IT — Cloud",
     "aliases": ["aws certified solutions architect", "aws solutions architect"]},
    {"name": "Google Cloud Associate Cloud Engineer", "category": "IT — Cloud",
     "aliases": ["associate cloud engineer", "gcp associate"]},
    {"name": "CISSP", "category": "IT — Security",
     "aliases": ["cissp", "certified information systems security professional"]},
    {"name": "CISM", "category": "IT — Security",
     "aliases": ["cism", "certified information security manager"]},
    {"name": "Certified Ethical Hacker (CEH)", "category": "IT — Security",
     "aliases": ["ceh", "certified ethical hacker"]},
    {"name": "ITIL Foundation", "category": "IT — Service Management",
     "aliases": ["itil"]},
    {"name": "Certified Kubernetes Administrator (CKA)", "category": "IT — DevOps",
     "aliases": ["cka", "certified kubernetes administrator"]},
    # --- Project management / process ---
    {"name": "PMP (Project Management Professional)", "category": "Project Management",
     "aliases": ["pmp", "project management professional"]},
    {"name": "CAPM", "category": "Project Management",
     "aliases": ["capm", "certified associate in project management"]},
    {"name": "Certified ScrumMaster / PSM", "category": "Project Management",
     "aliases": ["certified scrummaster", "certified scrum master", "csm certification",
                 "professional scrum master", "psm i", "psm certification"]},
    {"name": "Lean Six Sigma", "category": "Process Improvement",
     "aliases": ["six sigma", "lean six sigma", "green belt", "black belt"]},
    # --- Finance / business ---
    {"name": "CPA (Chartered Professional Accountant)", "category": "Finance",
     "aliases": ["cpa", "chartered professional accountant",
                 "certified public accountant"]},
    {"name": "CFA (Chartered Financial Analyst)", "category": "Finance",
     "aliases": ["cfa", "chartered financial analyst"]},
    {"name": "CFP (Certified Financial Planner)", "category": "Finance",
     "aliases": ["cfp", "certified financial planner"]},
    # --- HR / marketing ---
    {"name": "SHRM-CP / CPHR", "category": "Human Resources",
     "aliases": ["shrm-cp", "shrm certified", "cphr", "phr certification"]},
    {"name": "Google Analytics Certification", "category": "Marketing",
     "aliases": ["google analytics certification", "google analytics certified", "ga4 certification"]},
    {"name": "Google Ads Certification", "category": "Marketing",
     "aliases": ["google ads certification", "google ads certified"]},
    {"name": "HubSpot Certification", "category": "Marketing",
     "aliases": ["hubspot certification", "hubspot certified"]},
    # --- Healthcare ---
    {"name": "Registered Nurse (RN) license", "category": "Healthcare",
     "aliases": ["registered nurse", "rn license", "rn licence"]},
    {"name": "Licensed Practical Nurse (LPN)", "category": "Healthcare",
     "aliases": ["licensed practical nurse", "lpn"]},
    {"name": "BLS (Basic Life Support)", "category": "Healthcare",
     "aliases": ["basic life support", "bls certification", "bls certified"]},
    {"name": "ACLS", "category": "Healthcare",
     "aliases": ["acls", "advanced cardiac life support"]},
    {"name": "First Aid / CPR", "category": "Health & Safety",
     "aliases": ["first aid", "cpr certification", "cpr certified", "cpr/aed"]},
    # --- Trades / industrial ---
    {"name": "Red Seal endorsement", "category": "Skilled Trades",
     "aliases": ["red seal"]},
    {"name": "Forklift certification", "category": "Warehouse & Logistics",
     "aliases": ["forklift certification", "forklift certified", "forklift ticket",
                 "forklift license", "forklift licence"]},
    {"name": "WHMIS", "category": "Health & Safety",
     "aliases": ["whmis"]},
    {"name": "OSHA 10/30", "category": "Health & Safety",
     "aliases": ["osha 10", "osha 30", "osha certification", "osha certified"]},
    {"name": "H2S Alive", "category": "Oil & Gas Safety",
     "aliases": ["h2s alive", "h2s certification"]},
    {"name": "CWB welding certification", "category": "Skilled Trades",
     "aliases": ["cwb", "cwb certified", "welding ticket", "welding certification"]},
    {"name": "Commercial driver's licence (CDL / Class 1)", "category": "Transportation",
     "aliases": ["cdl", "class 1 license", "class 1 licence", "class 1 driver",
                 "commercial driver's license", "commercial driver's licence"]},
    # --- Food service / retail / other ---
    {"name": "Food Safe / ServSafe", "category": "Food Service",
     "aliases": ["servsafe", "food safe", "food handler certification", "food handlers certificate"]},
    {"name": "Smart Serve / ProServe", "category": "Food Service",
     "aliases": ["smart serve", "proserve"]},
    {"name": "Security guard licence", "category": "Security",
     "aliases": ["security guard license", "security guard licence", "security license", "security licence"]},
    {"name": "Real estate licence", "category": "Real Estate",
     "aliases": ["real estate license", "real estate licence"]},
    {"name": "TEFL / TESOL", "category": "Education",
     "aliases": ["tefl", "tesol"]},
]

# Non-alphanumeric boundaries instead of \b so aliases ending in "+" ("A+",
# "Security+") still terminate cleanly.
def _alias_pattern(alias: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(alias.lower()) + r"(?![a-z0-9])")


_COMPILED: list[dict[str, Any]] = [
    {**cert, "patterns": [_alias_pattern(a) for a in cert["aliases"]]}
    for cert in CERTIFICATIONS
]


def _mentions(text: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(text) for p in patterns)


def _job_text(job: dict[str, Any]) -> str:
    return f"{job.get('title') or ''} {job.get('description') or ''}".lower()


def _resume_cert_text(parsed: dict[str, Any]) -> str:
    """All the places a held certification can appear in the parsed resume."""
    parts: list[str] = []
    for cert in parsed.get("certifications") or []:
        parts.append(str(cert.get("name") or ""))
        parts.append(str(cert.get("issuer") or ""))
    parts.extend(str(s) for s in (parsed.get("skills") or {}).get("raw") or [])
    for heading, lines in (parsed.get("additional_sections") or {}).items():
        if "cert" in heading.lower() or "licen" in heading.lower():
            parts.extend(str(line) for line in lines)
    return " ".join(parts).lower()


def analyze_certifications(
    parsed: dict[str, Any], jobs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compare certification demand in ``jobs`` against the resume.

    Returns::

        {
          "total_jobs": int,
          "gaps":  [ {name, category, job_count, job_pct, message}, ... ],
          "held":  [ {name, category, job_count, job_pct, message}, ... ],
          "unrecognized_held": [str, ...],   # resume certs not in our dictionary
        }

    ``gaps`` are certifications mentioned by postings but absent from the
    resume, sorted by how many postings ask for them. ``held`` are the
    resume's certifications with their market-demand numbers, so the UI can
    affirm them ("keep it prominent"). Both lists cover only certifications
    that at least one posting mentions.
    """
    total = len(jobs)
    resume_text = _resume_cert_text(parsed)
    job_texts = [_job_text(j) for j in jobs]

    gaps: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []

    for cert in _COMPILED:
        count = sum(1 for text in job_texts if _mentions(text, cert["patterns"]))
        user_has = _mentions(resume_text, cert["patterns"])
        if count == 0 and not user_has:
            continue
        pct = round(100 * count / total) if total else 0
        entry = {
            "name": cert["name"],
            "category": cert["category"],
            "job_count": count,
            "job_pct": pct,
        }
        if user_has:
            if count:
                entry["message"] = (
                    f"You already hold {cert['name']} — it's mentioned in {pct}% of "
                    "your matched jobs. Make sure it's easy to spot on your resume."
                )
            else:
                entry["message"] = (
                    f"You hold {cert['name']}. None of these postings call for it, "
                    "but it never hurts to list it."
                )
            held.append(entry)
        elif count:
            if pct >= 50:
                entry["message"] = (
                    f"{pct}% of jobs that fit your search mention {cert['name']} "
                    "and you don't have it listed — it's likely worth earning."
                )
            else:
                entry["message"] = (
                    f"{pct}% of your matched jobs mention {cert['name']} — "
                    "you may want to consider it."
                )
            gaps.append(entry)

    gaps.sort(key=lambda c: c["job_count"], reverse=True)
    held.sort(key=lambda c: c["job_count"], reverse=True)

    # Resume certifications our dictionary doesn't know — still worth showing.
    unrecognized: list[str] = []
    known_text = " ".join(a for c in CERTIFICATIONS for a in c["aliases"])
    for cert in parsed.get("certifications") or []:
        name = (cert.get("name") or "").strip()
        if name and name.lower() not in known_text and not _mentions(
            name.lower(), [p for c in _COMPILED for p in c["patterns"]]
        ):
            unrecognized.append(name)

    return {
        "total_jobs": total,
        "gaps": gaps[:8],
        "held": held,
        "unrecognized_held": unrecognized,
    }
