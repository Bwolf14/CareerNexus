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

import hashlib
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


# ---------------------------------------------------------------------------
# Accounts / authentication
# ---------------------------------------------------------------------------
def _unique_username(cur, base: str) -> str:
    """A username derived from ``base`` that doesn't collide with an existing row."""
    base = (base.strip() or "user")[:45]
    username = base
    for _ in range(20):
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        if not cur.fetchone():
            return username
        username = f"{base[:40]}-{uuid.uuid4().hex[:4]}"
    return f"user-{uuid.uuid4().hex[:8]}"


def create_user(
    email: str,
    password_hash: str,
    consent: bool,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> int:
    """Create a registered account; return its id.

    Raises ``ValueError`` if the email is already registered. ``consent`` records
    the user's agreement to sensitive-data collection (the web layer refuses to
    call this without it). Degrades gracefully on databases whose ``users`` table
    predates the newer columns.
    """
    email = (email or "").strip()[:255]
    username_base = email.split("@")[0] if "@" in email else email
    first_name = (first_name or None) and first_name.strip()[:100]
    last_name = (last_name or None) and last_name.strip()[:100]

    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                raise ValueError("That email is already registered.")

            username = _unique_username(cur, username_base)
            try:
                cur.execute(
                    """
                    INSERT INTO users
                        (username, email, password_hash, first_name, last_name,
                         consent_data_collection, consent_at)
                    VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    """,
                    (username, email, password_hash, first_name, last_name,
                     bool(consent)),
                )
            except pymysql.err.OperationalError as exc:
                # Column doesn't exist on a DB initialised before these columns
                # were added — fall back to the base insert.
                if exc.args and exc.args[0] == 1054:
                    cur.execute(
                        "INSERT INTO users (username, email, password_hash) "
                        "VALUES (%s, %s, %s)",
                        (username, email, password_hash),
                    )
                else:
                    raise
            user_id = cur.lastrowid
        conn.commit()
        return user_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Admin: default admin bootstrap, user administration, app settings
# ---------------------------------------------------------------------------
def get_admin_by_login(identifier: str) -> Optional[dict[str, Any]]:
    """Fetch an admin account by username OR email (for the admin portal login).

    Returns id, username, email, password_hash, is_admin — only when is_admin is
    true. None otherwise.
    """
    identifier = (identifier or "").strip()[:255]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, email, password_hash, is_admin FROM users "
                "WHERE is_admin = TRUE AND (email = %s OR username = %s) LIMIT 1",
                (identifier, identifier),
            )
            return cur.fetchone()
    finally:
        conn.close()


def is_user_admin(user_id: int) -> bool:
    """True if the given account still has admin rights."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT is_admin FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return bool(row and row["is_admin"])
    finally:
        conn.close()


def ensure_default_admin(username: str, email: str, password_hash: str) -> None:
    """Create the bootstrap admin account if no admin exists yet.

    Idempotent: does nothing once any admin is present (so a promoted user or a
    changed password isn't clobbered on restart).
    """
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE is_admin = TRUE LIMIT 1")
            if cur.fetchone():
                conn.commit()
                return
            # Reuse an existing row with the same username/email if present.
            cur.execute(
                "SELECT id FROM users WHERE username = %s OR email = %s LIMIT 1",
                (username, email),
            )
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    "UPDATE users SET is_admin = TRUE WHERE id = %s", (existing["id"],)
                )
            else:
                cur.execute(
                    """
                    INSERT INTO users
                        (username, email, password_hash, is_admin,
                         consent_data_collection, consent_at)
                    VALUES (%s, %s, %s, TRUE, TRUE, CURRENT_TIMESTAMP)
                    """,
                    (username, email, password_hash),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def count_users() -> int:
    """Total number of registered accounts."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def count_admins() -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin = TRUE")
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def list_all_users(search: Optional[str] = None, limit: int = 500) -> list[dict[str, Any]]:
    """All accounts (id, name, email, admin flag, created_at), newest first.

    ``search`` filters by a case-insensitive substring of email, first/last
    name, or username.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if search:
                like = f"%{search.strip()}%"
                cur.execute(
                    """
                    SELECT id, username, email, first_name, last_name, is_admin,
                           created_at
                    FROM users
                    WHERE email LIKE %s OR username LIKE %s
                          OR first_name LIKE %s OR last_name LIKE %s
                          OR CONCAT(COALESCE(first_name,''), ' ',
                                    COALESCE(last_name,'')) LIKE %s
                    ORDER BY id DESC LIMIT %s
                    """,
                    (like, like, like, like, like, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, username, email, first_name, last_name, is_admin,
                           created_at
                    FROM users ORDER BY id DESC LIMIT %s
                    """,
                    (limit,),
                )
            return cur.fetchall()
    finally:
        conn.close()


