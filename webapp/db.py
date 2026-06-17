"""
Database layer for the Career Nexus web UI.

Wraps a small set of operations against the MariaDB instance defined in
``docker-compose.yml`` / ``init.sql``:

* wait for the DB to become reachable (the web container may start first),
* persist a ``ParsedResume`` (as a plain dict) across the relational schema,
* list previously parsed resumes and fetch a single stored JSON payload.

Everything connects as the limited ``career_app_user`` (DML only); all table
creation lives in ``init.sql`` and runs once when the DB volume is initialised.
The save path is defensive: the full parsed JSON is always written to
``user_resumes.parsed_data`` (nothing is ever lost), and the normalised
skills/experience/education tables are populated best-effort on top of that.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Optional

import pymysql
from pymysql.cursors import DictCursor


def _config() -> dict[str, Any]:
    return {
        "host": os.environ.get("DB_HOST", "db"),
        "port": int(os.environ.get("DB_PORT", "3306")),
        "user": os.environ.get("DB_USER", "career_app_user"),
        "password": os.environ.get("DB_PASSWORD", "dbSecur3d"),
        "database": os.environ.get("DB_NAME", "careernexus_db"),
    }


def get_connection() -> pymysql.connections.Connection:
    """Open a new connection with sensible defaults (DictCursor, autocommit off)."""
    cfg = _config()
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
    )


def wait_for_db(retries: int = 30, delay: float = 2.0) -> None:
    """Block until the DB accepts connections, or raise after ``retries`` attempts.

    The DB container is healthchecked in compose, but a manual ``docker run`` or
    a slow init can still leave the web app racing it, so we retry here too.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            conn = get_connection()
            conn.close()
            return
        except Exception as exc:  # pragma: no cover - timing dependent
            last_exc = exc
            print(f"[db] not ready (attempt {attempt}/{retries}): {exc}", flush=True)
            time.sleep(delay)
    raise RuntimeError(f"Database never became available: {last_exc}")


def _get_or_create_user(cur, name: Optional[str], email: Optional[str]) -> int:
    """Find a user by email, or create one synthesised from the resume contact.

    The schema requires a ``user_id`` for every resume and enforces unique,
    non-null username/email, so resumes without contact details get a generated
    placeholder identity rather than being rejected.
    """
    if not email:
        email = f"anon-{uuid.uuid4().hex[:12]}@careernexus.local"
    email = email.strip()[:255]

    cur.execute("SELECT id FROM users WHERE email = %s", (email,))
    row = cur.fetchone()
    if row:
        return row["id"]

    base_username = ((name or email.split("@")[0]).strip() or "user")[:45]
    username = base_username
    for _ in range(20):
        try:
            cur.execute(
                "INSERT INTO users (username, email, password_hash) VALUES (%s, %s, %s)",
                (username, email, "imported"),
            )
            return cur.lastrowid
        except pymysql.IntegrityError:
            # username collision (email is already known-unique here) -> uniquify
            username = f"{base_username[:40]}-{uuid.uuid4().hex[:4]}"
    raise RuntimeError("could not create a unique user for this resume")


def _skill_id(cur, name: str) -> Optional[int]:
    name = name.strip()[:100]
    if not name:
        return None
    cur.execute("INSERT IGNORE INTO skills (skill_name) VALUES (%s)", (name,))
    cur.execute("SELECT id FROM skills WHERE skill_name = %s", (name,))
    row = cur.fetchone()
    return row["id"] if row else None


def save_parsed_resume(parsed: dict[str, Any]) -> int:
    """Persist a parsed resume across the relational schema; return its row id.

    The complete parsed document is stored as JSON in ``user_resumes`` (the
    canonical, lossless record). Skills and the experience/education entries are
    additionally normalised into their own tables so the data is queryable in
    DBeaver without parsing JSON.
    """
    contact = parsed.get("contact_info") or {}
    name = contact.get("name")
    email = contact.get("email")

    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            user_id = _get_or_create_user(cur, name, email)

            cur.execute(
                "INSERT INTO user_resumes (user_id, parsed_data) VALUES (%s, %s)",
                (user_id, json.dumps(parsed)),
            )
            resume_id = cur.lastrowid

            # Skills -> skills + user_skills (deduped via UNIQUE + INSERT IGNORE).
            for skill in (parsed.get("skills") or {}).get("raw") or []:
                sid = _skill_id(cur, skill)
                if sid is not None:
                    cur.execute(
                        "INSERT IGNORE INTO user_skills (user_id, skill_id) VALUES (%s, %s)",
                        (user_id, sid),
                    )

            # Experience -> resume_experience.
            for exp in parsed.get("experience") or []:
                dates = exp.get("dates") or {}
                cur.execute(
                    """
                    INSERT INTO resume_experience
                        (resume_id, company, title, location,
                         start_date, end_date, is_current, description)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        resume_id,
                        exp.get("company"),
                        exp.get("title"),
                        exp.get("location"),
                        dates.get("start_date"),
                        dates.get("end_date"),
                        bool(dates.get("is_current")),
                        "\n".join(exp.get("description") or []),
                    ),
                )

            # Education -> resume_education.
            for edu in parsed.get("education") or []:
                dates = edu.get("dates") or {}
                cur.execute(
                    """
                    INSERT INTO resume_education
                        (resume_id, institution, degree, field_of_study,
                         start_date, end_date, is_current, gpa)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        resume_id,
                        edu.get("institution"),
                        edu.get("degree"),
                        edu.get("field_of_study"),
                        dates.get("start_date"),
                        dates.get("end_date"),
                        bool(dates.get("is_current")),
                        edu.get("gpa"),
                    ),
                )

        conn.commit()
        return resume_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_resumes(limit: int = 100) -> list[dict[str, Any]]:
    """Return recent resumes with the uploader's name/email for the index page."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.id, r.upload_date, u.username, u.email
                FROM user_resumes r
                JOIN users u ON u.id = r.user_id
                ORDER BY r.id DESC
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def get_resume_json(resume_id: int) -> Optional[dict[str, Any]]:
    """Fetch the stored parsed JSON for a resume, or None if it doesn't exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT parsed_data FROM user_resumes WHERE id = %s", (resume_id,)
            )
            row = cur.fetchone()
            if not row or row["parsed_data"] is None:
                return None
            data = row["parsed_data"]
            if isinstance(data, (str, bytes, bytearray)):
                return json.loads(data)
            return data  # some drivers return JSON columns pre-decoded
    finally:
        conn.close()
