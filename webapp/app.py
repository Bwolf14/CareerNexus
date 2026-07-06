"""
Career Nexus web UI — a guided, five-step career-matching flow.

    1. Upload      POST /upload                      parse the resume, store it
    2. Profile     GET  /profile/<resume_id>         review what was extracted
    3. Matches     POST /profile/<resume_id>/search  scrape live job boards
                   GET  /matches/<search_id>         browse every posting found
    4. Questions   GET/POST /questions/<search_id>   follow-up questionnaire
                                                     (template-generated today,
                                                     AI interviewer later)
    5. Plan        GET  /recommendations/<search_id> ranked shortlist with
                                                     reasons, resume tips, and
                                                     certification gaps

Everything except the AI reasoning works end-to-end today: parsing, live
scraping (JobSpy), heuristic ranking, certification-demand analysis, and
resume tips are all deterministic. AI-dependent features are clearly labelled
placeholders in the UI.

Supporting routes
-----------------
GET  /download/<resume_id>        parsed-resume JSON download
GET  /jobs/download/<search_id>   job-search JSON download (for the AI matcher)
GET  /api/resumes                 JSON list of stored resumes
GET  /api/jobs/<search_id>        JSON of a stored job search
GET  /health                      liveness probe
GET  /jobs, /jobs/<id>            legacy URLs → redirect into the new flow

Run directly with ``python -m webapp.app`` (debug server) or via gunicorn in
the container (``webapp.app:app``).
"""

from __future__ import annotations

import io
import json
import os
import tempfile

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from job_matcher import (
    analyze_certifications,
    build_questions,
    build_resume_tips,
    score_jobs,
)
from job_scraper import (
    DEFAULT_SITES,
    build_queries_from_resume,
    scrape_jobs_for_queries,
    write_results_json,
)
from job_scraper.output import build_payload
from job_scraper.scraper import COUNTRY_INDEED
from resume_parser import parse_resume
from resume_parser.exceptions import ResumeParserError

from . import db, plan_store

# Search-form option whitelists (anything else falls back to the default).
WORK_TYPES = {"any", "remote", "local"}
COUNTRIES = {"Canada", "USA"}
MAX_KEYWORDS = 4
SITE_CHOICES = [
    {"key": "indeed", "label": "Indeed"},
    {"key": "zip_recruiter", "label": "ZipRecruiter"},
    {"key": "glassdoor", "label": "Glassdoor"},
    {"key": "linkedin", "label": "LinkedIn"},
]
ALLOWED_SITES = {c["key"] for c in SITE_CHOICES}

ALLOWED_EXTENSIONS = {".pdf", ".docx"}
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB upload cap

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
# Only used for flash messages (no auth, nothing sensitive in the session).
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "careernexus-demo-flash-key")


