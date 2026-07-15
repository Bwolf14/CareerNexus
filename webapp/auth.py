"""
Authentication helpers for the Career Nexus web UI.

Small, session-based auth: a signed Flask session cookie holds the logged-in
``user_id``; the account row is loaded once per request and cached on
``flask.g``. Passwords are hashed with Werkzeug (PBKDF2 by default).

The whole guided flow is gated — :func:`login_required` redirects anonymous
visitors to the login page. Registration additionally requires explicit consent
to sensitive-data collection (enforced in the route, recorded on the account).
"""

from __future__ import annotations

import hashlib
import secrets
from functools import wraps
from typing import Any, Callable, Optional

from flask import flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db

SESSION_KEY = "user_id"

# Password-reset tokens are valid for this long.
RESET_TTL_SECONDS = 60 * 60  # 1 hour
# Login throttle: lock an identifier for LOGIN_LOCK_SECONDS after MAX_LOGIN_FAILS.
MAX_LOGIN_FAILS = 8
LOGIN_LOCK_SECONDS = 15 * 60  # 15 minutes


def new_reset_token() -> tuple[str, str]:
    """Return (raw_token, token_hash). Only the hash is ever stored."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    """SHA-256 of a reset token — what goes in the database."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return check_password_hash(password_hash, password)
    except Exception:
        return False


def login_user(user_id: int) -> None:
    """Start a session for ``user_id`` (rotating the session id)."""
    session.clear()
    session[SESSION_KEY] = user_id
    g.pop("_current_user", None)


def logout_user() -> None:
    session.clear()
    g.pop("_current_user", None)


def current_user() -> Optional[dict[str, Any]]:
    """The logged-in account (id, username, email), or None. Cached per request."""
    if "_current_user" in g:
        return g._current_user
    user = None
    uid = session.get(SESSION_KEY)
    if uid is not None:
        try:
            user = db.get_user_by_id(uid)
        except Exception:
            user = None
        if user is None:
            # Stale/invalid session (e.g. account removed) — drop it.
            session.pop(SESSION_KEY, None)
    g._current_user = user
    return user


def login_required(view: Callable) -> Callable:
    """Redirect anonymous requests to the login page, preserving the target."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("Please log in to continue.", "info")
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped
