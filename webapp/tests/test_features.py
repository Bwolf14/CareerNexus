"""
Tests for the second-round features: cross-board dedup, per-posting tailoring,
the background worker's scrape/alert logic, and the saved-jobs / alerts / auth
routes (password reset, login throttle, account export/delete).

DB and scraper are stubbed with monkeypatch, so no MariaDB or network is needed.
"""

from __future__ import annotations

import json

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
# Career-page redesign + job/company detail pages + resume naming
# ---------------------------------------------------------------------------
def test_upload_requires_resume_name(client, monkeypatch):
    monkeypatch.setattr(app_module.db, "save_parsed_resume",
                        lambda *a, **kw: pytest.fail("should not be called"))
    resp = client.post("/upload", data={})  # no resume_name
    assert resp.status_code == 400
    assert b"Name your resume" in resp.data


def test_plan_page_top_pick_and_panel(client):
    resp = client.get("/recommendations/7")
    assert resp.status_code == 200
    assert b"top-pick" in resp.data           # #1 highlighted
    assert b"TOP PICK" in resp.data
    assert b"rank-panel" in resp.data         # slide-out ranked list exists
    assert b"/saved/add" in resp.data         # save-to-tracker from the plan page
    assert b"Suggested resume alterations" in resp.data
    assert b"Coming soon" not in resp.data


def test_job_detail_page_renders(client):
    resp = client.get("/job/7/" + _job_key())
    assert resp.status_code == 200
    assert b"Network Technician" in resp.data
    assert b"About Northwind" in resp.data          # company section present
    assert b"View original posting" in resp.data
    # The async fetch URL must keep a literal & between params — Jinja
    # autoescaping to &amp; inside the script block breaks the key param
    # (regression: "Company lookup failed: SyntaxError: Unexpected token '<'").
    assert b"/api/company-info?part=basic&search_id=7&key=" in resp.data
    assert b"amp;key=" not in resp.data


def test_job_detail_404_for_unknown_key(client):
    assert client.get("/job/7/doesnotexist").status_code == 404


def test_tracker_job_detail_renders(client, monkeypatch):
    monkeypatch.setattr(
        app_module.db, "get_saved_job",
        lambda uid, sid: {"id": sid, "user_id": 1, "dedup_key": "k1",
                          "title": "Welder", "company": "Acme", "location": "Edmonton",
                          "source_site": "indeed", "salary_display": None,
                          "is_remote": 0, "job_url": "https://x", "description": "weld",
                          "status": "applied", "notes": None},
    )
    resp = client.get("/tracker/job/4")
    assert resp.status_code == 200
    assert b"Welder" in resp.data
    assert b"About Acme" in resp.data
    assert b"saved_id=4" in resp.data     # async info uses the saved context


def test_company_info_basic_returns_profile(client, monkeypatch):
    monkeypatch.setattr(
        app_module.company_info, "company_profile",
        lambda company: {"source": "wikidata", "qid": "Q1", "name": company,
                         "description": "Software company",
                         "extract": "Northwind makes software.", "url": "https://w",
                         "facts": {"employees": "12,000 (2024)",
                                   "revenue": "3.2 billion Canadian dollar (2024)"}},
    )
    resp = client.get("/api/company-info?part=basic&search_id=7&key=" + _job_key())
    data = json.loads(resp.data)
    assert data["company"] == "Northwind"
    assert data["profile"]["extract"].startswith("Northwind makes")
    assert data["profile"]["facts"]["employees"].startswith("12,000")


def test_company_info_ai_safe_mode(client, monkeypatch):
    # No AI configured -> the endpoint reports safe mode, never errors.
    resp = client.get("/api/company-info?part=ai&search_id=7&key=" + _job_key())
    data = json.loads(resp.data)
    assert data["ai"] is None
    assert data.get("safe_mode") is True


def test_tracker_page_has_phase_nav(client, monkeypatch):
    monkeypatch.setattr(
        app_module.db, "list_saved_jobs",
        lambda uid: [{"id": 1, "title": "Network Tech", "company": "NW",
                      "location": "Calgary", "source_site": "indeed",
                      "salary_display": None, "is_remote": 0, "job_url": "u",
                      "status": "applied", "notes": None}],
    )
    resp = client.get("/saved")
    assert resp.status_code == 200
    assert b"Job application tracker" in resp.data
    assert b"phase-btn" in resp.data                 # left-side phase nav
    assert b'data-phase="rejected"' in resp.data
    assert b"/tracker/job/1" in resp.data            # job links to detail page


def test_resume_preview_fragment(client):
    resp = client.get("/resume/3/preview")
    assert resp.status_code == 200
    assert b"Alex Smith" in resp.data
    assert b"Network Technician" in resp.data
    resp = client.get("/resume/999/preview")
    assert resp.status_code == 404


