"""
Pluggable email backend.

Runs with zero configuration: by default every message is written to the
application log (so password-reset links are visible in ``docker compose logs``
during development). Set the ``SMTP_HOST`` env var and it sends for real over
SMTP instead. Either way, :func:`send_email` never raises into the request path —
a delivery failure is logged and reported via the return value.

Env vars
--------
SMTP_HOST            enable real SMTP delivery (unset → log backend)
SMTP_PORT            default 587
SMTP_USERNAME        optional (login only attempted when set)
SMTP_PASSWORD        optional
SMTP_USE_TLS         "1" (default) uses STARTTLS
SMTP_FROM            From: address (default no-reply@careernexus.local)
APP_BASE_URL         absolute base for links in emails (e.g. http://localhost:8000)
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Optional

from . import db

log = logging.getLogger("careernexus.email")


def _setting(db_key: str, env_key: str, default: Optional[str] = None) -> Optional[str]:
    """An email setting: admin-set value (DB) first, then env var, then default.

    DB access is best-effort — :func:`db.get_app_setting` returns None if the
    table is missing or the DB is unreachable, so config always falls back to
    environment variables.
    """
    value = db.get_app_setting(db_key)
    if value is not None and str(value).strip() != "":
        return value
    return os.environ.get(env_key, default)


def _from_addr() -> str:
    return _setting("smtp_from", "SMTP_FROM", "no-reply@careernexus.local")


def base_url() -> str:
    """Absolute base URL for links embedded in emails."""
    return (_setting("app_base_url", "APP_BASE_URL", "http://localhost:8000")
            or "http://localhost:8000").rstrip("/")


def send_email(to: str, subject: str, body: str) -> bool:
    """Send (or log) a plain-text email. Returns True on success.

    With no SMTP host configured (neither admin setting nor ``SMTP_HOST`` env)
    this logs the message and returns True, so the reset/alert flows work
    end-to-end in the demo without a mail server.
    """
    host = _setting("smtp_host", "SMTP_HOST")
    if not host:
        log.info(
            "[email:log-backend] To: %s | Subject: %s\n%s", to, subject, body
        )
        print(
            f"\n----- EMAIL (log backend) -----\nTo: {to}\nSubject: {subject}\n\n"
            f"{body}\n-------------------------------\n",
            flush=True,
        )
        return True

    msg = EmailMessage()
    msg["From"] = _from_addr()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        port = int(_setting("smtp_port", "SMTP_PORT", "587") or "587")
    except ValueError:
        port = 587
    use_tls = (_setting("smtp_use_tls", "SMTP_USE_TLS", "1") or "1") != "0"
    username = _setting("smtp_username", "SMTP_USERNAME")
    password = _setting("smtp_password", "SMTP_PASSWORD")

    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            if use_tls:
                server.starttls()
            if username:
                server.login(username, password or "")
            server.send_message(msg)
        return True
    except Exception as exc:  # never propagate into the request
        log.warning("SMTP send to %s failed: %s", to, exc)
        return False
