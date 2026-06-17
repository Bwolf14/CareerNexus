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

from resume_parser import parse_resume
from resume_parser.exceptions import ResumeParserError

from . import db

ALLOWED_EXTENSIONS = {".pdf", ".docx"}
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB upload cap

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


def _allowed(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS


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
