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

The whole flow is gated behind a login: visitors register with an email +
password and must consent to sensitive-data collection to create an account.
Each account only sees its own resumes and searches.

Auth + supporting routes
------------------------
GET/POST /register                create an account (consent required)
GET/POST /login                   log in
POST /logout                      log out
POST /matches/<search_id>/retry   re-run a search that fell back to sample data
GET  /api/resumes                 JSON list of the account's resumes (internal)
GET  /api/jobs/<search_id>        JSON of a stored job search (internal)
GET  /health                      liveness probe
GET  /jobs, /jobs/<id>            legacy URLs → redirect into the new flow

Run directly with ``python -m webapp.app`` (debug server) or via gunicorn in
the container (``webapp.app:app``).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from ai_client import (
    AIClientError,
    generate_match_analysis,
    generate_questions,
    generate_resume_tailoring,
    load_settings,
    normalize_base_url,
    save_settings,
    test_connection,
)
from ai_client.settings import is_configured, settings_path
from job_matcher import (
    analyze_certifications,
    build_questions,
    build_resume_tips,
    score_jobs,
    tailor_for_job,
)
from job_scraper import DEFAULT_SITES, dedupe_cross_board
from job_scraper.output import build_payload
from job_scraper.scraper import COUNTRY_INDEED
from resume_parser import parse_resume
from resume_parser.exceptions import ResumeParserError

from . import ai_store, auth, db, email_utils, plan_store, search_service
from .auth import current_user, login_required

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
# Signs the session cookie that carries the logged-in user id and flash
# messages. Set FLASK_SECRET_KEY in production so sessions survive restarts and
# can't be forged; the default only exists so the demo runs out of the box.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "careernexus-demo-flash-key")


@app.context_processor
def inject_current_user() -> dict:
    """Expose the logged-in account to every template as ``current_user``."""
    return {"current_user": current_user()}


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


def _require_owned_resume(resume_id: int) -> None:
    """404 if the resume doesn't exist, 403 if it belongs to another account."""
    owner = db.get_resume_owner(resume_id)
    if owner is None:
        abort(404, description="No parsed resume found with that id.")
    if owner != current_user()["id"]:
        abort(403, description="That resume belongs to another account.")


def _require_owned_search(search_row: dict) -> None:
    """403 if the search belongs to another account."""
    if search_row.get("user_id") != current_user()["id"]:
        abort(403, description="That job search belongs to another account.")


def _saved_keys() -> set:
    """The current user's saved-job dedup keys (best-effort; empty on error)."""
    try:
        return db.saved_dedup_keys(current_user()["id"])
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Accounts — register / login / logout
# ---------------------------------------------------------------------------
MIN_PASSWORD_LEN = 8


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user() is not None:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        consent = bool(request.form.get("consent"))

        error = None
        if "@" not in email or "." not in email.split("@")[-1]:
            error = "Enter a valid email address."
        elif len(password) < MIN_PASSWORD_LEN:
            error = f"Password must be at least {MIN_PASSWORD_LEN} characters."
        elif password != confirm:
            error = "The two passwords don't match."
        elif not consent:
            # Hard gate: no account without consent to data collection.
            error = (
                "You must consent to the collection of your data to create an "
                "account and use the job search."
            )

        if error is None:
            try:
                user_id = db.create_user(email, auth.hash_password(password), consent)
            except ValueError as exc:
                error = str(exc)
            except Exception as exc:
                error = f"Could not create your account: {exc}"

        if error is not None:
            flash(error, "error")
            return render_template("register.html", email=email, consent=consent), 400

        auth.login_user(user_id)
        flash("Account created — welcome to Career Nexus.", "info")
        return redirect(url_for("index"))

    return render_template("register.html", email="", consent=False)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user() is not None:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        throttle_id = email.lower()

        try:
            locked_until = db.throttle_status(throttle_id)
        except Exception:
            locked_until = None
        if locked_until:
            flash(
                "Too many failed attempts. Try again later or reset your password.",
                "error",
            )
            return render_template("login.html", email=email), 429

        try:
            user = db.get_user_by_email(email)
        except Exception as exc:
            flash(f"Could not reach the account database: {exc}", "error")
            return render_template("login.html", email=email), 503

        if user and auth.verify_password(user["password_hash"], password):
            try:
                db.clear_login_failures(throttle_id)
            except Exception:
                pass
            auth.login_user(user["id"])
            dest = request.args.get("next")
            # Only allow local redirects (no open-redirect to other hosts).
            if not dest or not dest.startswith("/"):
                dest = url_for("index")
            return redirect(dest)

        try:
            db.record_login_failure(
                throttle_id, auth.MAX_LOGIN_FAILS, auth.LOGIN_LOCK_SECONDS
            )
        except Exception:
            pass
        flash("Incorrect email or password.", "error")
        return render_template("login.html", email=email), 401

    return render_template("login.html", email="")


