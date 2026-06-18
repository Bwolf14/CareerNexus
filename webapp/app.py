"""
Career Nexus resume-parser web UI.

A small Flask app that lets you drop a PDF/DOCX resume into the browser, runs it
through :func:`resume_parser.parse_resume`, stores the result in the MariaDB
database, and offers the parsed JSON for download.

Routes
------
GET  /                  upload form + table of previously parsed resumes
POST /upload            parse an uploaded file, persist it, show the result
GET  /download/<id>     download the stored parsed JSON for a resume
GET  /api/resumes       JSON list of stored resumes
GET  /jobs              upload form + table of previous job searches
POST /jobs/search       parse a resume, scrape matching jobs, store + show them
GET  /jobs/<id>         view the postings from a stored job search
GET  /jobs/download/<id>  download a job search's results as JSON
GET  /api/jobs/<id>     JSON of a stored job search
GET  /health            liveness probe

Run directly with ``python -m webapp.app`` (debug server) or via gunicorn in the
container (``webapp.app:app``).
"""

from __future__ import annotations

import io
import json
import os
import tempfile

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
)

from job_scraper import (
    DEFAULT_SITES,
    build_queries_from_resume,
    scrape_jobs_for_queries,
    write_results_json,
)
from job_scraper.output import build_payload
from resume_parser import parse_resume
from resume_parser.exceptions import ResumeParserError

from . import db

ALLOWED_EXTENSIONS = {".pdf", ".docx"}
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB upload cap

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


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


