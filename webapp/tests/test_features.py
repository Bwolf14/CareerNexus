"""
Tests for the second-round features: cross-board dedup, per-posting tailoring,
the background worker's scrape/alert logic, and the saved-jobs / alerts / auth
routes (password reset, login throttle, account export/delete).

DB and scraper are stubbed with monkeypatch, so no MariaDB or network is needed.
"""

from __future__ import annotations

import pytest

from webapp import app as app_module
from webapp.app import app

from .test_app import JOBS, PARSED, SEARCH_ROW, USER, _common_stubs  # reuse fixtures


@pytest.fixture()
def client(monkeypatch, tmp_path):
    _common_stubs(monkeypatch, tmp_path)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = 1
        yield c


@pytest.fixture()
def anon_client(monkeypatch, tmp_path):
    _common_stubs(monkeypatch, tmp_path)
    with app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Cross-board deduplication
# ---------------------------------------------------------------------------
def test_dedupe_cross_board_collapses_same_posting():
    from job_scraper import dedupe_cross_board

    jobs = [
        {"title": "Network Technician", "company": "Northwind",
         "location": "Calgary, AB", "source_site": "indeed", "job_url": "u1"},
        {"title": "Network  Technician", "company": "Northwind",
         "location": "Calgary, Alberta", "source_site": "glassdoor", "job_url": "u2"},
        {"title": "Welder", "company": "Acme",
         "location": "Edmonton, AB", "source_site": "indeed", "job_url": "u3"},
    ]
    out = dedupe_cross_board(jobs)
    assert len(out) == 2
    net = next(j for j in out if j["title"].startswith("Network"))
    assert net["also_on"] == ["glassdoor"]
    assert net["dedup_key"]


def test_dedupe_key_stable_across_boards():
    from job_scraper import posting_dedup_key

    a = posting_dedup_key("Senior Welder", "Acme Co.", "Calgary, AB")
    b = posting_dedup_key("senior  welder", "ACME CO", "Calgary, Alberta")
    assert a == b


# ---------------------------------------------------------------------------
# Per-posting resume tailoring (deterministic)
# ---------------------------------------------------------------------------
def test_tailor_for_job_emphasizes_matched_skills():
    from job_matcher import tailor_for_job

    job = {
        "title": "Network Technician",
        "company": "Northwind",
        "description": "We need Cisco IOS, VLANs and Python. CCNA a plus.",
    }
    out = tailor_for_job(PARSED, job)
    assert out["generator"] == "template"
    # Skills on the resume that appear in the posting are surfaced to lead with.
    assert any(s.lower() == "cisco ios" for s in out["emphasize"])
    assert out["bullets"]


def test_tailor_route_uses_deterministic_without_ai(client):
    resp = client.get("/tailor/7/" + _job_key())
    assert resp.status_code == 200
    assert b"Tailor your resume" in resp.data
    assert b"rule-based" in resp.data


def _job_key():
    from job_scraper import posting_dedup_key
    j = JOBS[0]
    return posting_dedup_key(j["title"], j["company"], j["location"])


# ---------------------------------------------------------------------------
# Saved jobs / tracker
# ---------------------------------------------------------------------------
def test_save_job_from_search(client, monkeypatch):
    captured = {}

    def fake_save(uid, key, job):
        captured["uid"], captured["key"], captured["title"] = uid, key, job.get("title")
        return 1

    monkeypatch.setattr(app_module.db, "save_job", fake_save)
    resp = client.post("/saved/add", data={"search_id": "7", "dedup_key": _job_key()})
    assert resp.status_code == 302
    assert captured["uid"] == 1
    assert captured["title"] == "Network Technician"


def test_saved_page_groups_by_status(client, monkeypatch):
    monkeypatch.setattr(
        app_module.db, "list_saved_jobs",
        lambda uid: [
            {"id": 1, "title": "Network Tech", "company": "NW", "location": "Calgary",
             "source_site": "indeed", "salary_display": None, "is_remote": 0,
             "job_url": "u", "status": "applied", "notes": None},
        ],
    )
    resp = client.get("/saved")
    assert resp.status_code == 200
    assert b"Network Tech" in resp.data
    assert b"Applied" in resp.data