def set_user_admin(user_id: int, is_admin: bool) -> bool:
    """Grant or revoke admin rights on an account. Returns False if not found."""
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET is_admin = %s WHERE id = %s",
                (bool(is_admin), user_id),
            )
            changed = cur.rowcount
        conn.commit()
        return changed > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_app_setting(key: str) -> Optional[str]:
    """A single admin-set application setting value, or None if unset.

    Best-effort: returns None if the DB is unreachable or the table doesn't
    exist yet (pre-migration), so callers fall back to environment defaults.
    """
    try:
        conn = get_connection()
    except Exception:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key = %s", (key,)
            )
            row = cur.fetchone()
            return row["setting_value"] if row else None
    except Exception:
        return None
    finally:
        conn.close()


def get_all_app_settings() -> dict[str, str]:
    """All admin-set settings as a dict (empty if none / DB down / table missing)."""
    try:
        conn = get_connection()
    except Exception:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT setting_key, setting_value FROM app_settings")
            return {r["setting_key"]: r["setting_value"] for r in cur.fetchall()}
    except Exception:
        return {}
    finally:
        conn.close()


def set_app_settings(values: dict[str, Any]) -> None:
    """Upsert a batch of application settings (value None deletes the key)."""
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            for key, value in values.items():
                if value is None:
                    cur.execute(
                        "DELETE FROM app_settings WHERE setting_key = %s", (key[:64],)
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO app_settings (setting_key, setting_value)
                        VALUES (%s, %s)
                        ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
                        """,
                        (key[:64], str(value)),
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_user_by_email(email: str) -> Optional[dict[str, Any]]:
    """Fetch an account by email (id, username, email, password_hash), or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, email, password_hash FROM users "
                "WHERE email = %s",
                ((email or "").strip()[:255],),
            )
            return cur.fetchone()
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> Optional[dict[str, Any]]:
    """Fetch an account by id (id, username, email, names, admin flag), or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, email, first_name, last_name, is_admin "
                "FROM users WHERE id = %s",
                (user_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def delete_resume(user_id: int, resume_id: int) -> bool:
    """Delete a resume the user owns. Returns False when it isn't theirs.

    ``resume_experience``/``resume_education`` cascade away with the row;
    ``job_searches.resume_id`` becomes NULL, so past searches survive.
    """
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_resumes WHERE id = %s AND user_id = %s",
                (resume_id, user_id),
            )
            changed = cur.rowcount
        conn.commit()
        return changed > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_resume_owner(resume_id: int) -> Optional[int]:
    """The user_id that owns a resume, or None if the resume doesn't exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM user_resumes WHERE id = %s", (resume_id,)
            )
            row = cur.fetchone()
            return row["user_id"] if row else None
    finally:
        conn.close()


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


