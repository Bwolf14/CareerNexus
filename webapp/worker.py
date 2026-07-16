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

from job_scraper import dedupe_cross_board

from . import configure_logging, db, email_utils, search_service

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


def _handle_alert(saved_search_id: int, new_search_id: int) -> None:
    """Email the user postings in the new run that weren't in the previous one."""
    ss = db.get_saved_search(saved_search_id)
    if not ss:
        return
    new_jobs = dedupe_cross_board(db.get_jobs_for_search(new_search_id))
    old_search_id = ss.get("last_search_id")

    if old_search_id:
        old_keys = {
            j["dedup_key"]
            for j in dedupe_cross_board(db.get_jobs_for_search(old_search_id))
        }
        fresh = [j for j in new_jobs if j.get("dedup_key") not in old_keys]
        if fresh:
            _email_alert(ss, new_search_id, fresh)
        else:
            log.info("saved_search %s: no new postings this run", saved_search_id)
    else:
        log.info(
            "saved_search %s: first run (baseline of %s postings, no email)",
            saved_search_id, len(new_jobs),
        )

    db.set_saved_search_result(saved_search_id, new_search_id)


def _email_alert(ss: dict, search_id: int, fresh: list) -> None:
    email = db.get_user_email(ss["user_id"])
    if not email:
        return
    label = ss.get("label") or "your saved search"
    lines = [
        f"{len(fresh)} new job posting(s) matched {label}:",
        "",
    ]
    for j in fresh[:ALERT_MAX_LISTED]:
        bits = " · ".join(
            b for b in [j.get("title"), j.get("company"), j.get("location")] if b
        )
        lines.append(f"• {bits}")
        if j.get("job_url"):
            lines.append(f"  {j['job_url']}")
    if len(fresh) > ALERT_MAX_LISTED:
        lines.append(f"…and {len(fresh) - ALERT_MAX_LISTED} more.")
    lines += [
        "",
        f"See them all: {email_utils.base_url()}/matches/{search_id}",
    ]
    email_utils.send_email(
        email, f"Career Nexus — {len(fresh)} new job match(es)", "\n".join(lines)
    )
    log.info("emailed %s new postings to %s (saved_search %s)",
             len(fresh), email, ss["id"])


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