@app.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if current_user() is not None:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        # Always show the same confirmation, whether or not the email exists —
        # don't let this endpoint reveal which addresses are registered.
        try:
            user = db.get_user_by_email(email)
            if user:
                token, token_hash = auth.new_reset_token()
                expires = datetime.utcnow() + timedelta(seconds=auth.RESET_TTL_SECONDS)
                db.create_password_reset(user["id"], token_hash, expires)
                link = email_utils.base_url() + url_for("reset_password", token=token)
                email_utils.send_email(
                    user["email"],
                    "Reset your Career Nexus password",
                    "We received a request to reset your password.\n\n"
                    f"Use this link within {auth.RESET_TTL_SECONDS // 60} minutes:\n"
                    f"{link}\n\nIf you didn't request this, ignore this email.",
                )
        except Exception:
            pass  # never leak backend/DB state on this endpoint
        flash(
            "If that email is registered, a reset link is on its way. "
            "(In the demo it's printed to the server log.)",
            "info",
        )
        return redirect(url_for("login"))

    return render_template("forgot.html")


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    if current_user() is not None:
        return redirect(url_for("index"))

    try:
        row = db.get_valid_reset(auth.hash_token(token))
    except Exception:
        row = None
    if row is None:
        return (
            render_template(
                "error.html",
                heading="Reset link invalid or expired",
                error="Password-reset links can only be used once and expire after "
                "an hour. Request a new one.",
                back_url=url_for("forgot_password"),
                back_label="Request a new link",
            ),
            400,
        )

    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        error = None
        if len(password) < MIN_PASSWORD_LEN:
            error = f"Password must be at least {MIN_PASSWORD_LEN} characters."
        elif password != confirm:
            error = "The two passwords don't match."
        if error:
            flash(error, "error")
            return render_template("reset.html", token=token), 400

        db.reset_password(row["id"], row["user_id"], auth.hash_password(password))
        flash("Your password has been reset — log in with your new password.", "info")
        return redirect(url_for("login"))

    return render_template("reset.html", token=token)


@app.route("/logout", methods=["POST"])
def logout():
    auth.logout_user()
    flash("You've been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/account")
@login_required
def account():
    return render_template("account.html", account=current_user())


@app.route("/account/export")
@login_required
def account_export():
    """Download everything stored for the logged-in account as one JSON file."""
    data = db.export_user_data(current_user()["id"])
    blob = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    resp = app.response_class(blob, mimetype="application/json")
    resp.headers["Content-Disposition"] = (
        'attachment; filename="careernexus_my_data.json"'
    )
    return resp


@app.route("/account/delete", methods=["POST"])
@login_required
def account_delete():
    """Permanently delete the account and everything belonging to it."""
    user = current_user()
    confirm = (request.form.get("confirm_email") or "").strip().lower()
    if confirm != (user["email"] or "").lower():
        flash("Type your email exactly to confirm account deletion.", "error")
        return redirect(url_for("account"))
    try:
        db.delete_user(user["id"])
    except Exception as exc:
        flash(f"Could not delete your account: {exc}", "error")
        return redirect(url_for("account"))
    auth.logout_user()
    flash("Your account and all associated data have been permanently deleted.", "info")
    return redirect(url_for("register"))


