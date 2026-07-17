"""
Career Nexus admin portal — a separate Flask app served on its own port.

Runs from the same image/package as the user app but is entirely separate:
its own login (default **admin / admin**, bootstrapped on first start), its own
session cookie, and admin-only routes. From here an admin can:

* see how many users are registered (plus resume/search counts),
* browse and search all accounts by name/email, and promote/demote admins,
* configure the AI (Ollama) connection and the email/SMTP settings that the
  user app + worker read at runtime.

Served in Docker by a dedicated ``admin`` service:
``gunicorn webapp.admin_app:admin_app --bind 0.0.0.0:8001``. Locally:
``python -m webapp.admin_app``.

Security note: the default admin/admin credential is a convenience for the
demo — change it (promote a real account and remove/aside the default) before
exposing the portal anywhere untrusted, and keep the port firewalled.
"""

from __future__ import annotations

import json
import os
from functools import wraps

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)

from ai_client import (
    CATALOG,
    OPTION_SPECS,
    SLOTS,
    AIClientError,
    chat,
    list_models,
    load_settings,
    normalize_base_url,
    pull_model,
    resources,
    save_settings,
    test_connection,
)
from ai_client import catalog as catalog_mod
from ai_client.settings import LOCAL_OLLAMA_URL, is_configured, settings_path

from . import auth, configure_logging, db

configure_logging()

ADMIN_SESSION_KEY = "admin_user_id"

# Default bootstrap admin (created only if no admin exists yet).
DEFAULT_ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
DEFAULT_ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@careernexus.local")

# Email settings the admin can edit (app_settings key -> form field).
EMAIL_KEYS = [
    "smtp_host", "smtp_port", "smtp_username", "smtp_password",
    "smtp_from", "smtp_use_tls", "app_base_url",
]

# SMS (Twilio) settings — the service side of per-user SMS alerts. Users add
# their own numbers on their account page; nothing sends until these are set.
SMS_KEYS = ["twilio_account_sid", "twilio_auth_token", "twilio_from_number"]

# Job-search depth presets users can pick per search; the admin sets which one
# is pre-selected (and used by retries/scheduled alert searches).
DEPTH_CHOICES = [
    {"key": "quick", "label": "Quick", "blurb": "~15 postings per term, last 7 days"},
    {"key": "standard", "label": "Standard", "blurb": "~50 postings per term, last 2 weeks"},
    {"key": "deep", "label": "Deep", "blurb": "~100 postings per term, up to 30 days"},
]

admin_app = Flask(__name__)
admin_app.secret_key = os.environ.get(
    "ADMIN_SECRET_KEY", "careernexus-admin-demo-secret"
)
# Distinct cookie name so the admin session doesn't collide with the user app's
# session (browser cookies aren't port-specific on the same host).
admin_app.config["SESSION_COOKIE_NAME"] = "cn_admin_session"

_bootstrapped = False


def _bootstrap_admin() -> None:
    """Create the default admin once, best-effort (needs the DB to be up)."""
    global _bootstrapped
    if _bootstrapped:
        return
    try:
        db.ensure_default_admin(
            DEFAULT_ADMIN_USERNAME,
            DEFAULT_ADMIN_EMAIL,
            auth.hash_password(DEFAULT_ADMIN_PASSWORD),
        )
        _bootstrapped = True
    except Exception:
        pass  # DB not ready yet — try again on the next request


@admin_app.before_request
def _ensure_admin_exists():
    _bootstrap_admin()


def current_admin():
    """The logged-in admin account (still holding admin rights), or None."""
    if "_admin" in g:
        return g._admin
    admin = None
    uid = session.get(ADMIN_SESSION_KEY)
    if uid is not None:
        try:
            user = db.get_user_by_id(uid)
            if user and user.get("is_admin"):
                admin = user
        except Exception:
            admin = None
        if admin is None:
            session.pop(ADMIN_SESSION_KEY, None)
    g._admin = admin
    return admin


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_admin() is None:
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


@admin_app.context_processor
def inject_admin():
    return {"current_admin": current_admin()}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@admin_app.route("/login", methods=["GET", "POST"])