def _allowed(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS


def _parse_resume_file(file) -> dict:
    """Spool an uploaded file to a temp path and run it through the parser.

    Returns the parsed-resume dict. Raises ``ValueError`` for bad input
    (missing/unsupported file) and ``ResumeParserError`` for parse failures, so
    callers can map each to the right HTTP status.
    """
    if file is None or not file.filename:
        raise ValueError("No file was selected.")
    if not _allowed(file.filename):
        ext = os.path.splitext(file.filename)[1] or "no extension"
        raise ValueError(
            f"Unsupported file type. Please upload a PDF or DOCX (got {ext})."
        )

    suffix = os.path.splitext(file.filename)[1].lower()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        resume = parse_resume(tmp_path)
        return resume.model_dump(mode="json")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _default_country() -> str:
    return COUNTRY_INDEED if COUNTRY_INDEED in COUNTRIES else "Canada"


def _read_job_settings(form) -> dict:
    """Pull and sanitise the job-search options from the search form.

    Returns ``keywords`` (list[str]), ``location`` (str | None), ``work_type``
    ("any"/"remote"/"local"), ``country`` ("Canada"/"USA"), and ``sites``
    (validated board list, defaulting to the configured boards). Unknown or
    invalid values fall back to safe defaults.
    """
    raw_keywords = form.get("keywords", "")
    keywords = [k.strip() for k in raw_keywords.split(",") if k.strip()][:MAX_KEYWORDS]

    location = (form.get("location") or "").strip() or None

    work_type = (form.get("work_type") or "any").strip().lower()
    if work_type not in WORK_TYPES:
        work_type = "any"

    country = (form.get("country") or _default_country()).strip()
    if country not in COUNTRIES:
        country = _default_country()

    sites = [s for s in form.getlist("sites") if s in ALLOWED_SITES]
    if not sites:
        sites = list(DEFAULT_SITES)

    return {
        "keywords": keywords,
        "location": location,
        "work_type": work_type,
        "country": country,
        "sites": sites,
    }


def _step_urls(resume_id=None, search_id=None) -> dict:
    """Links for the progress stepper — only steps that exist yet get URLs."""
    return {
        "profile_url": url_for("profile", resume_id=resume_id) if resume_id else None,
        "matches_url": url_for("matches", search_id=search_id) if search_id else None,
        "questions_url": url_for("questions", search_id=search_id) if search_id else None,
        "plan_url": url_for("recommendations", search_id=search_id) if search_id else None,
    }


def _load_search_or_404(search_id: int) -> dict:
    search = db.get_job_search(search_id)
    if search is None:
        abort(404, description="No job search found with that id.")
    return search


def _resume_for_search(search: dict) -> tuple[dict, list[str]]:
    """The parsed resume behind a search (or {}), plus any warnings for the UI."""
    notes: list[str] = []
    resume_id = search.get("resume_id")
    if not resume_id:
        notes.append(
            "This search isn't linked to a stored resume, so ranking and advice "
            "are based on the postings alone."
        )
        return {}, notes
    try:
        parsed = db.get_resume_json(resume_id)
    except Exception as exc:
        notes.append(f"Could not load the stored resume: {exc}")
        return {}, notes
    if parsed is None:
        notes.append("The resume linked to this search no longer exists.")
        return {}, notes
    return parsed, notes


# ---------------------------------------------------------------------------
# Step 1 — home + upload
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    resumes: list = []
    searches: list = []
    db_error = None
    try:
        resumes = db.list_resumes()
        searches = db.list_job_searches()
    except Exception as exc:  # surfaced in the UI rather than 500-ing
        db_error = str(exc)
    return render_template(
        "home.html", resumes=resumes, searches=searches, db_error=db_error
    )


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("resume")
    try:
        parsed = _parse_resume_file(file)
    except ValueError as exc:
        return render_template("error.html", error=str(exc)), 400
    except ResumeParserError as exc:
        return (
            render_template(
                "error.html",
                heading="Could not read that file",
                error=f"{exc} — try re-exporting the resume as a text-based PDF "
                "or DOCX (scanned images can't be parsed).",
            ),
            400,
        )
    except Exception as exc:  # pragma: no cover - unexpected parser failure
        return (
            render_template(
                "error.html", error=f"Unexpected error while parsing: {exc}"
            ),
            500,
        )

    try:
        resume_id = db.save_parsed_resume(parsed)
    except Exception as exc:
        # Parsed fine but not stored: show the profile inline so the parse
        # isn't wasted, with the search step disabled (it needs the DB).
        return render_template(
            "profile.html",
            step=2,
            parsed=parsed,
            resume_id=None,
            filename=file.filename,
            db_error=str(exc),
            pretty_json=json.dumps(parsed, indent=2, ensure_ascii=False),
            site_choices=SITE_CHOICES,
            default_sites=DEFAULT_SITES,
            default_country=_default_country(),
            **_step_urls(),
        )

    return redirect(url_for("profile", resume_id=resume_id, f=file.filename))


# ---------------------------------------------------------------------------
# Step 2 — profile review + search kickoff
# ---------------------------------------------------------------------------
@app.route("/profile/<int:resume_id>")
def profile(resume_id: int):
    parsed = db.get_resume_json(resume_id)
    if parsed is None:
        abort(404, description="No parsed resume found with that id.")
    return render_template(
        "profile.html",
        step=2,
        parsed=parsed,
        resume_id=resume_id,
        filename=request.args.get("f"),
        db_error=None,
        pretty_json=json.dumps(parsed, indent=2, ensure_ascii=False),
        site_choices=SITE_CHOICES,
        default_sites=DEFAULT_SITES,
        default_country=_default_country(),
        **_step_urls(resume_id=resume_id),
    )


@app.route("/profile/<int:resume_id>/search", methods=["POST"])
def search(resume_id: int):
    parsed = db.get_resume_json(resume_id)
    if parsed is None:
        abort(404, description="No parsed resume found with that id.")

    settings = _read_job_settings(request.form)
    queries = build_queries_from_resume(
        parsed,
        location_override=settings["location"],
        extra_keywords=settings["keywords"],
    )
    if not queries:
        return (
            render_template(
                "error.html",
                heading="Nothing to search on",
                error="We couldn't derive any search terms — add a keyword to the "
                "search form, or upload a resume with a clearer work-experience "
                "section.",
                back_url=url_for("profile", resume_id=resume_id),
                back_label="Back to your profile",
            ),
            400,
        )

    result = scrape_jobs_for_queries(
        queries,
        sites=settings["sites"],
        country_indeed=settings["country"],
        remote_preference=settings["work_type"],
    )
    jobs = result["jobs"]
    search_terms = [q["search_term"] for q in queries]
    location = queries[0].get("location")

    try:
        search_id = db.save_job_search(
            resume_id, search_terms, location, result["sites"], jobs, result["source"]
        )
    except Exception as exc:
        # Not stored -> the questionnaire/plan steps can't run, but the results
        # are still worth showing inline.
        return render_template(
            "matches.html",
            step=3,
            jobs=jobs,
            job_count=len(jobs),
            search_terms=search_terms,
            location=location,
            source=result["source"],
            sites=result["sites"],
            settings=settings,
            errors=result.get("errors") or [],
            resume_id=resume_id,
            search_id=None,
            db_error=f"This search couldn't be stored, so the follow-up steps are "
            f"unavailable: {exc}",
            contact_name=(parsed.get("contact_info") or {}).get("name"),
            **_step_urls(resume_id=resume_id),
        )

    # Drop a JSON file pairing the search context with the postings, ready to
    # be fed to the AI matcher alongside the resume JSON.
    try:
        payload = build_payload(
            jobs=jobs,
            queries=queries,
            source=result["source"],
            sites=result["sites"],
            resume_id=resume_id,
            search_id=search_id,
            settings=settings,
        )
        write_results_json(search_id, payload)
    except Exception as exc:  # non-fatal: the DB copy is the canonical one
        flash(f"Results JSON file not written: {exc}", "warn")

    for note in result.get("errors") or []:
        flash(note, "info")

    return redirect(url_for("matches", search_id=search_id))


# ---------------------------------------------------------------------------
# Step 3 — browse the matches
# ---------------------------------------------------------------------------
@app.route("/matches/<int:search_id>")
def matches(search_id: int):
    search_row = _load_search_or_404(search_id)
    jobs = db.get_jobs_for_search(search_id)
    terms = [
        t.strip()
        for t in (search_row.get("search_terms") or "").split(",")
        if t.strip()
    ]
    return render_template(
        "matches.html",
        step=3,
        jobs=jobs,
        job_count=len(jobs),
        search_terms=terms,
        location=search_row.get("location"),
        source=search_row.get("source"),
        sites=[s for s in (search_row.get("sites_searched") or "").split(",") if s],
        settings=None,
        errors=[],
        resume_id=search_row.get("resume_id"),
        search_id=search_id,
        db_error=None,
        contact_name=search_row.get("username"),
        **_step_urls(resume_id=search_row.get("resume_id"), search_id=search_id),
    )


# ---------------------------------------------------------------------------
# Step 4 — follow-up questionnaire
# ---------------------------------------------------------------------------
@app.route("/questions/<int:search_id>", methods=["GET", "POST"])
def questions(search_id: int):
    search_row = _load_search_or_404(search_id)
    parsed, _ = _resume_for_search(search_row)
    jobs = db.get_jobs_for_search(search_id)
    question_list = build_questions(parsed, jobs)

    if request.method == "POST":
        if not request.form.get("skip"):
            answers = _collect_answers(question_list, request.form)
            warning = plan_store.save_answers(
                search_id, search_row.get("resume_id"), answers
            )
            if warning:
                flash(warning, "warn")
        return redirect(url_for("recommendations", search_id=search_id))

    return render_template(
        "questions.html",
        step=4,
        search_id=search_id,
        job_count=len(jobs),
        questions=question_list,
        answers=plan_store.load_answers(search_id),
        **_step_urls(resume_id=search_row.get("resume_id"), search_id=search_id),
    )


def _collect_answers(question_list: list[dict], form) -> dict:
    """Read the questionnaire form into an answers dict keyed by question id.

    Empty answers are dropped — every question is optional.
    """
    answers: dict = {}
    for q in question_list:
        qid = q["id"]
        if q["type"] == "multichoice":
            values = [v for v in form.getlist(qid) if v]
            if values:
                answers[qid] = values
        elif q["type"] == "salary":
            salary = {
                "min": (form.get(f"{qid}_min") or "").strip(),
                "max": (form.get(f"{qid}_max") or "").strip(),
                "interval": (form.get(f"{qid}_interval") or "yearly").strip(),
            }
            if salary["min"] or salary["max"]:
                answers[qid] = salary
        else:
            value = (form.get(qid) or "").strip()
            if value:
                answers[qid] = value
    return answers


# ---------------------------------------------------------------------------
# Step 5 — the career plan
# ---------------------------------------------------------------------------
def _format_answer(question: dict, answer) -> str:
    if question["type"] == "salary" and isinstance(answer, dict):
        lo = answer.get("min") or "?"
        hi = answer.get("max") or "?"
        return f"${lo} – ${hi} / {answer.get('interval', 'yearly')}"
    if isinstance(answer, list):
        return ", ".join(str(a) for a in answer)
    return str(answer)


@app.route("/recommendations/<int:search_id>")
def recommendations(search_id: int):
    search_row = _load_search_or_404(search_id)
    jobs = db.get_jobs_for_search(search_id)
    parsed, notes = _resume_for_search(search_row)

    answers = plan_store.load_answers(search_id)
    answered = answers is not None

    picks = score_jobs(parsed, jobs, answers or {})
    certs = analyze_certifications(parsed, jobs)
    tips = build_resume_tips(parsed, cert_analysis=certs) if parsed else []

    answers_display = []
    if answers:
        for q in build_questions(parsed, jobs):
            if q["id"] in answers:
                answers_display.append(
                    {"prompt": q["prompt"], "answer": _format_answer(q, answers[q["id"]])}
                )

    return render_template(
        "recommendations.html",
        step=5,
        search_id=search_id,
        resume_id=search_row.get("resume_id"),
        contact_name=search_row.get("username"),
        total_jobs=len(jobs),
        picks=picks,
        certs=certs,
        tips=tips,
        answered=answered,
        answers_display=answers_display,
        notes=notes,
        **_step_urls(resume_id=search_row.get("resume_id"), search_id=search_id),
    )


# ---------------------------------------------------------------------------
# Downloads + JSON APIs
# ---------------------------------------------------------------------------
@app.route("/download/<int:resume_id>")
def download(resume_id: int):
    data = db.get_resume_json(resume_id)
    if data is None:
        abort(404, description="No parsed resume found with that id.")
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    return send_file(
        io.BytesIO(payload),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"resume_{resume_id}.json",
    )


def _job_search_payload(search_id: int):
    """Build the downloadable JSON for a stored search, or None if not found."""
    search_row = db.get_job_search(search_id)
    if search_row is None:
        return None
    jobs = db.get_jobs_for_search(search_id)
    terms = [
        t.strip()
        for t in (search_row.get("search_terms") or "").split(",")
        if t.strip()
    ]
    queries = [{"search_term": t, "location": search_row.get("location")} for t in terms]
    return build_payload(
        jobs=jobs,
        queries=queries,
        source=search_row.get("source") or "jobspy",
        sites=(search_row.get("sites_searched") or "").split(","),
        resume_id=search_row.get("resume_id"),
        search_id=search_id,
    )


@app.route("/jobs/download/<int:search_id>")
def jobs_download(search_id: int):
    payload = _job_search_payload(search_id)
    if payload is None:
        abort(404, description="No job search found with that id.")
    blob = json.dumps(payload, indent=2, ensure_ascii=False, default=str).encode("utf-8")
    return send_file(
        io.BytesIO(blob),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"jobs_{search_id}.json",
    )


@app.route("/api/resumes")
def api_resumes():
    resumes = db.list_resumes()
    # upload_date is a datetime; make it JSON-serialisable.
    for r in resumes:
        if r.get("upload_date") is not None:
            r["upload_date"] = r["upload_date"].isoformat()
    return jsonify(resumes)


@app.route("/api/jobs/<int:search_id>")
def api_jobs(search_id: int):
    payload = _job_search_payload(search_id)
    if payload is None:
        abort(404, description="No job search found with that id.")
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Legacy URLs from the tabbed UI + error handling + health
# ---------------------------------------------------------------------------
@app.route("/jobs")
def legacy_jobs():
    return redirect(url_for("index"))


@app.route("/jobs/<int:search_id>")
def legacy_jobs_view(search_id: int):
    return redirect(url_for("matches", search_id=search_id))


@app.errorhandler(404)
def not_found(exc):
    return (
        render_template(
            "error.html",
            heading="Not found",
            error=getattr(exc, "description", None) or "That page doesn't exist.",
        ),
        404,
    )


@app.errorhandler(413)
def too_large(exc):
    return (
        render_template(
            "error.html",
            heading="File too large",
            error="Resumes are capped at 16 MB — export a smaller PDF/DOCX and try again.",
        ),
        413,
    )


@app.route("/health")
def health():
    try:
        conn = db.get_connection()
        conn.close()
        return jsonify(status="ok"), 200
    except Exception as exc:
        return jsonify(status="degraded", detail=str(exc)), 503


if __name__ == "__main__":
    # Direct-run convenience (the container uses gunicorn instead).
    db.wait_for_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=True)