def test_company_profile_resolves_aliases_like_rbc(monkeypatch, tmp_path):
    """"RBC" must resolve to Royal Bank of Canada via Wikidata alias search."""
    from webapp import company_info as ci
    monkeypatch.setenv("JOB_RESULTS_DIR", str(tmp_path))

    # Wikidata search puts an obscure same-alias org FIRST (this happens for
    # real: "RBC" → Rwanda Biomedical Center). Prominence ranking must still
    # pick the bank.
    monkeypatch.setattr(ci, "_wd_search", lambda name: [
        {"id": "Q30296401", "label": "Rwanda Biomedical Center",
         "description": "healthcare organization in Kigali, Rwanda"},
        {"id": "Q106", "label": "red blood cell", "description": "type of blood cell"},
        {"id": "Q735261", "label": "Royal Bank of Canada",
         "description": "Canadian multinational banking and financial services company"},
    ])
    bank_entity = {
        "id": "Q735261",
        "labels": {"en": {"value": "Royal Bank of Canada"}},
        "descriptions": {"en": {"value": "Canadian multinational banking company"}},
        "sitelinks": {f"wiki{i}": {"title": "Royal Bank of Canada"} for i in range(30)}
        | {"enwiki": {"title": "Royal Bank of Canada"}},
        "claims": {
            "P1128": [{"rank": "normal",
                       "mainsnak": {"datavalue": {"value": {"amount": "+94624", "unit": "1"}}},
                       "qualifiers": {"P585": [{"datavalue": {"value": {"time": "+2024-00-00T00:00:00Z"}}}]}}],
            "P2139": [{"rank": "preferred",
                       "mainsnak": {"datavalue": {"value": {"amount": "+56100000000",
                                    "unit": "http://www.wikidata.org/entity/Q1104069"}}},
                       "qualifiers": {"P585": [{"datavalue": {"value": {"time": "+2024-00-00T00:00:00Z"}}}]}}],
            "P571": [{"rank": "normal",
                      "mainsnak": {"datavalue": {"value": {"time": "+1864-00-00T00:00:00Z"}}}}],
            "P856": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "https://www.rbc.com"}}}],
            "P452": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": "Q806718"}}}}],
            "P159": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": "Q172"}}}}],
        },
    }
    rwanda_entity = {
        "id": "Q30296401",
        "labels": {"en": {"value": "Rwanda Biomedical Center"}},
        "descriptions": {"en": {"value": "healthcare organization in Kigali, Rwanda"}},
        "sitelinks": {"enwiki": {"title": "Rwanda Biomedical Centre"}},
        "claims": {},
    }
    monkeypatch.setattr(ci, "_wd_entities",
                        lambda qids: {"Q735261": bank_entity, "Q30296401": rwanda_entity})
    monkeypatch.setattr(ci, "_wd_labels", lambda qids: {
        "Q806718": "banking industry", "Q172": "Toronto", "Q1104069": "Canadian dollar",
    })
    monkeypatch.setattr(ci, "_fetch_summary", lambda title: {
        "title": title, "description": "Canadian bank",
        "extract": "The Royal Bank of Canada is a Canadian multinational…",
        "url": "https://en.wikipedia.org/wiki/Royal_Bank_of_Canada",
    })

    profile = ci.company_profile("RBC")
    assert profile["name"] == "Royal Bank of Canada"
    assert profile["facts"]["employees"] == "94,624 (2024)"
    assert profile["facts"]["revenue"] == "56.1 billion Canadian dollar (2024)"
    assert profile["facts"]["founded"] == "1864"
    assert profile["facts"]["headquarters"] == "Toronto"
    assert profile["facts"]["website"] == "https://www.rbc.com"
    assert profile["extract"].startswith("The Royal Bank")

    # Cached: a second call must not hit the (now broken) network fns.
    monkeypatch.setattr(ci, "_wd_search", lambda name: pytest.fail("cache miss"))
    assert ci.company_profile("RBC")["name"] == "Royal Bank of Canada"


def test_company_profile_rejects_non_org(monkeypatch, tmp_path):
    from webapp import company_info as ci
    monkeypatch.setenv("JOB_RESULTS_DIR", str(tmp_path))
    # Only non-org candidates -> no profile (and the negative is cached).
    monkeypatch.setattr(ci, "_wd_search", lambda name: [
        {"id": "Q89", "label": "apple", "description": "fruit of the apple tree"},
    ])
    assert ci.company_profile("Apple Orchard Fresh") is None