# ---------------------------------------------------------------------------
# Step 1 — home + upload
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    resumes: list = []
    searches: list = []
    db_error = None
    uid = current_user()["id"]
    try:
        resumes = db.list_resumes(user_id=uid)
        searches = db.list_job_searches(user_id=uid)
    except Exception as exc:  # surfaced in the UI rather than 500-ing
        db_error = str(exc)
    return render_template(
        "home.html", resumes=resumes, searches=searches, db_error=db_error
    )


@app.route("/upload", methods=["POST"])
@login_required
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
        resume_id = db.save_parsed_resume(parsed, user_id=current_user()["id"])
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
@login_required
def profile(resume_id: int):
    _require_owned_resume(resume_id)
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
        site_choices=SITE_CHOICES,
        default_sites=DEFAULT_SITES,
        default_country=_default_country(),
        **_step_urls(resume_id=resume_id),
    )


def _scrape_async() -> bool:
    """Whether searches are queued for the worker (default) or run inline.

    Read per-request (not at import) so it can be toggled with the
    ``SCRAPE_ASYNC`` env var, and disabled in tests.
    """
    return os.environ.get("SCRAPE_ASYNC", "1") != "0"


def _nothing_to_search(resume_id: int):
    return (
        render_template(
            "error.html",
            heading="Nothing to search on",
            error="We couldn't derive any search terms — add a keyword to the "
            "search form, or upload a resume with a clearer work-experience section.",
            back_url=url_for("profile", resume_id=resume_id),
            back_label="Back to your profile",
        ),
        400,
    )


def _run_search_sync(resume_id: int, parsed: dict, settings: dict):
    """Run the scrape inline (fallback path) and redirect to the matches page."""
    try:
        res = search_service.run_scrape(resume_id, parsed, settings)
    except search_service.NoQueriesError:
        return _nothing_to_search(resume_id)
    except Exception as exc:
        return (
            render_template(
                "error.html",
                heading="Search couldn't be stored",
                error=f"The scrape ran but couldn't be saved: {exc}. The follow-up "
                "steps need the database — check the DB connection and try again.",
                back_url=url_for("profile", resume_id=resume_id),
                back_label="Back to your profile",
            ),
            500,
        )
    for note in res["errors"]:
        flash(note, "info")
    return redirect(url_for("matches", search_id=res["search_id"]))


def _start_search(resume_id: int, parsed: dict, settings: dict):
    """Kick off a search: queue it for the worker (async) or run it inline."""
    if not search_service.build_queries(parsed, settings):
        return _nothing_to_search(resume_id)
    if _scrape_async():
        try:
            job_id = db.enqueue_scrape(current_user()["id"], resume_id, settings)
            return redirect(url_for("scrape_status", job_id=job_id))
        except Exception:
            pass  # queue unavailable -> fall back to a synchronous scrape
    return _run_search_sync(resume_id, parsed, settings)


@app.route("/profile/<int:resume_id>/search", methods=["POST"])
@login_required
def search(resume_id: int):
    _require_owned_resume(resume_id)
    parsed = db.get_resume_json(resume_id)
    if parsed is None:
        abort(404, description="No parsed resume found with that id.")
    settings = _read_job_settings(request.form)
    return _start_search(resume_id, parsed, settings)


@app.route("/matches/<int:search_id>/retry", methods=["POST"])
@login_required
def retry_search(search_id: int):
    """Re-run a search that fell back to sample data, reusing its parameters.

    Country and work-type aren't persisted per search, so the retry uses the
    resume's stored search terms + location + boards with default work-type and
    region — enough to have another go at getting live postings.
    """
    search_row = _load_search_or_404(search_id)
    _require_owned_search(search_row)
    resume_id = search_row.get("resume_id")
    if not resume_id:
        abort(400, description="This search isn't linked to a resume, so it can't be retried.")
    parsed = db.get_resume_json(resume_id)
    if parsed is None:
        abort(404, description="The resume behind this search no longer exists.")

    sites = [s for s in (search_row.get("sites_searched") or "").split(",")
             if s in ALLOWED_SITES] or list(DEFAULT_SITES)
    settings = {
        "keywords": [],
        "location": search_row.get("location"),
        "work_type": "any",
        "country": _default_country(),
        "sites": sites,
    }
    return _start_search(resume_id, parsed, settings)


