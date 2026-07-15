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

log = logging.getLogger("careernexus.email")


def _from_addr() -> str:
    return os.environ.get("SMTP_FROM", "no-reply@careernexus.local")


def base_url() -> str:
    """Absolute base URL for links embedded in emails."""
    return os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")


def send_email(to: str, subject: str, body: str) -> bool:
    """Send (or log) a plain-text email. Returns True on success.

    With no ``SMTP_HOST`` configured this logs the message and returns True, so
    the reset/alert flows work end-to-end in the demo without a mail server.
    """
    host = os.environ.get("SMTP_HOST")
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

    port = int(os.environ.get("SMTP_PORT", "587"))
    use_tls = os.environ.get("SMTP_USE_TLS", "1") != "0"
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")

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