def save_parsed_resume(
    parsed: dict[str, Any],
    user_id: Optional[int] = None,
    label: Optional[str] = None,
) -> int:
    """Persist a parsed resume across the relational schema; return its row id.

    When ``user_id`` is given (the logged-in uploader) the resume is tied to that
    account; otherwise a placeholder identity is synthesised from the resume's
    contact info (legacy/anonymous path). ``label`` is the user-chosen name shown
    everywhere the resume is referenced. The complete parsed document is stored
    as JSON in ``user_resumes`` (the canonical, lossless record). Skills and the
    experience/education entries are additionally normalised into their own
    tables so the data is queryable in DBeaver without parsing JSON.
    """
    contact = parsed.get("contact_info") or {}
    name = contact.get("name")
    email = contact.get("email")
    label = (label or None) and label.strip()[:120]

    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            if user_id is None:
                user_id = _get_or_create_user(cur, name, email)

            try:
                cur.execute(
                    "INSERT INTO user_resumes (user_id, label, parsed_data) "
                    "VALUES (%s, %s, %s)",
                    (user_id, label, json.dumps(parsed)),
                )
            except pymysql.err.OperationalError as exc:
                # label column missing on a pre-upgrade database -> base insert.
                if exc.args and exc.args[0] == 1054:
                    cur.execute(
                        "INSERT INTO user_resumes (user_id, parsed_data) "
                        "VALUES (%s, %s)",
                        (user_id, json.dumps(parsed)),
                    )
                else:
                    raise
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


def list_resumes(limit: int = 100, user_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Recent resumes (with label + uploader's name/email) for the index page.

    When ``user_id`` is given, only that account's resumes are returned (each
    signed-in user sees their own uploads, not everyone's). Falls back to a
    label-less query on pre-upgrade databases.
    """
    where = "WHERE r.user_id = %s" if user_id is not None else ""
    params: tuple = (user_id, limit) if user_id is not None else (limit,)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    f"""
                    SELECT r.id, r.label, r.upload_date, u.username, u.email
                    FROM user_resumes r
                    JOIN users u ON u.id = r.user_id
                    {where}
                    ORDER BY r.id DESC
                    LIMIT %s
                    """,
                    params,
                )
                return cur.fetchall()
            except pymysql.err.OperationalError as exc:
                if not (exc.args and exc.args[0] == 1054):
                    raise
                cur.execute(
                    f"""
                    SELECT r.id, r.upload_date, u.username, u.email
                    FROM user_resumes r
                    JOIN users u ON u.id = r.user_id
                    {where}
                    ORDER BY r.id DESC
                    LIMIT %s
                    """,
                    params,
                )
                return [{**row, "label": None} for row in cur.fetchall()]
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


# ---------------------------------------------------------------------------
# Job side — scrape runs and the postings they produced
# ---------------------------------------------------------------------------
def _external_id(job: dict[str, Any]) -> str:
    """Never-null id for the per-search UNIQUE key (board id, else url hash)."""
    ext = (job.get("external_id") or "").strip()
    if ext:
        return ext[:255]
    basis = (job.get("job_url") or job.get("title") or "") + (job.get("company") or "")
    return ("url-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16])[:255]


def save_job_search(
    resume_id: Optional[int],
    search_terms: list[str],
    location: Optional[str],
    sites: list[str],
    jobs: list[dict[str, Any]],
    source: str,
) -> int:
    """Persist a scrape run plus its postings; return the new job_searches id.

    The user_id is derived from the resume so the run links back to a person;
    both stay NULL for an anonymous/ad-hoc scrape. Each posting is written into
    ``jobs`` with this run's ``search_id`` (deduped per run via the UNIQUE key).
    """
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            user_id = None
            if resume_id is not None:
                cur.execute(
                    "SELECT user_id FROM user_resumes WHERE id = %s", (resume_id,)
                )
                row = cur.fetchone()
                if row:
                    user_id = row["user_id"]

            cur.execute(
                """
                INSERT INTO job_searches
                    (resume_id, user_id, search_terms, location,
                     sites_searched, source, results_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    resume_id,
                    user_id,
                    ", ".join(t for t in search_terms if t)[:512],
                    (location or None) and location[:255],
                    ",".join(sites)[:255],
                    source[:20],
                    len(jobs),
                ),
            )
            search_id = cur.lastrowid

            for job in jobs:
                cur.execute(
                    """
                    INSERT INTO jobs
                        (search_id, source_site, external_id, title, company,
                         location, job_type, is_remote, salary_min, salary_max,
                         salary_currency, salary_interval, description, link,
                         search_term, date_posted)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        title = VALUES(title),
                        company = VALUES(company),
                        description = VALUES(description),
                        scraped_at = CURRENT_TIMESTAMP
                    """,
                    (
                        search_id,
                        (job.get("source_site") or None),
                        _external_id(job),
                        (job.get("title") or "Untitled posting")[:255],
                        (job.get("company") or None) and job["company"][:255],
                        (job.get("location") or None) and job["location"][:255],
                        (job.get("job_type") or None) and job["job_type"][:100],
                        bool(job.get("is_remote")),
                        job.get("salary_min"),
                        job.get("salary_max"),
                        (job.get("salary_currency") or None) and job["salary_currency"][:10],
                        (job.get("salary_interval") or None) and job["salary_interval"][:20],
                        job.get("description"),
                        job.get("job_url"),
                        (job.get("search_term") or None) and job["search_term"][:255],
                        job.get("date_posted"),
                    ),
                )

        conn.commit()
        return search_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_plan_answers(
    search_id: int, resume_id: Optional[int], answers: dict[str, Any]
) -> None:
    """Store (or overwrite) the questionnaire answers for a job search.

    Raises on any DB problem — including the ``career_plans`` table not
    existing yet on databases initialised before it was added to ``init.sql``.
    Callers treat this as best-effort and fall back to the file store.
    """
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO career_plans (search_id, resume_id, answers)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    answers = VALUES(answers),
                    resume_id = VALUES(resume_id)
                """,
                (search_id, resume_id, json.dumps(answers)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_plan_answers(search_id: int) -> Optional[dict[str, Any]]:
    """Fetch stored questionnaire answers for a search, or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT answers FROM career_plans WHERE search_id = %s", (search_id,)
            )
            row = cur.fetchone()
            if not row or row["answers"] is None:
                return None
            data = row["answers"]
            if isinstance(data, (str, bytes, bytearray)):
                return json.loads(data)
            return data
    finally:
        conn.close()