@app.route("/scrape/<int:job_id>")
@login_required
def scrape_status(job_id: int):
    """Progress page for a queued scrape; auto-refreshes until the worker is done."""
    job = db.get_scrape_job(job_id)
    if job is None:
        abort(404, description="No scrape job with that id.")
    if job.get("user_id") not in (None, current_user()["id"]):
        abort(403, description="That scrape belongs to another account.")

    status = job.get("status")
    if status == "done" and job.get("search_id"):
        return redirect(url_for("matches", search_id=job["search_id"]))
    if status == "error":
        return (
            render_template(
                "error.html",
                heading="Search failed",
                error=job.get("error") or "The background scrape failed.",
                back_url=url_for("profile", resume_id=job.get("resume_id"))
                if job.get("resume_id") else url_for("index"),
                back_label="Back to your profile",
            ),
            200,
        )
    return render_template("scrape_status.html", job=job)


# ---------------------------------------------------------------------------
# Step 3 — browse the matches
# ---------------------------------------------------------------------------
@app.route("/matches/<int:search_id>")
@login_required
def matches(search_id: int):
    search_row = _load_search_or_404(search_id)
    _require_owned_search(search_row)
    jobs = dedupe_cross_board(db.get_jobs_for_search(search_id))
    saved_keys = _saved_keys()
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
        saved_keys=saved_keys,
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
        ai_on=is_configured(load_settings()),
        **_step_urls(resume_id=search_row.get("resume_id"), search_id=search_id),
    )


# ---------------------------------------------------------------------------
# Step 4 — follow-up questionnaire
# ---------------------------------------------------------------------------
def _questionnaire_for(
    search_id: int, parsed: dict, jobs: list, *, regenerate: bool = False
) -> tuple[list[dict], dict]:
    """The question list for a search, plus an ``ai_status`` dict for the UI.

    The rendered list is cached per search (see :mod:`webapp.ai_store`) so the
    answering POST maps onto exactly the questions the user saw — essential
    when the AI wrote them, since generation isn't reproducible. Falls back to
    the deterministic template questions whenever the AI is unconfigured or
    fails.
    """
    ai_settings = load_settings()
    ai_status = {
        "configured": is_configured(ai_settings),
        "model": ai_settings.get("model"),
        "generator": "template",
        "error": None,
    }

    if not regenerate:
        cached = ai_store.load("questions", search_id)
        if cached and cached.get("questions"):
            ai_status["generator"] = cached.get("generator", "template")
            if cached.get("model"):
                ai_status["model"] = cached["model"]
            return cached["questions"], ai_status

    question_list = None
    if ai_status["configured"]:
        try:
            question_list = generate_questions(ai_settings, parsed, jobs)
            ai_status["generator"] = "ai"
        except AIClientError as exc:
            ai_status["error"] = str(exc)
    if question_list is None:
        question_list = build_questions(parsed, jobs)

    try:
        ai_store.save(
            "questions",
            search_id,
            {
                "generator": ai_status["generator"],
                "model": ai_settings.get("model")
                if ai_status["generator"] == "ai"
                else None,
                "questions": question_list,
            },
        )
    except Exception:
        pass  # cache is best-effort; template fallback keeps POST consistent
    return question_list, ai_status