# ---------------------------------------------------------------------------
# Resume deletion + job-detail facts + score levels
# ---------------------------------------------------------------------------
def test_resume_delete_own(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(app_module.db, "delete_resume",
                        lambda uid, rid: captured.update(uid=uid, rid=rid) or True)
    resp = client.post("/resume/3/delete")
    assert resp.status_code == 302
    assert captured == {"uid": 1, "rid": 3}


def test_resume_delete_not_owned_404(client, monkeypatch):
    monkeypatch.setattr(app_module.db, "delete_resume", lambda uid, rid: False)
    assert client.post("/resume/999/delete").status_code == 404


def test_home_has_name_above_drop_and_delete(client):
    resp = client.get("/")
    body = resp.data.decode()
    # The resume-name field must come before the drop zone in the form.
    assert body.index('id="resume_name"') < body.index('id="drop"')


def test_job_detail_at_a_glance_and_toggle(client):
    resp = client.get("/job/7/" + _job_key())
    body = resp.data.decode()
    assert "At a glance" in body
    assert "Pay" in body and "$70,000" in body        # salary fact surfaced
    # Fixture description is short, so no toggle — but the summary section shows.
    assert "Summary" in body


def test_scores_reach_healthy_levels():
    from job_matcher import score_jobs
    picks = score_jobs(PARSED, JOBS)
    assert picks[0]["score"] >= 60      # good match without answers
    answers = {"preferred_skills": ["Python", "Cisco IOS"], "work_style": "On-site",
               "salary": {"min": "70000", "max": "95000", "interval": "yearly"}}
    picks = score_jobs(PARSED, JOBS, answers)
    assert picks[0]["score"] >= 80      # strong match with answers


def test_company_profile_includes_leadership_facts(monkeypatch, tmp_path):
    from webapp import company_info as ci
    monkeypatch.setenv("JOB_RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(ci, "_wd_search", lambda name: [
        {"id": "Q1", "label": "MegaCorp", "description": "multinational company"}])
    monkeypatch.setattr(ci, "_wd_entities", lambda qids: {"Q1": {
        "id": "Q1",
        "labels": {"en": {"value": "MegaCorp"}},
        "descriptions": {"en": {"value": "multinational company"}},
        "sitelinks": {"enwiki": {"title": "MegaCorp"}},
        "claims": {
            "P749": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": "Q8"}}}}],
            "P414": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": "Q7"}}}}],
        },
    }})
    monkeypatch.setattr(ci, "_wd_labels", lambda qids: {
        "Q8": "MegaHoldings", "Q7": "Toronto Stock Exchange"})
    monkeypatch.setattr(ci, "_fetch_summary", lambda title: None)
    profile = ci.company_profile("MegaCorp")
    assert profile["facts"]["parent"] == "MegaHoldings"
    assert profile["facts"]["listed_on"] == "Toronto Stock Exchange"


