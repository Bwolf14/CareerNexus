"""
Deterministic resume-improvement tips.

Inspects the parsed resume (and optionally the certification analysis) for
concrete, actionable gaps: missing contact info, no summary, thin or
unquantified experience bullets, sparse skills, and formatting problems the
parser itself flagged. This fills the "How to improve your resume" section of
the recommendations page today; the future AI step will add rewrite
suggestions on top, using the same ``{severity, title, detail}`` shape.
"""

from __future__ import annotations

import re
from typing import Any, Optional

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _tip(severity: str, title: str, detail: str) -> dict[str, str]:
    return {"severity": severity, "title": title, "detail": detail}


def build_resume_tips(
    parsed: dict[str, Any],
    cert_analysis: Optional[dict[str, Any]] = None,
) -> list[dict[str, str]]:
    """Return improvement tips sorted most-important first."""
    tips: list[dict[str, str]] = []

    contact = parsed.get("contact_info") or {}
    links = contact.get("links") or {}

    # --- Contact info ----------------------------------------------------------
    missing_contact = [
        label
        for key, label in (("email", "an email address"), ("phone", "a phone number"))
        if not contact.get(key)
    ]
    if missing_contact:
        tips.append(
            _tip(
                "high",
                "Add missing contact details",
                f"We couldn't find {' or '.join(missing_contact)} on your resume. "
                "Recruiters filter out resumes they can't respond to — put your "
                "contact details at the very top.",
            )
        )
    if not links.get("linkedin"):
        tips.append(
            _tip(
                "low",
                "Link your LinkedIn profile",
                "Most recruiters look you up anyway; adding the link keeps you in "
                "control of what they find first.",
            )
        )
    if not contact.get("location"):
        tips.append(
            _tip(
                "low",
                "Add your city or region",
                "Location-filtered searches (both recruiters' and this site's) work "
                "much better when your resume states where you're based.",
            )
        )

    # --- Summary ---------------------------------------------------------------
    if not (parsed.get("summary") or "").strip():
        tips.append(
            _tip(
                "medium",
                "Add a professional summary",
                "Two or three sentences at the top — who you are, your strongest "
                "skills, and what you're looking for — give reviewers (and matching "
                "algorithms) instant context.",
            )
        )

    # --- Experience bullets ------------------------------------------------------
    experience = parsed.get("experience") or []
    bullets = [b for exp in experience for b in (exp.get("description") or []) if b]
    if experience and not bullets:
        tips.append(
            _tip(
                "high",
                "Describe what you did in each role",
                "Your work history lists positions but no accomplishments. Add 2–4 "
                "bullet points per role covering what you did and the impact it had.",
            )
        )
    elif experience and len(bullets) / len(experience) < 2:
        tips.append(
            _tip(
                "medium",
                "Expand your experience bullets",
                "Aim for 2–4 bullet points per role. Focus on outcomes and "
                "responsibilities, not just duties.",
            )
        )
    if bullets:
        quantified = sum(1 for b in bullets if re.search(r"\d", b))
        if quantified / len(bullets) < 0.2:
            tips.append(
                _tip(
                    "medium",
                    "Quantify your achievements",
                    "Few of your bullet points contain numbers. Metrics make impact "
                    "concrete — “cut ticket backlog 40%”, “supported 200+ users”, "
                    "“managed a $50k budget”.",
                )
            )

    # --- Skills ------------------------------------------------------------------
    skills = [s for s in (parsed.get("skills") or {}).get("raw") or [] if s]
    if len(skills) < 5:
        tips.append(
            _tip(
                "medium",
                "Build out your skills section",
                "We only found "
                f"{len(skills) or 'none'} listed skill{'s' if len(skills) != 1 else ''}. "
                "A dedicated skills section with 8–15 specific tools and technologies "
                "is what both keyword filters and this site's matching key on.",
            )
        )

    # --- Certifications ------------------------------------------------------------
    has_certs = bool(parsed.get("certifications"))
    top_gap = (cert_analysis or {}).get("gaps") or []
    if top_gap:
        gap = top_gap[0]
        tips.append(
            _tip(
                "medium",
                "Close your biggest certification gap",
                f"{gap['job_pct']}% of your matched postings mention "
                f"{gap['name']}. See the certifications section below for the "
                "full breakdown.",
            )
        )
    elif not has_certs:
        tips.append(
            _tip(
                "low",
                "List certifications (or start earning one)",
                "We didn't find a certifications section. Even entry-level "
                "certificates help you clear automated screening filters.",
            )
        )

    # --- Parser-detected formatting problems ---------------------------------------
    metadata = parsed.get("metadata") or {}
    status = metadata.get("section_detection_status")
    if status == "failed":
        tips.append(
            _tip(
                "high",
                "Use a simpler resume layout",
                "Automated parsers (ours included) couldn't detect standard sections "
                "in your resume — many employers' applicant-tracking systems will "
                "have the same problem. Use clear headings (Experience, Education, "
                "Skills) in a single-column layout.",
            )
        )
    elif metadata.get("warnings"):
        tips.append(
            _tip(
                "low",
                "Tighten up formatting for automated screeners",
                "Parts of your resume were ambiguous to automated parsing "
                f"({len(metadata['warnings'])} warning"
                f"{'s' if len(metadata['warnings']) != 1 else ''}). A consistent "
                "“Title — Company — Dates” line for each role avoids mix-ups in "
                "applicant-tracking systems.",
            )
        )

    tips.sort(key=lambda t: _SEVERITY_ORDER.get(t["severity"], 3))
    return tips