@app.route("/questions/<int:search_id>", methods=["GET", "POST"])
@login_required
def questions(search_id: int):
    search_row = _load_search_or_404(search_id)
    _require_owned_search(search_row)
    parsed, _ = _resume_for_search(search_row)
    jobs = db.get_jobs_for_search(search_id)

    if request.method == "POST":
        question_list, _ = _questionnaire_for(search_id, parsed, jobs)
        if not request.form.get("skip"):
            answers = _collect_answers(question_list, request.form)
            warning = plan_store.save_answers(
                search_id, search_row.get("resume_id"), answers
            )
            if warning:
                flash(warning, "warn")
        return redirect(url_for("recommendations", search_id=search_id))

    regenerate = request.args.get("regen") == "1"
    question_list, ai_status = _questionnaire_for(
        search_id, parsed, jobs, regenerate=regenerate
    )
    return render_template(
        "questions.html",
        step=4,
        search_id=search_id,
        job_count=len(jobs),
        questions=question_list,
        answers=plan_store.load_answers(search_id),
        ai_status=ai_status,
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


def _attach_ai_analysis(
    search_id: int, parsed: dict, picks: list[dict], answers: dict | None
) -> tuple[str | None, dict]:
    """Fill ``pick["ai_analysis"]`` on each pick; return (overall, ai_status).

    Results are cached per search, keyed by a hash of the answers and the
    shortlist, so revisits are instant but a changed questionnaire or a
    re-ranked list triggers a fresh generation.
    """
    ai_settings = load_settings()
    ai_status = {
        "configured": is_configured(ai_settings),
        "model": ai_settings.get("model"),
        "used": False,
        "error": None,
    }
    if not ai_status["configured"] or not picks:
        return None, ai_status

    basis = json.dumps(
        {
            "answers": answers or {},
            "picks": [
                [(p.get("job") or {}).get("source_site"),
                 (p.get("job") or {}).get("external_id")]
                for p in picks
            ],
            "model": ai_settings.get("model"),
        },
        sort_keys=True,
        default=str,
    )
    cache_key = hashlib.sha1(basis.encode("utf-8")).hexdigest()

    analysis = None
    if request.args.get("regen") != "1":
        cached = ai_store.load("analysis", search_id)
        if cached and cached.get("key") == cache_key:
            analysis = cached

    if analysis is None:
        try:
            result = generate_match_analysis(ai_settings, parsed, picks, answers)
            analysis = {
                "key": cache_key,
                "model": ai_settings.get("model"),
                "overall": result["overall"],
                # JSON object keys are strings; store them that way from the start.
                "per_index": {str(i): text for i, text in result["per_index"].items()},
            }
            try:
                ai_store.save("analysis", search_id, analysis)
            except Exception:
                pass
        except AIClientError as exc:
            ai_status["error"] = str(exc)
            return None, ai_status

    ai_status["used"] = True
    if analysis.get("model"):
        ai_status["model"] = analysis["model"]
    per_index = analysis.get("per_index") or {}
    for i, pick in enumerate(picks):
        pick["ai_analysis"] = per_index.get(str(i))
    return analysis.get("overall"), ai_status


@app.route("/recommendations/<int:search_id>")
@login_required
def recommendations(search_id: int):
    search_row = _load_search_or_404(search_id)
    _require_owned_search(search_row)
    jobs = dedupe_cross_board(db.get_jobs_for_search(search_id))
    parsed, notes = _resume_for_search(search_row)

    answers = plan_store.load_answers(search_id)
    answered = answers is not None

    picks = score_jobs(parsed, jobs, answers or {})
    certs = analyze_certifications(parsed, jobs)
    tips = build_resume_tips(parsed, cert_analysis=certs) if parsed else []

    overall_analysis, ai_status = _attach_ai_analysis(search_id, parsed, picks, answers)

    answers_display = []
    if answers:
        # Use the questionnaire the user actually answered (it may have been
        # AI-generated), falling back to the deterministic template list.
        cached_q = ai_store.load("questions", search_id)
        question_list = (cached_q or {}).get("questions") or build_questions(parsed, jobs)
        for q in question_list:
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
        ai_status=ai_status,
        overall_analysis=overall_analysis,
        **_step_urls(resume_id=search_row.get("resume_id"), search_id=search_id),
    )


# ---------------------------------------------------------------------------
# Saved jobs + application tracker
# ---------------------------------------------------------------------------
@app.route("/saved/add", methods=["POST"])
@login_required
def saved_add():
    """Bookmark a posting from a search's results (snapshotted into saved_jobs)."""
    search_id = request.form.get("search_id", type=int)
    dedup_key = (request.form.get("dedup_key") or "").strip()
    search_row = db.get_job_search(search_id) if search_id else None
    if search_row is None:
        abort(404, description="No job search found with that id.")
    _require_owned_search(search_row)

    jobs = dedupe_cross_board(db.get_jobs_for_search(search_id))
    job = next((j for j in jobs if j.get("dedup_key") == dedup_key), None)
    if job is None:
        abort(404, description="That posting isn't in this search anymore.")

    try:
        db.save_job(current_user()["id"], dedup_key, job)
        flash(f"Saved “{job.get('title') or 'posting'}” to your tracker.", "info")
    except Exception as exc:
        flash(f"Could not save that job: {exc}", "error")
    return redirect(url_for("matches", search_id=search_id) + "#job-" + dedup_key)


