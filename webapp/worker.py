"""
Background worker: drains the scrape queue and runs scheduled saved-search alerts.

Run as its own process (its own container in docker-compose), sharing the same
image and database as the web app::

    python -m webapp.worker

Two responsibilities, both DB-backed (no Redis/broker):

1. **Scrape queue** — claims ``scrape_jobs`` rows the web app enqueued (so the
   upload/search request returns instantly), runs the scrape, and stores the
   results as a normal ``job_searches`` row.
2. **Scheduler** — periodically finds ``saved_searches`` whose ``next_run_at``
   has passed, enqueues a scrape for each, and (when that scrape finishes)
   emails the user any postings that weren't in the previous run.

Everything is defensive: one bad job never stops the loop, and the worker
survives the DB going away and coming back.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta

from ai_client import AIClientError, load_settings, score_dream_likeness
from job_scraper import dedupe_cross_board

from . import configure_logging, db, email_utils, notifications, search_service

configure_logging()
log = logging.getLogger("careernexus.worker")

POLL_SECONDS = int(os.environ.get("WORKER_POLL_SECONDS", "5"))
SCHEDULE_SECONDS = int(os.environ.get("WORKER_SCHEDULE_SECONDS", "60"))
# How many new postings to list in an alert email before summarising the rest.
ALERT_MAX_LISTED = 15


def _next_run(frequency: str, now: datetime) -> datetime:
    return now + (timedelta(weeks=1) if frequency == "weekly" else timedelta(days=1))


# ---------------------------------------------------------------------------
# Scrape queue
# ---------------------------------------------------------------------------
def process_one_scrape() -> bool:
    """Claim and run a single queued scrape. Returns False if the queue is empty."""
    job = db.claim_next_scrape()
    if not job:
        return False

    job_id = job["id"]
    log.info("running scrape job %s (resume %s)", job_id, job.get("resume_id"))
    try:
        parsed = db.get_resume_json(job["resume_id"])
        if parsed is None:
            raise RuntimeError("the resume for this search no longer exists")
        res = search_service.run_scrape(job["resume_id"], parsed, job.get("params") or {})
        db.finish_scrape(job_id, res["search_id"])
        log.info(
            "scrape job %s done: search %s, %s postings (%s)",
            job_id, res["search_id"], res["job_count"], res["source"],
        )
        if job.get("saved_search_id"):
            _handle_alert(job["saved_search_id"], res["search_id"])
    except Exception as exc:
        log.warning("scrape job %s failed: %s", job_id, exc)
        try:
            db.finish_scrape(job_id, None, error=str(exc)[:1000])
        except Exception:
            log.exception("could not mark scrape job %s failed", job_id)
    return True


# ---------------------------------------------------------------------------
# Scheduled saved-search alerts
# ---------------------------------------------------------------------------
def scheduler_tick(now: datetime) -> int:
    """Enqueue a scrape for every saved search that's due. Returns how many."""
    try:
        due = db.due_saved_searches(now)
    except Exception as exc:
        log.warning("could not read due saved searches: %s", exc)
        return 0

    enqueued = 0
    for ss in due:
        try:
            db.enqueue_scrape(
                ss["user_id"], ss["resume_id"], ss.get("params") or {},
                saved_search_id=ss["id"],
            )
            # Push next_run_at forward now so we don't re-enqueue before it runs.
            db.bump_saved_search_next_run(ss["id"], _next_run(ss.get("frequency"), now))
            enqueued += 1
            log.info("enqueued scheduled search for saved_search %s", ss["id"])
        except Exception as exc:
            log.warning("could not enqueue saved_search %s: %s", ss.get("id"), exc)
    return enqueued


def _ai_config_for_user(user_id: int) -> dict:
    """Backend AI slots, plus the user's own cloud key when they enabled it."""
    config = load_settings()
    try:
        us = db.get_user_settings(user_id)
    except Exception:
        us = {}
    if us.get("ai_cloud_enabled") and us.get("ai_api_key") and us.get("ai_model"):
        config = {**config, "cloud": {
            "enabled": True, "provider": us.get("ai_provider") or "openai",
            "api_key": us.get("ai_api_key"), "model": us.get("ai_model"),
        }}
    return config


