"""
Career Nexus job matcher — deterministic (non-AI) matching engine.

This package powers the "AI" sections of the web UI *today*, without any model:

* :mod:`job_matcher.scoring` ranks scraped postings against a parsed resume
  (plus the user's follow-up answers) with a transparent heuristic score,
* :mod:`job_matcher.certifications` measures certification demand across the
  matched postings ("78% of your matched jobs mention CCNA…"),
* :mod:`job_matcher.questions` generates the follow-up questionnaire from the
  resume with templates, and
* :mod:`job_matcher.resume_tips` produces concrete resume-improvement advice.

When the real AI matching/profiling step lands, it slots in behind the same
call sites: the scoring reasons and question wording become model-generated,
while the data plumbing (resume JSON in, ranked jobs out) stays identical.
"""

from __future__ import annotations

from .certifications import analyze_certifications
from .questions import build_questions
from .resume_tips import build_resume_tips
from .scoring import score_jobs

__all__ = [
    "analyze_certifications",
    "build_questions",
    "build_resume_tips",
    "score_jobs",
]