def login():
    if current_admin() is not None:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        identifier = (request.form.get("identifier") or "").strip()
        password = request.form.get("password") or ""
        throttle_id = "admin:" + identifier.lower()
        try:
            if db.throttle_status(throttle_id):
                flash("Too many failed attempts. Try again later.", "error")
                return render_template("admin_login.html", identifier=identifier), 429
        except Exception:
            pass

        try:
            admin = db.get_admin_by_login(identifier)
        except Exception as exc:
            flash(f"Could not reach the database: {exc}", "error")
            return render_template("admin_login.html", identifier=identifier), 503

        if admin and auth.verify_password(admin["password_hash"], password):
            try:
                db.clear_login_failures(throttle_id)
            except Exception:
                pass
            session.clear()
            session[ADMIN_SESSION_KEY] = admin["id"]
            g.pop("_admin", None)
            return redirect(url_for("dashboard"))

        try:
            db.record_login_failure(throttle_id, auth.MAX_LOGIN_FAILS, auth.LOGIN_LOCK_SECONDS)
        except Exception:
            pass
        flash("Incorrect admin username/email or password.", "error")
        return render_template("admin_login.html", identifier=identifier), 401

    return render_template("admin_login.html", identifier="")


@admin_app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    g.pop("_admin", None)
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@admin_app.route("/")
@admin_required
def dashboard():
    stats, db_error = {}, None
    try:
        stats = {
            "users": db.count_users(),
            "admins": db.count_admins(),
            "resumes": len(db.list_resumes(limit=100000)),
            "searches": len(db.list_job_searches(limit=100000)),
        }
    except Exception as exc:
        db_error = str(exc)
    return render_template("admin_dashboard.html", stats=stats, db_error=db_error)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
@admin_app.route("/users")
@admin_required
def users():
    q = (request.args.get("q") or "").strip()
    rows, db_error = [], None
    try:
        rows = db.list_all_users(search=q or None)
    except Exception as exc:
        db_error = str(exc)
    return render_template("admin_users.html", users=rows, q=q, db_error=db_error)


@admin_app.route("/users/<int:user_id>/admin", methods=["POST"])
@admin_required
def set_admin(user_id: int):
    make_admin = request.form.get("is_admin") == "1"
    try:
        if not make_admin:
            # Don't allow removing the last admin (would lock everyone out).
            if db.count_admins() <= 1 and db.is_user_admin(user_id):
                flash("You can't remove the last remaining admin.", "error")
                return redirect(url_for("users", q=request.args.get("q") or ""))
        db.set_user_admin(user_id, make_admin)
        flash("Admin rights " + ("granted." if make_admin else "revoked."), "info")
    except Exception as exc:
        flash(f"Could not update that account: {exc}", "error")
    return redirect(url_for("users", q=request.args.get("q") or ""))


# ---------------------------------------------------------------------------
# Settings (tiered AI models + email)
# ---------------------------------------------------------------------------
@admin_app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        section = request.form.get("section")
        if section == "ai":
            return _save_ai_settings()
        if section == "email":
            return _save_email_settings()
        if section == "sms":
            return _save_sms_settings()
        if section == "search":
            return _save_search_settings()
        flash("Unknown settings section.", "error")
        return redirect(url_for("settings"))

    try:
        email_settings = {k: (db.get_app_setting(k) or "") for k in EMAIL_KEYS}
    except Exception:
        email_settings = {k: "" for k in EMAIL_KEYS}
    email_settings.setdefault("app_base_url", "")
    try:
        sms_settings = {k: (db.get_app_setting(k) or "") for k in SMS_KEYS}
    except Exception:
        sms_settings = {k: "" for k in SMS_KEYS}
    try:
        depth_default = (db.get_app_setting("search_depth_default") or "").strip()
    except Exception:
        depth_default = ""
    if depth_default not in {d["key"] for d in DEPTH_CHOICES}:
        depth_default = "standard"

    # Local catalog: annotate with detected RAM fit + which are installed.
    res = resources()
    installed = set(list_models(LOCAL_OLLAMA_URL))
    catalog = []
    for entry in CATALOG:
        catalog.append({
            **entry,
            "installed": entry["model"] in installed
            or entry["model"].split(":")[0] in {m.split(":")[0] for m in installed},
            "fits": catalog_mod.fits(entry["model"], res.get("ram_available_gb")),
        })

    return render_template(
        "admin_settings.html",
        ai=load_settings(),
        slots=SLOTS,
        option_specs=OPTION_SPECS,
        settings_file=settings_path(),
        email=email_settings,
        sms=sms_settings,
        env_smtp_host=os.environ.get("SMTP_HOST", ""),
        depth_choices=DEPTH_CHOICES,
        depth_default=depth_default,
        catalog=catalog,
        res=res,
        local_url=LOCAL_OLLAMA_URL,
    )


def _save_search_settings():
    depth = (request.form.get("search_depth_default") or "").strip().lower()
    if depth not in {d["key"] for d in DEPTH_CHOICES}:
        flash("Unknown search depth.", "error")
        return redirect(url_for("settings"))
    try:
        db.set_app_settings({"search_depth_default": depth})
        flash("Search settings saved.", "info")
    except Exception as exc:
        flash(f"Could not save search settings: {exc}", "error")
    return redirect(url_for("settings"))


