"""
Outbound notifications beyond email: SMS (Twilio) and Discord webhooks.

* **SMS** — the service credentials (Twilio account SID / auth token / from
  number) are configured by an admin in the admin portal (app_settings, with
  TWILIO_* env vars as fallback). Each user adds their own phone number on
  their account page; no number → no SMS.
* **Discord** — entirely per-user: they paste a channel webhook URL on their
  account page and alerts get posted there.

Both senders are best-effort and never raise into the caller — a failed
notification is logged and reported via the return value, and email remains
the always-on channel.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import requests

from . import db

log = logging.getLogger("careernexus.notify")

_TIMEOUT = (3.0, 10.0)
_DISCORD_RE = re.compile(r"^https://(discord\.com|discordapp\.com)/api/webhooks/", re.I)


def _setting(db_key: str, env_key: str) -> Optional[str]:
    value = db.get_app_setting(db_key)
    if value is not None and str(value).strip():
        return str(value).strip()
    return os.environ.get(env_key) or None


def sms_config() -> Optional[dict[str, str]]:
    """Twilio credentials from admin settings (env fallback), or None."""
    sid = _setting("twilio_account_sid", "TWILIO_ACCOUNT_SID")
    token = _setting("twilio_auth_token", "TWILIO_AUTH_TOKEN")
    from_number = _setting("twilio_from_number", "TWILIO_FROM_NUMBER")
    if sid and token and from_number:
        return {"sid": sid, "token": token, "from": from_number}
    return None


def sms_configured() -> bool:
    return sms_config() is not None


def send_sms(to: str, body: str) -> bool:
    """Send one SMS via Twilio. False when unconfigured, no number, or failed."""
    cfg = sms_config()
    to = (to or "").strip()
    if not cfg or not to:
        return False
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{cfg['sid']}/Messages.json",
            auth=(cfg["sid"], cfg["token"]),
            data={"To": to, "From": cfg["from"], "Body": body[:1500]},
            timeout=_TIMEOUT,
        )
        if resp.status_code >= 300:
            log.warning("Twilio SMS to %s failed: HTTP %s %s",
                        to, resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:
        log.warning("Twilio SMS to %s failed: %s", to, exc)
        return False


def valid_discord_webhook(url: str) -> bool:
    return bool(_DISCORD_RE.match((url or "").strip()))


def send_discord(webhook_url: str, content: str) -> bool:
    """Post a message to a user's Discord webhook. Best-effort."""
    webhook_url = (webhook_url or "").strip()
    if not valid_discord_webhook(webhook_url):
        return False
    try:
        resp = requests.post(
            webhook_url, json={"content": content[:1900]}, timeout=_TIMEOUT
        )
        if resp.status_code >= 300:
            log.warning("Discord webhook post failed: HTTP %s", resp.status_code)
            return False
        return True
    except Exception as exc:
        log.warning("Discord webhook post failed: %s", exc)
        return False


def notify_user(user_id: int, subject: str, body: str, *, email_fn=None) -> dict[str, Any]:
    """Fan an alert out to every channel the user has set up.

    Email always goes (via ``email_fn`` — injected to avoid a circular import);
    SMS when the user has a number AND an admin configured Twilio; Discord when
    the user pasted a webhook. Returns which channels were attempted/succeeded.
    """
    sent = {"email": False, "sms": False, "discord": False}
    email = db.get_user_email(user_id)
    if email and email_fn:
        sent["email"] = bool(email_fn(email, subject, body))

    settings = db.get_user_settings(user_id)
    phone = (settings.get("phone_number") or "").strip()
    if phone:
        sent["sms"] = bool(send_sms(phone, f"{subject}\n{body}"))
    webhook = (settings.get("discord_webhook") or "").strip()
    if webhook:
        sent["discord"] = bool(send_discord(webhook, f"**{subject}**\n{body}"))
    return sent