@app.route("/saved")
@login_required
def saved_jobs_page():
    """The application tracker: saved jobs grouped by status."""
    try:
        jobs = db.list_saved_jobs(current_user()["id"])
        db_error = None
    except Exception as exc:
        jobs, db_error = [], str(exc)
    columns = [(s, [j for j in jobs if j["status"] == s]) for s in db.SAVED_JOB_STATUSES]
    return render_template(
        "saved.html",
        columns=columns,
        statuses=db.SAVED_JOB_STATUSES,
        total=len(jobs),
        db_error=db_error,
    )


@app.route("/saved/<int:saved_id>/update", methods=["POST"])
@login_required
def saved_update(saved_id: int):
    status = request.form.get("status")
    notes = request.form.get("notes")
    try:
        ok = db.update_saved_job(current_user()["id"], saved_id, status, notes)
        if not ok:
            abort(404, description="No saved job with that id.")
    except ValueError:
        abort(400, description="Invalid status.")
    return redirect(url_for("saved_jobs_page"))


@app.route("/saved/<int:saved_id>/delete", methods=["POST"])
@login_required
def saved_delete(saved_id: int):
    db.delete_saved_job(current_user()["id"], saved_id)
    flash("Removed from your tracker.", "info")
    return redirect(url_for("saved_jobs_page"))


# ---------------------------------------------------------------------------
# Saved searches + scheduled alerts
# ---------------------------------------------------------------------------
ALERT_FREQUENCIES = {"daily", "weekly"}


@app.route("/alerts")
@login_required
def alerts_page():
    uid = current_user()["id"]
    db_error = None
    saved_searches, resumes = [], []
    try:
        saved_searches = db.list_saved_searches(uid)
        resumes = db.list_resumes(user_id=uid)
    except Exception as exc:
        db_error = str(exc)
    return render_template(
        "alerts.html",
        saved_searches=saved_searches,
        resumes=resumes,
        db_error=db_error,
        site_choices=SITE_CHOICES,
        default_sites=DEFAULT_SITES,
        default_country=_default_country(),
    )


@app.route("/alerts/create", methods=["POST"])
@login_required
def alerts_create():
    resume_id = request.form.get("resume_id", type=int)
    if not resume_id:
        flash("Choose a resume to base the alert on.", "error")
        return redirect(url_for("alerts_page"))
    _require_owned_resume(resume_id)

    params = _read_job_settings(request.form)
    label = (request.form.get("label") or "").strip() or None
    frequency = (request.form.get("frequency") or "daily").strip().lower()
    if frequency not in ALERT_FREQUENCIES:
        frequency = "daily"

    # next_run_at in the past → the worker's next scheduler tick runs it.
    try:
        db.create_saved_search(
            current_user()["id"], resume_id, label, params, frequency,
            datetime.utcnow(),
        )
        flash("Alert saved — it'll run on the schedule and email you new matches.", "info")
    except Exception as exc:
        flash(f"Could not save that alert: {exc}", "error")
    return redirect(url_for("alerts_page"))


@app.route("/alerts/<int:saved_search_id>/toggle", methods=["POST"])
@login_required
def alerts_toggle(saved_search_id: int):
    active = request.form.get("active") == "1"
    db.set_saved_search_active(current_user()["id"], saved_search_id, active)
    return redirect(url_for("alerts_page"))


@app.route("/alerts/<int:saved_search_id>/delete", methods=["POST"])
@login_required
def alerts_delete(saved_search_id: int):
    db.delete_saved_search(current_user()["id"], saved_search_id)
    flash("Alert deleted.", "info")
    return redirect(url_for("alerts_page"))


