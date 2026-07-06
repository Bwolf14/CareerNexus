"""
Questionnaire-answer storage with graceful degradation.

Answers live in the ``career_plans`` table when it exists (fresh databases
initialised from the current ``init.sql``). Databases created before that
table was added — or a DB that's down entirely — fall back to a JSON file in
the job-results directory (``plan_<search_id>.json``, same volume-mounted
directory as the jobs JSON), so the questionnaire → recommendations flow keeps
working either way.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from job_scraper.output import results_dir

from . import db


def _plan_path(search_id: int) -> str:
    return os.path.join(results_dir(), f"plan_{search_id}.json")


def save_answers(
    search_id: int, resume_id: Optional[int], answers: dict[str, Any]
) -> Optional[str]:
    """Persist answers; DB first, file as fallback.

    Returns a warning string when the DB copy could not be written (the file
    copy still succeeded), or raises only if *both* stores fail.
    """
    warning: Optional[str] = None
    try:
        db.save_plan_answers(search_id, resume_id, answers)
    except Exception as exc:
        warning = (
            "Answers were saved to a local file, but not the database "
            f"({exc}). If the database volume predates the career_plans "
            "table, re-initialise it with `docker compose down -v`."
        )

    try:
        os.makedirs(results_dir(), exist_ok=True)
        with open(_plan_path(search_id), "w", encoding="utf-8") as fh:
            json.dump(
                {"search_id": search_id, "resume_id": resume_id, "answers": answers},
                fh,
                indent=2,
                ensure_ascii=False,
            )
    except Exception:
        if warning is not None:
            raise  # both stores failed — surface the file error
    return warning


def load_answers(search_id: int) -> Optional[dict[str, Any]]:
    """Fetch answers for a search from the DB, else the file store, else None."""
    try:
        answers = db.get_plan_answers(search_id)
        if answers is not None:
            return answers
    except Exception:
        pass
    try:
        with open(_plan_path(search_id), encoding="utf-8") as fh:
            return json.load(fh).get("answers")
    except FileNotFoundError:
        return None
    except Exception:
        return None