def _apply_likeness(ss: dict, fresh: list[dict]) -> list[dict]:
    """AI dream-job likeness gate: keep postings scoring >= the alert's bar.

    When no model is reachable the objective filters alone decide (logged) —
    an offline model must never silently swallow alerts.
    """
    params = ss.get("params") or {}
    dream = (params.get("dream_description") or "").strip()
    if not dream or not fresh:
        return fresh
    threshold = int(params.get("likeness_threshold") or 70)
    try:
        scores = score_dream_likeness(_ai_config_for_user(ss["user_id"]), dream, fresh)
    except AIClientError as exc:
        log.warning("saved_search %s: likeness scoring unavailable (%s) — "
                    "keeping objective-filter results", ss.get("id"), exc)
        return fresh
    kept = []
    for i, job in enumerate(fresh):
        score = scores.get(i)
        if score is None or score >= threshold:
            job = dict(job)
            job["likeness"] = score
            kept.append(job)
    log.info("saved_search %s: %s/%s postings passed likeness >= %s",
             ss.get("id"), len(kept), len(fresh), threshold)
    return kept


def _matches_criteria(job: dict, params: dict) -> bool:
    """Apply the alert's specific criteria (company / title-contains) to one job.

    These narrow a broad resume-driven search to e.g. "Google" + "engineer" so
    the alert only fires for genuinely relevant new postings.
    """
    company_filter = (params.get("filter_company") or "").strip().lower()
    title_filter = (params.get("filter_title") or "").strip().lower()
    if company_filter and company_filter not in (job.get("company") or "").lower():
        return False
    if title_filter and title_filter not in (job.get("title") or "").lower():
        return False
    return True


def _handle_alert(saved_search_id: int, new_search_id: int) -> None:
    """Notify the user of postings in the new run that weren't in the previous
    one AND match the alert's criteria (company/title filters)."""
    ss = db.get_saved_search(saved_search_id)
    if not ss:
        return
    params = ss.get("params") or {}
    new_jobs = dedupe_cross_board(db.get_jobs_for_search(new_search_id))
    old_search_id = ss.get("last_search_id")

    if old_search_id:
        old_keys = {
            j["dedup_key"]
            for j in dedupe_cross_board(db.get_jobs_for_search(old_search_id))
        }
        fresh = [
            j for j in new_jobs
            if j.get("dedup_key") not in old_keys and _matches_criteria(j, params)
        ]
        fresh = _apply_likeness(ss, fresh)
        if fresh:
            _send_alert(ss, new_search_id, fresh)
        else:
            log.info("saved_search %s: no new postings matching the criteria",
                     saved_search_id)
    else:
        log.info(
            "saved_search %s: first run (baseline of %s postings, no alert)",
            saved_search_id, len(new_jobs),
        )

    db.set_saved_search_result(saved_search_id, new_search_id)


def _send_alert(ss: dict, search_id: int, fresh: list) -> None:
    """Deliver one alert on every channel the user configured."""
    label = ss.get("label") or "your saved search"
    subject = f"Career Nexus — {len(fresh)} new job match(es) for {label}"
    lines = []
    for j in fresh[:ALERT_MAX_LISTED]:
        bits = " · ".join(
            b for b in [j.get("title"), j.get("company"), j.get("location")] if b
        )
        if j.get("likeness") is not None:
            bits += f" · {j['likeness']}% match to your dream job"
        lines.append(f"• {bits}")
        if j.get("job_url"):
            lines.append(f"  {j['job_url']}")
    if len(fresh) > ALERT_MAX_LISTED:
        lines.append(f"…and {len(fresh) - ALERT_MAX_LISTED} more.")
    lines.append(f"\nSee them all: {email_utils.base_url()}/matches/{search_id}")
    body = "\n".join(lines)

    sent = notifications.notify_user(
        ss["user_id"], subject, body, email_fn=email_utils.send_email
    )
    log.info("alert for saved_search %s: %s new postings, channels %s",
             ss["id"], len(fresh), sent)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run() -> None:
    db.wait_for_db()
    log.info("worker started (poll=%ss, schedule=%ss)", POLL_SECONDS, SCHEDULE_SECONDS)
    last_schedule = 0.0
    while True:
        try:
            worked = process_one_scrape()
        except Exception as exc:  # never let the loop die
            log.exception("scrape loop error: %s", exc)
            worked = False

        now_mono = time.monotonic()
        if now_mono - last_schedule >= SCHEDULE_SECONDS:
            scheduler_tick(datetime.utcnow())
            last_schedule = now_mono

        if not worked:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