def _save_sms_settings():
    values = {}
    for key in SMS_KEYS:
        raw = (request.form.get(key) or "").strip()
        values[key] = raw or None  # empty clears the override (falls back to env)
    try:
        db.set_app_settings(values)
        flash("SMS settings saved.", "info")
    except Exception as exc:
        flash(f"Could not save SMS settings: {exc}", "error")
    return redirect(url_for("settings"))


def _save_ai_settings():
    config = load_settings()  # keep current timeouts
    slots, options = {}, {}
    for name in SLOTS:
        slots[name] = {
            "enabled": bool(request.form.get(f"slot_{name}_enabled")),
            "base_url": (request.form.get(f"slot_{name}_base_url") or "").strip(),
            "model": (request.form.get(f"slot_{name}_model") or "").strip(),
            "use_local": bool(request.form.get(f"slot_{name}_use_local")),
        }
        opt = {}
        for spec in OPTION_SPECS:
            key = spec["key"]
            entry = {"on": bool(request.form.get(f"opt_{name}_{key}_on"))}
            if spec["kind"] == "value":
                entry["value"] = request.form.get(
                    f"opt_{name}_{key}_value", spec["default_value"]
                )
            opt[key] = entry
        options[name] = opt

    save_settings({
        "slots": slots,
        "options": options,
        "connect_timeout": config.get("connect_timeout", 4),
        "read_timeout": config.get("read_timeout", 180),
    })
    flash("AI model settings saved.", "info")
    return redirect(url_for("settings"))


def _save_email_settings():
    values = {}
    for key in EMAIL_KEYS:
        raw = (request.form.get(key) or "").strip()
        values[key] = raw or None  # empty clears the override (falls back to env)
    values["smtp_use_tls"] = "1" if request.form.get("smtp_use_tls") else "0"
    try:
        db.set_app_settings(values)
        flash("Email settings saved.", "info")
    except Exception as exc:
        flash(f"Could not save email settings: {exc}", "error")
    return redirect(url_for("settings"))


@admin_app.route("/api/ai/test", methods=["POST"])
@admin_required
def api_ai_test():
    """Probe an Ollama server (or the local engine) and list its models."""
    data = request.get_json(silent=True) or {}
    if data.get("local"):
        base = LOCAL_OLLAMA_URL
    else:
        base = normalize_base_url(data.get("base_url") or "")
    result = test_connection(base)
    result["normalized_url"] = base
    return jsonify(result)


# ---------------------------------------------------------------------------
# Local model management (the bundled Ollama engine)
# ---------------------------------------------------------------------------
@admin_app.route("/api/models/installed")
@admin_required
def models_installed():
    return jsonify({"models": list_models(LOCAL_OLLAMA_URL)})


@admin_app.route("/api/models/pull", methods=["POST"])
@admin_required
def models_pull():
    """Stream an Ollama model download as NDJSON (status/total/completed lines)."""
    data = request.get_json(silent=True) or {}
    model = (data.get("model") or "").strip()
    if not model:
        return jsonify({"error": "No model specified."}), 400

    @stream_with_context
    def generate():
        try:
            for chunk in pull_model(LOCAL_OLLAMA_URL, model):
                yield json.dumps(chunk) + "\n"
        except AIClientError as exc:
            yield json.dumps({"error": str(exc)}) + "\n"

    return Response(generate(), mimetype="application/x-ndjson")


@admin_app.route("/api/models/load", methods=["POST"])
@admin_required
def models_load():
    """Warm a local model into memory (a tiny request with a long keep-alive)."""
    data = request.get_json(silent=True) or {}
    model = (data.get("model") or "").strip()
    if not model:
        return jsonify({"ok": False, "error": "No model specified."}), 400
    try:
        chat(
            LOCAL_OLLAMA_URL, model,
            [{"role": "user", "content": "ok"}],
            think=False, keep_alive="30m",
            options={"num_predict": 1}, read_timeout=600.0,
        )
        return jsonify({"ok": True})
    except AIClientError as exc:
        return jsonify({"ok": False, "error": str(exc)})


@admin_app.route("/health")
def health():
    try:
        conn = db.get_connection()
        conn.close()
        return jsonify(status="ok"), 200
    except Exception as exc:
        return jsonify(status="degraded", detail=str(exc)), 503


if __name__ == "__main__":
    db.wait_for_db()
    _bootstrap_admin()
    admin_app.run(host="0.0.0.0", port=int(os.environ.get("ADMIN_PORT", "8001")), debug=True)