def test_saved_update_rejects_bad_status(client, monkeypatch):
    monkeypatch.setattr(app_module.db, "update_saved_job",
                        lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad")))
    resp = client.post("/saved/1/update", data={"status": "nonsense"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Alerts (saved searches)
# ---------------------------------------------------------------------------
def test_alerts_page_renders(client, monkeypatch):
    monkeypatch.setattr(app_module.db, "list_saved_searches", lambda uid: [])
    monkeypatch.setattr(app_module.db, "list_resumes",
                        lambda limit=100, user_id=None: [
                            {"id": 3, "username": "Alex", "upload_date": "2026-07-01"}])
    resp = client.get("/alerts")
    assert resp.status_code == 200
    assert b"Saved searches" in resp.data


def test_alerts_create(client, monkeypatch):
    captured = {}

    def fake_create(uid, rid, label, params, freq, next_run):
        captured.update(uid=uid, rid=rid, freq=freq, params=params)
        return 3

    monkeypatch.setattr(app_module.db, "create_saved_search", fake_create)
    resp = client.post(
        "/alerts/create",
        data={"resume_id": "3", "label": "Calgary jobs", "frequency": "weekly",
              "keywords": "welding", "work_type": "any", "country": "Canada"},
    )
    assert resp.status_code == 302
    assert captured["rid"] == 3
    assert captured["freq"] == "weekly"
    assert captured["params"]["keywords"] == ["welding"]


def test_alerts_create_requires_owned_resume(client, monkeypatch):
    # resume 999 isn't owned (get_resume_owner returns None) -> 404.
    resp = client.post("/alerts/create", data={"resume_id": "999"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Account export + deletion
# ---------------------------------------------------------------------------
def test_account_export_downloads_json(client, monkeypatch):
    monkeypatch.setattr(app_module.db, "export_user_data",
                        lambda uid: {"account": dict(USER), "resumes": []})
    resp = client.get("/account/export")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    assert "attachment" in resp.headers["Content-Disposition"]
    assert b"alex@example.com" in resp.data


def test_account_delete_requires_email_confirmation(client, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(app_module.db, "delete_user",
                        lambda uid: called.__setitem__("n", called["n"] + 1))
    # Wrong confirmation -> no deletion.
    resp = client.post("/account/delete", data={"confirm_email": "wrong@example.com"})
    assert resp.status_code == 302
    assert called["n"] == 0
    # Correct confirmation -> deleted + session cleared.
    resp = client.post("/account/delete", data={"confirm_email": "alex@example.com"})
    assert resp.status_code == 302
    assert called["n"] == 1
    with client.session_transaction() as sess:
        assert "user_id" not in sess


# ---------------------------------------------------------------------------
# Auth: password reset + login throttle
# ---------------------------------------------------------------------------
def test_forgot_password_always_confirms(anon_client, monkeypatch):
    monkeypatch.setattr(app_module.db, "get_user_by_email", lambda e: None)
    resp = anon_client.post("/forgot", data={"email": "nobody@example.com"})
    assert resp.status_code == 302  # same response whether or not the email exists


def test_reset_with_valid_token(anon_client, monkeypatch):
    from webapp import auth as auth_module

    monkeypatch.setattr(app_module.db, "get_valid_reset",
                        lambda th: {"id": 2, "user_id": 9})
    captured = {}
    monkeypatch.setattr(app_module.db, "reset_password",
                        lambda rid, uid, ph: captured.update(rid=rid, uid=uid))
    resp = anon_client.post(
        "/reset/sometoken",
        data={"password": "brandnewpw1", "confirm": "brandnewpw1"},
    )
    assert resp.status_code == 302
    assert captured == {"rid": 2, "uid": 9}


def test_reset_invalid_token(anon_client, monkeypatch):
    monkeypatch.setattr(app_module.db, "get_valid_reset", lambda th: None)
    resp = anon_client.get("/reset/badtoken")
    assert resp.status_code == 400
    assert b"invalid or expired" in resp.data


def test_login_locked_out(anon_client, monkeypatch):
    monkeypatch.setattr(app_module.db, "throttle_status", lambda ident: "2099-01-01")
    resp = anon_client.post("/login", data={"email": "u@example.com", "password": "x"})
    assert resp.status_code == 429
    assert b"Too many failed attempts" in resp.data


def test_login_failure_is_recorded(anon_client, monkeypatch):
    recorded = {"n": 0}
    monkeypatch.setattr(app_module.db, "get_user_by_email", lambda e: None)
    monkeypatch.setattr(app_module.db, "record_login_failure",
                        lambda *a, **kw: recorded.__setitem__("n", recorded["n"] + 1))
    resp = anon_client.post("/login", data={"email": "u@example.com", "password": "x"})
    assert resp.status_code == 401
    assert recorded["n"] == 1


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------
def test_worker_processes_scrape(monkeypatch):
    from webapp import worker

    claimed = {"job": {"id": 5, "resume_id": 3, "saved_search_id": None, "params": {}}}
    monkeypatch.setattr(worker.db, "claim_next_scrape",
                        lambda: claimed.pop("job", None))
    monkeypatch.setattr(worker.db, "get_resume_json", lambda rid: PARSED)
    monkeypatch.setattr(worker.search_service, "run_scrape",
                        lambda rid, parsed, params: {"search_id": 7, "job_count": 2,
                                                     "source": "jobspy"})
    finished = {}
    monkeypatch.setattr(worker.db, "finish_scrape",
                        lambda jid, sid, error=None: finished.update(jid=jid, sid=sid, error=error))

    assert worker.process_one_scrape() is True
    assert finished == {"jid": 5, "sid": 7, "error": None}
    # Queue now empty.
    assert worker.process_one_scrape() is False


def test_worker_marks_failure(monkeypatch):
    from webapp import worker

    jobs = [{"id": 6, "resume_id": 3, "saved_search_id": None, "params": {}}]
    monkeypatch.setattr(worker.db, "claim_next_scrape",
                        lambda: jobs.pop() if jobs else None)
    monkeypatch.setattr(worker.db, "get_resume_json", lambda rid: PARSED)

    def boom(rid, parsed, params):
        raise RuntimeError("scrape exploded")

    monkeypatch.setattr(worker.search_service, "run_scrape", boom)
    finished = {}
    monkeypatch.setattr(worker.db, "finish_scrape",
                        lambda jid, sid, error=None: finished.update(jid=jid, sid=sid, error=error))

    worker.process_one_scrape()
    assert finished["jid"] == 6
    assert finished["sid"] is None
    assert "exploded" in finished["error"]


def test_worker_alert_emails_new_postings(monkeypatch):
    from webapp import worker

    # saved search whose previous run was search 6.
    monkeypatch.setattr(worker.db, "get_saved_search",
                        lambda sid: {"id": 2, "user_id": 1, "label": "Calgary",
                                     "last_search_id": 6})
    # new run (7) has one extra posting vs the old run (6).
    def jobs_for(search_id):
        base = [{"title": "Network Technician", "company": "Northwind",
                 "location": "Calgary, AB", "source_site": "indeed", "job_url": "u1"}]
        if search_id == 7:
            base.append({"title": "Welder", "company": "Acme",
                         "location": "Edmonton", "source_site": "indeed", "job_url": "u2"})
        return base

    monkeypatch.setattr(worker.db, "get_jobs_for_search", jobs_for)
    monkeypatch.setattr(worker.db, "get_user_email", lambda uid: "alex@example.com")
    monkeypatch.setattr(worker.db, "set_saved_search_result", lambda sid, search_id: None)
    sent = {}
    monkeypatch.setattr(worker.email_utils, "send_email",
                        lambda to, subject, body: sent.update(to=to, body=body) or True)

    worker._handle_alert(2, 7)
    assert sent["to"] == "alex@example.com"
    assert "Welder" in sent["body"]          # the new posting is emailed
    assert "Network Technician" not in sent["body"]  # the pre-existing one isn't


# ---------------------------------------------------------------------------
# OCR fallback (graceful when Tesseract/pytesseract absent)
# ---------------------------------------------------------------------------
def test_ocr_degrades_gracefully():
    from resume_parser.extractors.ocr import ocr_available, ocr_document_lines

    assert isinstance(ocr_available(), bool)
    # A bad "document" never raises out of the OCR helper — it returns [].
    assert ocr_document_lines(None) == []