# ---------------------------------------------------------------------------
# SMS / Discord notifications + per-user settings
# ---------------------------------------------------------------------------
def test_account_saves_notification_channels(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(app_module.db, "upsert_user_settings",
                        lambda uid, **f: saved.update(uid=uid, **f))
    resp = client.post("/account/notifications", data={
        "phone_number": "+1 403 555 0142",
        "discord_webhook": "https://discord.com/api/webhooks/123/abc",
    })
    assert resp.status_code == 302
    assert saved["phone_number"] == "+1 403 555 0142"
    assert saved["discord_webhook"].startswith("https://discord.com/api/webhooks/")


def test_account_rejects_bad_webhook(client, monkeypatch):
    monkeypatch.setattr(app_module.db, "upsert_user_settings",
                        lambda uid, **f: pytest.fail("should not save"))
    resp = client.post("/account/notifications", data={
        "discord_webhook": "https://evil.example.com/hook",
    }, follow_redirects=True)
    assert b"Discord webhook" in resp.data


def test_notify_user_fans_out(monkeypatch):
    from webapp import notifications as n
    monkeypatch.setattr(n.db, "get_user_email", lambda uid: "a@b.c")
    monkeypatch.setattr(n.db, "get_user_settings", lambda uid: {
        "phone_number": "+14035550142",
        "discord_webhook": "https://discord.com/api/webhooks/1/x",
    })
    calls = {}
    monkeypatch.setattr(n, "send_sms", lambda to, body: calls.setdefault("sms", to) or True)
    monkeypatch.setattr(n, "send_discord", lambda url, c: calls.setdefault("dc", url) or True)
    sent = n.notify_user(1, "subj", "body",
                         email_fn=lambda to, s, b: calls.setdefault("email", to) or True)
    assert sent == {"email": True, "sms": True, "discord": True}
    assert calls["sms"] == "+14035550142"


def test_send_sms_unconfigured_returns_false(monkeypatch):
    from webapp import notifications as n
    monkeypatch.setattr(n, "sms_config", lambda: None)
    assert n.send_sms("+15551234567", "hi") is False


# ---------------------------------------------------------------------------
# Alert criteria filtering (worker)
# ---------------------------------------------------------------------------
def test_alert_criteria_filters_postings(monkeypatch):
    from webapp import worker
    monkeypatch.setattr(worker.db, "get_saved_search", lambda sid: {
        "id": 2, "user_id": 1, "label": "Google roles", "last_search_id": 6,
        "params": {"filter_company": "google", "filter_title": "engineer"},
    })

    def jobs_for(search_id):
        base = [{"title": "Software Engineer", "company": "Google",
                 "location": "Toronto", "source_site": "indeed", "job_url": "u0"}]
        if search_id == 7:
            base += [
                {"title": "Software Engineer II", "company": "Google",
                 "location": "Toronto", "source_site": "indeed", "job_url": "u1"},
                {"title": "Software Engineer", "company": "Amazon",   # wrong company
                 "location": "Toronto", "source_site": "indeed", "job_url": "u2"},
                {"title": "Chef", "company": "Google",                # wrong title
                 "location": "Toronto", "source_site": "indeed", "job_url": "u3"},
            ]
        return base

    monkeypatch.setattr(worker.db, "get_jobs_for_search", jobs_for)
    monkeypatch.setattr(worker.db, "set_saved_search_result", lambda *a: None)
    captured = {}
    monkeypatch.setattr(worker, "_send_alert",
                        lambda ss, sid, fresh: captured.update(fresh=fresh))

    worker._handle_alert(2, 7)
    titles = [j["title"] for j in captured["fresh"]]
    assert titles == ["Software Engineer II"]   # only Google + engineer + new


def test_alerts_create_stores_filters(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(app_module.db, "create_saved_search",
                        lambda uid, rid, label, params, freq, nxt:
                        captured.update(params=params) or 3)
    resp = client.post("/alerts/create", data={
        "resume_id": "3", "frequency": "daily",
        "filter_company": "Google", "filter_title": "engineer",
        "work_type": "any", "country": "Canada",
    })
    assert resp.status_code == 302
    assert captured["params"]["filter_company"] == "Google"
    assert captured["params"]["filter_title"] == "engineer"
    assert "Google" in captured["params"]["keywords"]  # searched, not just filtered


# ---------------------------------------------------------------------------
# BYO cloud AI
# ---------------------------------------------------------------------------
def test_account_ai_requires_acknowledgement(client, monkeypatch):
    monkeypatch.setattr(app_module.db, "upsert_user_settings",
                        lambda uid, **f: pytest.fail("should not save"))
    resp = client.post("/account/ai", data={
        "ai_cloud_enabled": "1", "ai_provider": "openai",
        "ai_api_key": "sk-x", "ai_model": "gpt-4o-mini",
    }, follow_redirects=True)
    assert b"acknowledgement" in resp.data


def test_account_ai_saves_with_acknowledgement(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(app_module.db, "upsert_user_settings",
                        lambda uid, **f: saved.update(f))
    resp = client.post("/account/ai", data={
        "ai_cloud_enabled": "1", "ai_acknowledge": "1", "ai_provider": "anthropic",
        "ai_api_key": "sk-ant-x", "ai_model": "claude-sonnet-5",
    })
    assert resp.status_code == 302
    assert saved["ai_cloud_enabled"] is True
    assert saved["ai_provider"] == "anthropic"


def test_ai_config_injects_user_cloud(client, monkeypatch):
    monkeypatch.setattr(app_module.db, "get_user_settings", lambda uid: {
        "ai_cloud_enabled": 1, "ai_provider": "openai",
        "ai_api_key": "sk-x", "ai_model": "gpt-4o-mini",
    })
    with app.test_request_context("/"):
        with client.session_transaction():
            pass
    # Call within a request context where current_user resolves.
    calls = {}
    from ai_client.settings import is_configured
    from ai_client.tiered import configured_model
    with app.test_request_context("/"):
        from flask import session
        session["user_id"] = 1
        cfg = app_module._ai_config()
    assert cfg["cloud"]["enabled"] is True
    assert configured_model(cfg) == "gpt-4o-mini"   # cloud wins over slots
    assert is_configured(cfg) is True               # AI offered w/o backend slots


# ---------------------------------------------------------------------------
# OCR fallback (graceful when Tesseract/pytesseract absent)
# ---------------------------------------------------------------------------
def test_ocr_degrades_gracefully():
    from resume_parser.extractors.ocr import ocr_available, ocr_document_lines

    assert isinstance(ocr_available(), bool)
    # A bad "document" never raises out of the OCR helper — it returns [].
    assert ocr_document_lines(None) == []