@app.route("/")
def index():
    try:
        resumes = db.list_resumes()
        db_error = None
    except Exception as exc:  # surfaced in the UI rather than 500-ing
        resumes = []
        db_error = str(exc)
    return render_template("index.html", resumes=resumes, db_error=db_error)


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("resume")
    if file is None or not file.filename:
        return render_template("result.html", error="No file was selected."), 400
    if not _allowed(file.filename):
        return (
            render_template(
                "result.html",
                error=f"Unsupported file type. Please upload a PDF or DOCX "
                f"(got {os.path.splitext(file.filename)[1] or 'no extension'}).",
            ),
            400,
        )

    suffix = os.path.splitext(file.filename)[1].lower()
    tmp_path = None
    try:
        # parse_resume() needs a real path, so spool the upload to a temp file.
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name

        resume = parse_resume(tmp_path)
        parsed = resume.model_dump(mode="json")
    except ResumeParserError as exc:
        return (
            render_template(
                "result.html",
                error=f"Could not read the file: {exc}",
                filename=file.filename,
            ),
            400,
        )
    except Exception as exc:  # pragma: no cover - unexpected parser failure
        return (
            render_template(
                "result.html",
                error=f"Unexpected error while parsing: {exc}",
                filename=file.filename,
            ),
            500,
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    try:
        resume_id = db.save_parsed_resume(parsed)
        db_error = None
    except Exception as exc:
        resume_id = None
        db_error = str(exc)

    return render_template(
        "result.html",
        filename=file.filename,
        resume_id=resume_id,
        db_error=db_error,
        parsed=parsed,
        metadata=parsed.get("metadata", {}),
        pretty_json=json.dumps(parsed, indent=2, ensure_ascii=False),
    )


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


@app.route("/api/resumes")
def api_resumes():
    resumes = db.list_resumes()
    # upload_date is a datetime; make it JSON-serialisable.
    for r in resumes:
        if r.get("upload_date") is not None:
            r["upload_date"] = r["upload_date"].isoformat()
    return jsonify(resumes)


@app.route("/jobs")
def jobs_index():
    try:
        searches = db.list_job_searches()
        db_error = None
    except Exception as exc:  # surfaced in the UI rather than 500-ing
        searches = []
        db_error = str(exc)
    return render_template(
        "jobs.html", searches=searches, db_error=db_error, sites=DEFAULT_SITES
    )


@app.route("/jobs/search", methods=["POST"])
def jobs_search():
    """Parse an uploaded resume, scrape matching jobs, store + display them."""
    file = request.files.get("resume")
    try:
        parsed = _parse_resume_file(file)
    except ValueError as exc:
        return render_template("job_results.html", error=str(exc)), 400
    except ResumeParserError as exc:
        return (
            render_template(
                "job_results.html", error=f"Could not read the file: {exc}"
            ),
            400,
        )
    except Exception as exc:  # pragma: no cover - unexpected parser failure
        return (
            render_template(
                "job_results.html", error=f"Unexpected error while parsing: {exc}"
            ),
            500,
        )

    # Persist the resume too, so the scrape run can link back to a resume_id
    # (and the parsed JSON is downloadable to pair with the jobs JSON later).
    resume_id = None
    db_error = None
    try:
        resume_id = db.save_parsed_resume(parsed)
    except Exception as exc:
        db_error = str(exc)

    queries = build_queries_from_resume(parsed)
    if not queries:
        return render_template(
            "job_results.html",
            error="Couldn't find any job titles or skills in this resume to "
            "search on. Try a resume with a clear work-experience section.",
            filename=file.filename,
            resume_id=resume_id,
        )

    result = scrape_jobs_for_queries(queries)
    jobs = result["jobs"]
    search_terms = [q["search_term"] for q in queries]
    location = queries[0].get("location")

    search_id = None
    try:
        search_id = db.save_job_search(
            resume_id, search_terms, location, result["sites"], jobs, result["source"]
        )
    except Exception as exc:
        db_error = f"{db_error}; {exc}" if db_error else str(exc)

    # Drop a JSON file pairing the search context with the postings, ready to be
    # fed to the AI matcher alongside the resume JSON.
    json_filename = None
    try:
        payload = build_payload(
            jobs=jobs,
            queries=queries,
            source=result["source"],
            sites=result["sites"],
            resume_id=resume_id,
            search_id=search_id,
        )
        path = write_results_json(search_id or resume_id or "latest", payload)
        json_filename = os.path.basename(path)
    except Exception as exc:  # non-fatal: the DB copy is the canonical one
        result.setdefault("errors", []).append(f"JSON file not written: {exc}")

    return render_template(
        "job_results.html",
        jobs=jobs,
        job_count=len(jobs),
        search_terms=search_terms,
        location=location,
        source=result["source"],
        sites=result["sites"],
        errors=result.get("errors") or [],
        filename=file.filename,
        resume_id=resume_id,
        search_id=search_id,
        json_filename=json_filename,
        db_error=db_error,
        contact_name=(parsed.get("contact_info") or {}).get("name"),
    )


@app.route("/jobs/<int:search_id>")
def jobs_view(search_id: int):
    """Re-render the postings from a previously stored job search."""
    search = db.get_job_search(search_id)
    if search is None:
        abort(404, description="No job search found with that id.")
    jobs = db.get_jobs_for_search(search_id)
    terms = [t.strip() for t in (search.get("search_terms") or "").split(",") if t.strip()]
    return render_template(
        "job_results.html",
        jobs=jobs,
        job_count=len(jobs),
        search_terms=terms,
        location=search.get("location"),
        source=search.get("source"),
        sites=(search.get("sites_searched") or "").split(","),
        errors=[],
        resume_id=search.get("resume_id"),
        search_id=search_id,
        json_filename=None,
        db_error=None,
        contact_name=search.get("username"),
        stored_view=True,
    )


def _job_search_payload(search_id: int):
    """Build the downloadable JSON for a stored search, or None if not found."""
    search = db.get_job_search(search_id)
    if search is None:
        return None
    jobs = db.get_jobs_for_search(search_id)
    terms = [t.strip() for t in (search.get("search_terms") or "").split(",") if t.strip()]
    queries = [{"search_term": t, "location": search.get("location")} for t in terms]
    return build_payload(
        jobs=jobs,
        queries=queries,
        source=search.get("source") or "jobspy",
        sites=(search.get("sites_searched") or "").split(","),
        resume_id=search.get("resume_id"),
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


@app.route("/api/jobs/<int:search_id>")
def api_jobs(search_id: int):
    payload = _job_search_payload(search_id)
    if payload is None:
        abort(404, description="No job search found with that id.")
    return jsonify(payload)


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