def list_job_searches(limit: int = 100, user_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Recent scrape runs (with the uploader's name) for the index page.

    When ``user_id`` is given, only that account's searches are returned.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if user_id is None:
                cur.execute(
                    """
                    SELECT s.id, s.search_terms, s.location, s.sites_searched,
                           s.source, s.results_count, s.ran_at, u.username
                    FROM job_searches s
                    LEFT JOIN users u ON u.id = s.user_id
                    ORDER BY s.id DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            else:
                cur.execute(
                    """
                    SELECT s.id, s.search_terms, s.location, s.sites_searched,
                           s.source, s.results_count, s.ran_at, u.username
                    FROM job_searches s
                    LEFT JOIN users u ON u.id = s.user_id
                    WHERE s.user_id = %s
                    ORDER BY s.id DESC
                    LIMIT %s
                    """,
                    (user_id, limit),
                )
            return cur.fetchall()
    finally:
        conn.close()


def get_job_search(search_id: int) -> Optional[dict[str, Any]]:
    """Fetch one scrape run's metadata, or None if it doesn't exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.*, u.username
                FROM job_searches s
                LEFT JOIN users u ON u.id = s.user_id
                WHERE s.id = %s
                """,
                (search_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def jobs_by_company(
    user_id: int, company: str, limit: int = 25
) -> list[dict[str, Any]]:
    """Stored postings from one company across all of a user's searches.

    Powers the "more jobs at this company" section of the job-detail page —
    non-AI, instant, and scoped to the requesting user's own scrape history.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.source_site, j.external_id, j.title, j.company,
                       j.location, j.job_type, j.is_remote, j.salary_min,
                       j.salary_max, j.salary_currency, j.salary_interval,
                       j.link AS job_url, j.date_posted, j.search_id
                FROM jobs j
                JOIN job_searches s ON s.id = j.search_id
                WHERE s.user_id = %s AND j.company = %s
                ORDER BY (j.date_posted IS NULL), j.date_posted DESC, j.id DESC
                LIMIT %s
                """,
                (user_id, (company or "")[:255], limit),
            )
            return cur.fetchall()
    finally:
        conn.close()


def get_saved_job(user_id: int, saved_id: int) -> Optional[dict[str, Any]]:
    """One saved-job row, only if it belongs to the user."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM saved_jobs WHERE id = %s AND user_id = %s",
                (saved_id, user_id),
            )
            return cur.fetchone()
    finally:
        conn.close()


def get_jobs_for_search(search_id: int) -> list[dict[str, Any]]:
    """All postings stored for a scrape run, newest-board-date first."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_site, external_id, title, company, location,
                       job_type, is_remote, salary_min, salary_max,
                       salary_currency, salary_interval, description,
                       link AS job_url, search_term, date_posted
                FROM jobs
                WHERE search_id = %s
                ORDER BY (date_posted IS NULL), date_posted DESC, id ASC
                """,
                (search_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Password resets
# ---------------------------------------------------------------------------
def create_password_reset(user_id: int, token_hash: str, expires_at) -> None:
    """Store a single-use reset token hash; invalidate the user's older ones."""
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE password_resets SET used_at = CURRENT_TIMESTAMP "
                "WHERE user_id = %s AND used_at IS NULL",
                (user_id,),
            )
            cur.execute(
                "INSERT INTO password_resets (user_id, token_hash, expires_at) "
                "VALUES (%s, %s, %s)",
                (user_id, token_hash, expires_at),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_valid_reset(token_hash: str) -> Optional[dict[str, Any]]:
    """Return an unused, unexpired reset row (id, user_id), or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id FROM password_resets
                WHERE token_hash = %s AND used_at IS NULL
                      AND expires_at > CURRENT_TIMESTAMP
                """,
                (token_hash,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def reset_password(reset_id: int, user_id: int, password_hash: str) -> None:
    """Set a new password hash and mark the reset token consumed, atomically."""
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (password_hash, user_id),
            )
            cur.execute(
                "UPDATE password_resets SET used_at = CURRENT_TIMESTAMP WHERE id = %s",
                (reset_id,),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Login throttle (brute-force protection)
# ---------------------------------------------------------------------------
def throttle_status(identifier: str) -> Optional[Any]:
    """Return ``locked_until`` if the identifier is currently locked, else None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT locked_until FROM auth_throttle "
                "WHERE identifier = %s AND locked_until IS NOT NULL "
                "AND locked_until > CURRENT_TIMESTAMP",
                (identifier[:255],),
            )
            row = cur.fetchone()
            return row["locked_until"] if row else None
    finally:
        conn.close()


def record_login_failure(identifier: str, max_fails: int, lock_seconds: int) -> None:
    """Increment the failure counter; lock the identifier once it hits max_fails."""
    identifier = identifier[:255]
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auth_throttle (identifier, fail_count)
                VALUES (%s, 1)
                ON DUPLICATE KEY UPDATE fail_count = fail_count + 1
                """,
                (identifier,),
            )
            cur.execute(
                """
                UPDATE auth_throttle
                SET locked_until = CURRENT_TIMESTAMP + INTERVAL %s SECOND
                WHERE identifier = %s AND fail_count >= %s
                """,
                (lock_seconds, identifier, max_fails),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_login_failures(identifier: str) -> None:
    """Reset the throttle for an identifier after a successful login."""
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM auth_throttle WHERE identifier = %s", (identifier[:255],)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Account data export + deletion
# ---------------------------------------------------------------------------
def export_user_data(user_id: int) -> dict[str, Any]:
    """Gather everything stored for an account into one JSON-serialisable dict."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, email, consent_data_collection, consent_at, "
                "created_at FROM users WHERE id = %s",
                (user_id,),
            )
            account = cur.fetchone()

            cur.execute(
                "SELECT id, parsed_data, upload_date FROM user_resumes "
                "WHERE user_id = %s ORDER BY id",
                (user_id,),
            )
            resumes = cur.fetchall()
            for r in resumes:
                data = r.get("parsed_data")
                if isinstance(data, (str, bytes, bytearray)):
                    try:
                        r["parsed_data"] = json.loads(data)
                    except Exception:
                        pass

            cur.execute(
                "SELECT id, search_terms, location, sites_searched, source, "
                "results_count, ran_at FROM job_searches WHERE user_id = %s ORDER BY id",
                (user_id,),
            )
            searches = cur.fetchall()

            cur.execute(
                "SELECT id, title, company, location, source_site, salary_display, "
                "is_remote, job_url, status, notes, saved_at, updated_at "
                "FROM saved_jobs WHERE user_id = %s ORDER BY id",
                (user_id,),
            )
            saved = cur.fetchall()

            cur.execute(
                "SELECT id, label, params, frequency, active, last_run_at, "
                "next_run_at FROM saved_searches WHERE user_id = %s ORDER BY id",
                (user_id,),
            )
            saved_searches = cur.fetchall()
        return {
            "account": account,
            "resumes": resumes,
            "job_searches": searches,
            "saved_jobs": saved,
            "saved_searches": saved_searches,
        }
    finally:
        conn.close()


def delete_user(user_id: int) -> None:
    """Delete an account and everything belonging to it.

    Most child tables cascade from ``users``; ``job_searches`` is
    ``ON DELETE SET NULL``, so its rows are removed explicitly first (which
    cascades to ``jobs`` and ``career_plans``).
    """
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM job_searches WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Saved jobs + application tracker
# ---------------------------------------------------------------------------
SAVED_JOB_STATUSES = ("interested", "applied", "interviewing", "offer", "rejected")


def save_job(user_id: int, dedup_key: str, job: dict[str, Any]) -> int:
    """Bookmark a posting for a user (idempotent per dedup_key); return row id."""
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO saved_jobs
                    (user_id, dedup_key, title, company, location, source_site,
                     salary_display, is_remote, job_url, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)
                """,
                (
                    user_id,
                    dedup_key[:40],
                    (job.get("title") or None) and job["title"][:255],
                    (job.get("company") or None) and job["company"][:255],
                    (job.get("location") or None) and job["location"][:255],
                    (job.get("source_site") or None) and job["source_site"][:50],
                    (job.get("salary_display") or None) and job["salary_display"][:120],
                    bool(job.get("is_remote")),
                    job.get("job_url"),
                    job.get("description"),
                ),
            )
            saved_id = cur.lastrowid
        conn.commit()
        return saved_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_saved_jobs(user_id: int) -> list[dict[str, Any]]:
    """All of a user's saved jobs, newest first."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM saved_jobs WHERE user_id = %s "
                "ORDER BY updated_at DESC, id DESC",
                (user_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def saved_dedup_keys(user_id: int) -> set:
    """The set of dedup_keys a user has already saved (to flag them in matches)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT dedup_key FROM saved_jobs WHERE user_id = %s", (user_id,))
            return {row["dedup_key"] for row in cur.fetchall()}
    finally:
        conn.close()


def update_saved_job(
    user_id: int, saved_id: int, status: Optional[str], notes: Optional[str]
) -> bool:
    """Update a saved job's status and/or notes. Returns False if not owned."""
    sets, params = [], []
    if status is not None:
        if status not in SAVED_JOB_STATUSES:
            raise ValueError(f"invalid status: {status}")
        sets.append("status = %s")
        params.append(status)
    if notes is not None:
        sets.append("notes = %s")
        params.append(notes[:2000] if notes else None)
    if not sets:
        return True
    params.extend([saved_id, user_id])
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE saved_jobs SET {', '.join(sets)} "
                "WHERE id = %s AND user_id = %s",
                params,
            )
            changed = cur.rowcount
        conn.commit()
        return changed > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_saved_job(user_id: int, saved_id: int) -> bool:
    """Remove a saved job; returns False if it isn't the user's."""
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM saved_jobs WHERE id = %s AND user_id = %s",
                (saved_id, user_id),
            )
            changed = cur.rowcount
        conn.commit()
        return changed > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Background scrape queue
# ---------------------------------------------------------------------------
def enqueue_scrape(
    user_id: Optional[int],
    resume_id: int,
    params: dict[str, Any],
    saved_search_id: Optional[int] = None,
) -> int:
    """Insert a pending scrape job for the worker to pick up; return its id."""
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scrape_jobs (user_id, resume_id, saved_search_id, params)
                VALUES (%s, %s, %s, %s)
                """,
                (user_id, resume_id, saved_search_id, json.dumps(params)),
            )
            job_id = cur.lastrowid
        conn.commit()
        return job_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_scrape_job(job_id: int) -> Optional[dict[str, Any]]:
    """Fetch a queued scrape job's row (params decoded), or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM scrape_jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
            if row and isinstance(row.get("params"), (str, bytes, bytearray)):
                try:
                    row["params"] = json.loads(row["params"])
                except Exception:
                    row["params"] = {}
            return row
    finally:
        conn.close()


def claim_next_scrape() -> Optional[dict[str, Any]]:
    """Atomically move the oldest pending scrape to 'running' and return it.

    Uses ``UPDATE ... LIMIT 1`` guarded by ``status='pending'`` so two worker
    loops never grab the same job. Returns None when the queue is empty.
    """
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE scrape_jobs
                SET status = 'running', started_at = CURRENT_TIMESTAMP
                WHERE id = (
                    SELECT id FROM (
                        SELECT id FROM scrape_jobs WHERE status = 'pending'
                        ORDER BY id LIMIT 1
                    ) AS next
                )
                """
            )
            if cur.rowcount == 0:
                conn.commit()
                return None
            cur.execute(
                "SELECT * FROM scrape_jobs WHERE status = 'running' "
                "ORDER BY started_at DESC, id DESC LIMIT 1"
            )
            row = cur.fetchone()
        conn.commit()
        if row and isinstance(row.get("params"), (str, bytes, bytearray)):
            try:
                row["params"] = json.loads(row["params"])
            except Exception:
                row["params"] = {}
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_scrape(
    job_id: int, search_id: Optional[int], error: Optional[str] = None
) -> None:
    """Mark a scrape job done (with its search_id) or errored."""
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE scrape_jobs
                SET status = %s, search_id = %s, error = %s,
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                ("error" if error else "done", search_id, error, job_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Saved searches + scheduled alerts
# ---------------------------------------------------------------------------
def create_saved_search(
    user_id: int,
    resume_id: int,
    label: Optional[str],
    params: dict[str, Any],
    frequency: str,
    next_run_at,
) -> int:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO saved_searches
                    (user_id, resume_id, label, params, frequency, next_run_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (user_id, resume_id, (label or None) and label[:255],
                 json.dumps(params), frequency, next_run_at),
            )
            sid = cur.lastrowid
        conn.commit()
        return sid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_saved_searches(user_id: int) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM saved_searches WHERE user_id = %s ORDER BY id DESC",
                (user_id,),
            )
            rows = cur.fetchall()
            for row in rows:
                if isinstance(row.get("params"), (str, bytes, bytearray)):
                    try:
                        row["params"] = json.loads(row["params"])
                    except Exception:
                        row["params"] = {}
            return rows
    finally:
        conn.close()


def delete_saved_search(user_id: int, saved_search_id: int) -> bool:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM saved_searches WHERE id = %s AND user_id = %s",
                (saved_search_id, user_id),
            )
            changed = cur.rowcount
        conn.commit()
        return changed > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_saved_search_active(user_id: int, saved_search_id: int, active: bool) -> bool:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE saved_searches SET active = %s WHERE id = %s AND user_id = %s",
                (bool(active), saved_search_id, user_id),
            )
            changed = cur.rowcount
        conn.commit()
        return changed > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def due_saved_searches(now) -> list[dict[str, Any]]:
    """Active saved searches whose next_run_at has passed (params decoded)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM saved_searches
                WHERE active = TRUE
                      AND (next_run_at IS NULL OR next_run_at <= %s)
                ORDER BY id
                """,
                (now,),
            )
            rows = cur.fetchall()
            for row in rows:
                if isinstance(row.get("params"), (str, bytes, bytearray)):
                    try:
                        row["params"] = json.loads(row["params"])
                    except Exception:
                        row["params"] = {}
            return rows
    finally:
        conn.close()


def get_saved_search(saved_search_id: int) -> Optional[dict[str, Any]]:
    """Fetch one saved search (params decoded), or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM saved_searches WHERE id = %s", (saved_search_id,)
            )
            row = cur.fetchone()
            if row and isinstance(row.get("params"), (str, bytes, bytearray)):
                try:
                    row["params"] = json.loads(row["params"])
                except Exception:
                    row["params"] = {}
            return row
    finally:
        conn.close()


