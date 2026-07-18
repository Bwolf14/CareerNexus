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
      "type":    "textarea" | "text" | "choice" | "multichoice" | "salary" | "experience",
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

from .experience import estimate_experience_years

MAX_QUESTIONS = 10
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
    max_select: Optional[int] = None,
) -> dict[str, Any]:
    return {
        "id": qid,
        "prompt": prompt,
        "type": qtype,
        "options": options or [],
        "hint": hint,
        "origin": origin,
        # For multichoice: the maximum number of options the user may pick
        # (None = unlimited). Enforced in the browser + on the server.
        "max_select": max_select,
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
        hint="Pick up to three — jobs that use them will rank higher.",
        origin="resume",
        max_select=3,
    )


def _aspiration_questions() -> list[dict[str, Any]]:
    """Open-ended questions a resume can't answer — motivations, culture, dreams.

    These give the AI matcher (and the future interviewer) the personal context
    keyword matching can never capture: what the person actually wants next.
    """
    return [
        _q(
            "aspiration_tech",
            "What kind of technology, tools, or ways of working excite you right now?",
            hint="Anything you'd love to spend more time with — even if it isn't "
            "on your resume yet.",
            origin="aspiration",
        ),
        _q(
            "aspiration_culture",
            "What kind of company culture are you looking for?",
            hint="Pace, team size, formality, how decisions get made, how wins "
            "and mistakes are handled…",
            origin="aspiration",
        ),
        _q(
            "aspiration_dream",
            "If you could do any job in your field, anywhere, what would it be — "
            "and what draws you to it?",
            hint="Dream big: a company, a role, a mission, a place.",
            origin="aspiration",
        ),
        _q(
            "aspiration_day",
            "Describe a genuinely great workday. What were you doing, and with whom?",
            hint="Helps us weigh day-to-day fit, not just job titles.",
            origin="aspiration",
        ),
    ]


def _experience_question(parsed: dict[str, Any]) -> dict[str, Any]:
    """Years-of-experience slider, pre-filled from the resume's date ranges.

    The value doubles as a target seniority: 0 ≈ entry level, 40+ ≈ senior
    leadership. Scoring uses it to avoid ranking roles that demand far more
    (or far less) experience than the user has at the top.
    """
    est = estimate_experience_years(parsed)
    if est is not None:
        hint = (
            f"We estimated {est} year{'' if est == 1 else 's'} from your resume — "
            "slide or type to correct it. This sets the level of role we rank "
            "first, from entry level (0) to senior leadership (40+)."
        )
    else:
        hint = (
            "From entry level (0) to senior leadership (40+). Roles asking for "
            "much more experience than this won't be ranked first."
        )
    q = _q(
        "experience_years",
        "How many years of experience do you have in this field — and what "
        "level of role are you aiming for?",
        "experience",
        hint=hint,
        origin="resume" if est is not None else "standard",
    )
    q["default"] = est
    return q


def _salary_question() -> dict[str, Any]:
    return _q(
        "salary",
        "What is your ideal, realistic pay range?",
        "salary",
        hint="Postings inside your range rank higher; ones below it are flagged.",
    )


def _work_style_question() -> dict[str, Any]:
    return _q(
        "work_style",
        "How do you prefer to work?",
        "choice",
        options=WORK_STYLE_OPTIONS,
    )


def _priorities_question() -> dict[str, Any]:
    return _q(
        "priorities",
        "What matters most to you in your next role?",
        "multichoice",
        options=PRIORITY_OPTIONS,
        hint="Pick up to three.",
        max_select=3,
    )


def structured_questions(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """The machine-usable preference questions, always asked.

    Their ids (``preferred_skills``, ``salary``, ``work_style``,
    ``priorities``) feed directly into :mod:`job_matcher.scoring`, so they are
    appended to the questionnaire even when an AI generates the open-ended
    part of the interview.
    """
    out: list[dict[str, Any]] = [_experience_question(parsed)]
    skills_q = _skills_question(parsed)
    if skills_q:
        out.append(skills_q)
    out.extend([_salary_question(), _work_style_question(), _priorities_question()])
    return out


def build_questions(
    parsed: dict[str, Any],
    jobs: Optional[list[dict[str, Any]]] = None,
    max_questions: int = MAX_QUESTIONS,
) -> list[dict[str, Any]]:
    """Build the 4–8 question list for a parsed resume (template path).

    ``jobs`` is accepted (and currently unused) so the signature matches the
    AI implementation (:func:`ai_client.features.generate_questions`), which
    reads the postings to ask sharper questions.
    """
    questions = _resume_questions(parsed)
    aspirations = _aspiration_questions()

    five_year = _q(
        "five_year",
        "Where do you see yourself in 5–10 years?",
        hint="A rough direction is fine — deepen your craft, lead a team, "
        "switch specialties, start something of your own…",
    )
    # The machine-usable tail always survives the cap — the ranking depends on
    # those ids (experience, skills, salary, work style, priorities). The
    # open-ended head mixes resume-grounded questions with the aspirational
    # ones (culture, dream job, great-day) a resume can't answer.
    tail = structured_questions(parsed)

    head: list[dict[str, Any]] = []
    head.extend(questions[:2])
    head.append(five_year)
    head.extend(aspirations)
    head.extend(questions[2:])

    return head[: max(0, max_questions - len(tail))] + tail
