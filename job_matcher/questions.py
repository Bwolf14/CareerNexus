"""
Template-generated follow-up questions for the profiling step.

The real product plans to have an AI interviewer read the parsed resume and the
scraped postings, then ask 4–8 tailored questions. That model isn't wired in
yet, so this module produces the same *shape* of output deterministically:
a handful of resume-specific questions built from templates, plus the standard
career-goals questions (5–10 year plan, pay range, work style, priorities).

Every question is a plain dict the web layer can render as a form field::

    {
      "id":      str,              # stable key the answers are stored under
      "prompt":  str,              # the question text
      "type":    "textarea" | "text" | "choice" | "multichoice" | "salary",
      "options": [str, ...],       # for choice/multichoice
      "hint":    str | None,       # small helper text under the field
      "origin":  "resume" | "standard",
    }

Machine-usable answers (``preferred_skills``, ``salary``, ``work_style``) feed
directly into :mod:`job_matcher.scoring`; free-text answers are stored with the
session so the future AI step can use them.
"""

from __future__ import annotations

from typing import Any, Optional

MAX_QUESTIONS = 8
MIN_QUESTIONS = 4

WORK_STYLE_OPTIONS = ["Remote", "Hybrid", "On-site", "No preference"]
PRIORITY_OPTIONS = [
    "Career growth",
    "Compensation",
    "Job stability",
    "Company culture",
    "Learning new skills",
    "Leadership opportunities",
    "Work–life balance",
]


def _q(
    qid: str,
    prompt: str,
    qtype: str = "textarea",
    *,
    options: Optional[list[str]] = None,
    hint: Optional[str] = None,
    origin: str = "standard",
) -> dict[str, Any]:
    return {
        "id": qid,
        "prompt": prompt,
        "type": qtype,
        "options": options or [],
        "hint": hint,
        "origin": origin,
    }


def _most_recent_role(parsed: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Current role if flagged, else the first experience entry (resume order)."""
    experience = parsed.get("experience") or []
    for exp in experience:
        if (exp.get("dates") or {}).get("is_current"):
            return exp
    return experience[0] if experience else None


def _resume_questions(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Up to three questions grounded in the person's own resume."""
    questions: list[dict[str, Any]] = []

    exp = _most_recent_role(parsed)
    if exp and (exp.get("title") or exp.get("company")):
        title = exp.get("title") or "your role"
        at = f" at {exp['company']}" if exp.get("company") else ""
        current = (exp.get("dates") or {}).get("is_current")
        verb = "work" if current else "worked"
        questions.append(
            _q(
                "recent_role",
                f"I see you {verb} as {title}{at}. Which parts of that job would "
                "you like more of in your next role — and which would you rather "
                "leave behind?",
                hint="Think day-to-day tasks, responsibilities, tools, and the people side.",
                origin="resume",
            )
        )

    projects = parsed.get("projects") or []
    if projects and projects[0].get("title"):
        questions.append(
            _q(
                "project_detail",
                f"You listed the project “{projects[0]['title']}” — tell me more "
                "about it. What was your role, and what are you most proud of?",
                hint="Concrete details help match you with roles that use the same strengths.",
                origin="resume",
            )
        )

    education = parsed.get("education") or []
    current_edu = next(
        (e for e in education if (e.get("dates") or {}).get("is_current")), None
    )
    if current_edu and (current_edu.get("institution") or current_edu.get("field_of_study")):
        field = current_edu.get("field_of_study") or "your program"
        school = (
            f" at {current_edu['institution']}" if current_edu.get("institution") else ""
        )
        questions.append(
            _q(
                "studies",
                f"You're currently studying {field}{school}. Are you looking for "
                "work that fits around school (part-time, internships, co-op), or "
                "full-time roles?",
                "text",
                origin="resume",
            )
        )

    return questions


def _skills_question(parsed: dict[str, Any]) -> Optional[dict[str, Any]]:
    skills = [s for s in (parsed.get("skills") or {}).get("raw") or [] if s]
    if len(skills) < 4:
        return None
    return _q(
        "preferred_skills",
        "Which of these skills do you most want to use day-to-day?",
        "multichoice",
        options=skills[:8],
        hint="Pick any number — jobs that use them will rank higher.",
        origin="resume",
    )


def build_questions(
    parsed: dict[str, Any],
    jobs: Optional[list[dict[str, Any]]] = None,
    max_questions: int = MAX_QUESTIONS,
) -> list[dict[str, Any]]:
    """Build the 4–8 question list for a parsed resume.

    ``jobs`` is accepted (and currently unused) so the signature already
    matches the future AI implementation, which will read the postings to ask
    sharper questions.
    """
    questions = _resume_questions(parsed)

    standard = [
        _q(
            "five_year",
            "Where do you see yourself in 5–10 years?",
            hint="A rough direction is fine — deepen your craft, lead a team, "
            "switch specialties, start something of your own…",
        ),
        _q(
            "salary",
            "What is your ideal, realistic pay range?",
            "salary",
            hint="Postings inside your range rank higher; ones below it are flagged.",
        ),
        _q(
            "work_style",
            "How do you prefer to work?",
            "choice",
            options=WORK_STYLE_OPTIONS,
        ),
        _q(
            "priorities",
            "What matters most to you in your next role?",
            "multichoice",
            options=PRIORITY_OPTIONS,
            hint="Pick up to three.",
        ),
    ]

    skills_q = _skills_question(parsed)

    # Interleave: lead with the personal questions, keep the goal-setting
    # questions in a sensible conversational order, close with priorities.
    ordered: list[dict[str, Any]] = []
    ordered.extend(questions[:2])
    ordered.append(standard[0])  # five_year
    if skills_q:
        ordered.append(skills_q)
    ordered.extend(questions[2:])
    ordered.append(standard[1])  # salary
    ordered.append(standard[2])  # work_style
    ordered.append(standard[3])  # priorities

    return ordered[:max_questions]