def bump_saved_search_next_run(saved_search_id: int, next_run_at) -> None:
    """Set only next_run_at (used when the scheduler enqueues a run) so the same
    saved search isn't enqueued again before this run finishes."""
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE saved_searches SET next_run_at = %s WHERE id = %s",
                (next_run_at, saved_search_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_saved_search_result(saved_search_id: int, search_id: Optional[int]) -> None:
    """Record the latest completed run (last_run_at + last_search_id)."""
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE saved_searches SET last_run_at = CURRENT_TIMESTAMP, "
                "last_search_id = %s WHERE id = %s",
                (search_id, saved_search_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_saved_search_ran(saved_search_id: int, search_id: Optional[int], next_run_at) -> None:
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE saved_searches
                SET last_run_at = CURRENT_TIMESTAMP, last_search_id = %s,
                    next_run_at = %s
                WHERE id = %s
                """,
                (search_id, next_run_at, saved_search_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_user_email(user_id: int) -> Optional[str]:
    """Just the email for an account (used by the alert emailer)."""
    row = get_user_by_id(user_id)
    return row["email"] if row else None


# ---------------------------------------------------------------------------
# Per-user settings (notifications + bring-your-own cloud AI)
# ---------------------------------------------------------------------------
_USER_SETTINGS_FIELDS = {
    "phone_number", "discord_webhook",
    "ai_cloud_enabled", "ai_provider", "ai_api_key", "ai_model",
}


def get_user_settings(user_id: int) -> dict[str, Any]:
    """A user's settings row as a dict — {} when unset, DB down, or table missing."""
    try:
        conn = get_connection()
    except Exception:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM user_settings WHERE user_id = %s", (user_id,)
            )
            return cur.fetchone() or {}
    except Exception:
        return {}
    finally:
        conn.close()


def upsert_user_settings(user_id: int, **fields: Any) -> None:
    """Insert-or-update a user's settings (only whitelisted columns)."""
    clean = {k: v for k, v in fields.items() if k in _USER_SETTINGS_FIELDS}
    if not clean:
        return
    cols = ", ".join(clean)
    placeholders = ", ".join(["%s"] * len(clean))
    updates = ", ".join(f"{k} = VALUES({k})" for k in clean)
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO user_settings (user_id, {cols}) "
                f"VALUES (%s, {placeholders}) "
                f"ON DUPLICATE KEY UPDATE {updates}",
                (user_id, *clean.values()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