# ---------------------------------------------------------------------------
# Tailor-my-resume for one posting (AI when configured, else deterministic)
# ---------------------------------------------------------------------------
@app.route("/tailor/<int:search_id>/<dedup_key>")
@login_required
def tailor(search_id: int, dedup_key: str):
    search_row = _load_search_or_404(search_id)
    _require_owned_search(search_row)
    parsed, _ = _resume_for_search(search_row)
    if not parsed:
        abort(400, description="This search isn't linked to a resume, so it can't be tailored.")

    job = next(
        (j for j in dedupe_cross_board(db.get_jobs_for_search(search_id))
         if j.get("dedup_key") == dedup_key),
        None,
    )
    if job is None:
        abort(404, description="That posting isn't in this search anymore.")

    ai_settings = load_settings()
    ai_status = {"configured": is_configured(ai_settings),
                 "model": ai_settings.get("model"), "used": False, "error": None}
    tailoring = None
    if ai_status["configured"]:
        try:
            tailoring = generate_resume_tailoring(ai_settings, parsed, job)
            ai_status["used"] = True
        except AIClientError as exc:
            ai_status["error"] = str(exc)
    if tailoring is None:
        tailoring = tailor_for_job(parsed, job)

    return render_template(
        "tailor.html",
        step=3,
        job=job,
        tailoring=tailoring,
        ai_status=ai_status,
        search_id=search_id,
        resume_id=search_row.get("resume_id"),
        **_step_urls(resume_id=search_row.get("resume_id"), search_id=search_id),
    )


# ---------------------------------------------------------------------------
# AI settings (Ollama / any OpenAI-compatible server)
# ---------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    if request.method == "POST":
        submitted = {
            "enabled": bool(request.form.get("enabled")),
            "base_url": (request.form.get("base_url") or "").strip(),
            "model": (request.form.get("model") or "").strip(),
            "connect_timeout": request.form.get("connect_timeout") or 4,
            "read_timeout": request.form.get("read_timeout") or 180,
        }
        error = None
        if submitted["enabled"] and not normalize_base_url(submitted["base_url"]):
            error = "Enter the AI server address before enabling AI features."
        elif submitted["enabled"] and not submitted["model"]:
            error = (
                "Pick a model before enabling AI features — use “Test connection” "
                "to list what the server has installed."
            )
        if error:
            flash(error, "error")
            current = {**load_settings(), **submitted}
            return render_template(
                "settings.html", s=current, settings_file=settings_path()
            ), 400

        save_settings(submitted)
        flash("AI settings saved.", "info")
        return redirect(url_for("settings_page"))

    return render_template(
        "settings.html", s=load_settings(), settings_file=settings_path()
    )


@app.route("/api/ai/test", methods=["POST"])
@login_required
def api_ai_test():
    """Probe an AI server (address from the request body, not saved settings).

    Powers the settings page's Test button, so users can verify a URL before
    saving it. Returns ``{ok, latency_ms, models, error, normalized_url}``.
    """
    data = request.get_json(silent=True) or {}
    base = normalize_base_url(data.get("base_url") or "")
    result = test_connection({"base_url": base, "connect_timeout": 4.0})
    result["normalized_url"] = base
    return jsonify(result)


# ---------------------------------------------------------------------------
# JSON APIs (internal — no user-facing download buttons)
# ---------------------------------------------------------------------------
def _job_search_payload(search_id: int):
    """Build the JSON payload for a stored search, or None if not found."""
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


@app.route("/api/resumes")
@login_required
def api_resumes():
    resumes = db.list_resumes(user_id=current_user()["id"])
    # upload_date is a datetime; make it JSON-serialisable.
    for r in resumes:
        if r.get("upload_date") is not None:
            r["upload_date"] = r["upload_date"].isoformat()
    return jsonify(resumes)


@app.route("/api/jobs/<int:search_id>")
@login_required
def api_jobs(search_id: int):
    search_row = db.get_job_search(search_id)
    if search_row is None:
        abort(404, description="No job search found with that id.")
    _require_owned_search(search_row)
    payload = _job_search_payload(search_id)
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


@app.errorhandler(403)
def forbidden(exc):
    return (
        render_template(
            "error.html",
            heading="Not your data",
            error=getattr(exc, "description", None)
            or "You don't have access to that.",
            back_url=url_for("index"),
            back_label="Back to your sessions",
        ),
        403,
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
