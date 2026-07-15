"""
Route tests for the guided web flow.

The DB layer and the JobSpy scraper are stubbed with monkeypatch, so these run
without MariaDB or network access — they exercise the Flask routes, templates,
and the wiring into job_matcher.
"""

from __future__ import annotations

import json

import pytest

from webapp import app as app_module
from webapp.app import app

PARSED = {
    "metadata": {
        "source_format": "pdf",
        "parser_version": "test",
        "section_detection_status": "success",
        "extraction_confidence": 0.9,
        "warnings": [],
    },
    "contact_info": {
        "name": "Alex Smith",
        "email": "alex@example.com",
        "phone": None,
        "location": "Calgary, AB",
        "links": {"linkedin": None, "github": None, "portfolio": None, "other": []},
    },
    "summary": "Network technician.",
    "experience": [
        {
            "company": "Acme Corp",
            "title": "Network Technician",
            "location": "Calgary, AB",
            "dates": {"start_date": "2022-01", "end_date": None, "is_current": True},
            "description": ["Maintained switching for 400 users"],
        }
    ],
    "education": [],
    "skills": {"raw": ["Networking", "Cisco IOS", "Python", "Linux", "VLANs"],
               "categorized": {}},
    "certifications": [],
    "projects": [],
    "volunteer_experience": [],
    "additional_sections": {},
    "raw_text": "Alex Smith",
}

JOBS = [
    {
        "source_site": "indeed",
        "external_id": "j1",
        "title": "Network Technician",
        "company": "Northwind",
        "location": "Calgary, AB",
        "job_type": "fulltime",
        "is_remote": False,
        "salary_min": 70000.0,
        "salary_max": 90000.0,
        "salary_currency": "CAD",
        "salary_interval": "yearly",
        "salary_display": "$70,000–$90,000 / yearly",
        "description": "Cisco IOS, VLANs, firewalls. CCNA required.",
        "job_url": "https://example.com/j1",
        "date_posted": "2026-07-01",
        "search_term": "Network Technician",
    }
]

SEARCH_ROW = {
    "id": 7,
    "resume_id": 3,
    "user_id": 1,
    "search_terms": "Network Technician",
    "location": "Calgary, AB",
    "sites_searched": "indeed",
    "source": "jobspy",
    "results_count": 1,
    "ran_at": "2026-07-01 12:00:00",
    "username": "Alex Smith",
}


USER = {"id": 1, "username": "Alex Smith", "email": "alex@example.com"}


def _common_stubs(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.db, "list_resumes",
                        lambda limit=100, user_id=None: [])
    monkeypatch.setattr(app_module.db, "list_job_searches",
                        lambda limit=100, user_id=None: [])
    monkeypatch.setattr(app_module.db, "get_resume_json",
                        lambda rid: PARSED if rid == 3 else None)
    monkeypatch.setattr(app_module.db, "get_resume_owner",
                        lambda rid: 1 if rid == 3 else None)
    monkeypatch.setattr(app_module.db, "save_parsed_resume",
                        lambda parsed, user_id=None: 3)
    monkeypatch.setattr(app_module.db, "get_job_search",
                        lambda sid: dict(SEARCH_ROW) if sid == 7 else None)
    monkeypatch.setattr(app_module.db, "get_jobs_for_search",
                        lambda sid: list(JOBS) if sid == 7 else [])
    monkeypatch.setattr(app_module.db, "save_job_search", lambda *a, **kw: 7)
    monkeypatch.setattr(app_module.db, "get_user_by_id",
                        lambda uid: dict(USER) if uid == 1 else None)
    monkeypatch.setattr(app_module.db, "saved_dedup_keys", lambda uid: set())
    monkeypatch.setattr(app_module.db, "enqueue_scrape", lambda *a, **kw: 55)
    monkeypatch.setattr(app_module.db, "throttle_status", lambda ident: None)
    monkeypatch.setattr(app_module.db, "record_login_failure", lambda *a, **kw: None)
    monkeypatch.setattr(app_module.db, "clear_login_failures", lambda ident: None)
    # Keep plan/cache/settings files in a temp dir instead of ./job_results.
    monkeypatch.setenv("JOB_RESULTS_DIR", str(tmp_path))
    monkeypatch.delenv("AI_ENABLED", raising=False)
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)
    app.config["TESTING"] = True


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """A test client with a logged-in session (user id 1)."""
    _common_stubs(monkeypatch, tmp_path)
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = 1
        yield c


@pytest.fixture()
def anon_client(monkeypatch, tmp_path):
    """A test client with no session — for auth/redirect tests."""
    _common_stubs(monkeypatch, tmp_path)
    with app.test_client() as c:
        yield c


AI_SETTINGS = {"enabled": True, "base_url": "http://pc.lan:11434/v1",
               "model": "qwen3:32b", "connect_timeout": 4.0, "read_timeout": 180.0}


@pytest.fixture()
def ai_client_on(monkeypatch):
    """Pretend a healthy Ollama server is configured."""
    monkeypatch.setattr(app_module, "load_settings", lambda: dict(AI_SETTINGS))
    return AI_SETTINGS


def test_home_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Upload your resume" in resp.data


def test_home_survives_db_outage(client, monkeypatch):
    def boom(limit=100, user_id=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(app_module.db, "list_resumes", boom)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"db down" in resp.data


def test_profile_shows_parsed_resume(client):
    resp = client.get("/profile/3")
    assert resp.status_code == 200
    assert b"Alex Smith" in resp.data
    assert b"Network Technician" in resp.data
    assert b"Cisco IOS" in resp.data


def test_profile_404_for_unknown_resume(client):
    assert client.get("/profile/999").status_code == 404


def test_search_enqueues_async_by_default(client):
    # SCRAPE_ASYNC defaults on: the search is queued and we go to the progress page.
    resp = client.post("/profile/3/search", data={"work_type": "any", "country": "Canada"})
    assert resp.status_code == 302
    assert "/scrape/55" in resp.headers["Location"]


def test_search_sync_when_async_disabled(client, monkeypatch):
    monkeypatch.setenv("SCRAPE_ASYNC", "0")
    monkeypatch.setattr(
        app_module.search_service,
        "scrape_jobs_for_queries",
        lambda queries, **kw: {"jobs": list(JOBS), "source": "jobspy",
                               "sites": kw.get("sites") or ["indeed"], "errors": []},
    )
    resp = client.post("/profile/3/search", data={"work_type": "any", "country": "Canada"})
    assert resp.status_code == 302
    assert "/matches/7" in resp.headers["Location"]


def test_scrape_status_redirects_when_done(client, monkeypatch):
    monkeypatch.setattr(
        app_module.db, "get_scrape_job",
        lambda jid: {"id": jid, "user_id": 1, "resume_id": 3,
                     "status": "done", "search_id": 7, "error": None},
    )
    resp = client.get("/scrape/55")
    assert resp.status_code == 302
    assert "/matches/7" in resp.headers["Location"]


def test_scrape_status_shows_progress_while_pending(client, monkeypatch):
    monkeypatch.setattr(
        app_module.db, "get_scrape_job",
        lambda jid: {"id": jid, "user_id": 1, "resume_id": 3,
                     "status": "pending", "search_id": None, "error": None},
    )
    resp = client.get("/scrape/55")
    assert resp.status_code == 200
    assert b"Searching live job boards" in resp.data


def test_matches_page_lists_jobs(client):
    resp = client.get("/matches/7")
    assert resp.status_code == 200
    assert b"Northwind" in resp.data
    assert b"Continue: tell us more" in resp.data


def test_questions_are_resume_grounded(client):
    resp = client.get("/questions/7")
    assert resp.status_code == 200
    assert b"Acme Corp" in resp.data          # template question uses the resume
    assert b"5\xe2\x80\x9310 years" in resp.data  # standard question ("5–10 years")
    assert b"AI interviewer available" in resp.data  # AI-disabled banner


def test_answers_flow_into_recommendations(client):
    resp = client.post(
        "/questions/7",
        data={
            "five_year": "Team lead",
            "work_style": "Remote",
            "salary_min": "70000",
            "salary_max": "95000",
            "salary_interval": "yearly",
            "preferred_skills": ["Python", "Linux"],
        },
    )
    assert resp.status_code == 302
    assert "/recommendations/7" in resp.headers["Location"]

    resp = client.get("/recommendations/7")
    assert resp.status_code == 200
    assert b"Top picks" in resp.data
    assert b"MATCH SCORE" in resp.data
    assert b"What you told us" in resp.data
    assert b"Team lead" in resp.data
    # certification gap analysis found CCNA in the posting text
    assert b"Cisco CCNA" in resp.data


def test_recommendations_without_answers_show_skip_notice(client, tmp_path):
    resp = client.get("/recommendations/7")
    assert resp.status_code == 200
    assert b"skipped the follow-up questions" in resp.data


def test_skip_does_not_store_answers(client):
    resp = client.post("/questions/7", data={"skip": "1", "five_year": "ignored"})
    assert resp.status_code == 302
    resp = client.get("/recommendations/7")
    assert b"skipped the follow-up questions" in resp.data


def test_legacy_routes_redirect(client):
    assert client.get("/jobs").status_code == 302
    resp = client.get("/jobs/7")
    assert resp.status_code == 302
    assert "/matches/7" in resp.headers["Location"]


def test_api_jobs_payload(client):
    resp = client.get("/api/jobs/7")
    assert resp.status_code == 200
    payload = json.loads(resp.data)
    assert payload["job_count"] == 1
    assert payload["jobs"][0]["company"] == "Northwind"


def test_health_degraded_without_db(client, monkeypatch):
    def no_conn():
        raise RuntimeError("no db")
    monkeypatch.setattr(app_module.db, "get_connection", no_conn)
    resp = client.get("/health")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# AI settings page + AI-enabled flow
# ---------------------------------------------------------------------------
def test_settings_page_renders_and_saves(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert b"AI features are OFF" in resp.data

    resp = client.post(
        "/settings",
        data={"enabled": "1", "base_url": "192.168.1.50:11434", "model": "qwen3:32b",
              "connect_timeout": "4", "read_timeout": "180"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"AI settings saved" in resp.data
    assert b"AI features are ON" in resp.data
    assert b"http://192.168.1.50:11434/v1" in resp.data  # URL normalised


def test_settings_rejects_enable_without_model(client):
    resp = client.post(
        "/settings",
        data={"enabled": "1", "base_url": "192.168.1.50:11434", "model": ""},
    )
    assert resp.status_code == 400
    assert b"Pick a model" in resp.data


def test_api_ai_test_endpoint(client, monkeypatch):
    monkeypatch.setattr(
        app_module, "test_connection",
        lambda s: {"ok": True, "latency_ms": 42, "models": ["qwen3:32b"], "error": None},
    )
    resp = client.post("/api/ai/test", json={"base_url": "pc.lan:11434"})
    data = json.loads(resp.data)
    assert data["ok"] is True
    assert data["normalized_url"] == "http://pc.lan:11434/v1"
    assert "qwen3:32b" in data["models"]


def test_questions_use_ai_and_post_maps_to_same_list(client, ai_client_on, monkeypatch):
    ai_questions = [
        {"id": "ai_0", "prompt": "Tell me about the LAN rebuild at Acme Corp.",
         "type": "textarea", "options": [], "hint": None, "origin": "ai"},
        {"id": "salary", "prompt": "What is your ideal, realistic pay range?",
         "type": "salary", "options": [], "hint": None, "origin": "standard"},
    ]
    calls = {"n": 0}

    def fake_generate(settings, parsed, jobs=None):
        calls["n"] += 1
        return [dict(q) for q in ai_questions]

    monkeypatch.setattr(app_module, "generate_questions", fake_generate)

    resp = client.get("/questions/7")
    assert resp.status_code == 200
    assert b"AI interviewer active" in resp.data
    assert b"LAN rebuild at Acme Corp" in resp.data
    assert calls["n"] == 1

    # Second GET is served from the cache — no second generation.
    resp = client.get("/questions/7")
    assert b"LAN rebuild at Acme Corp" in resp.data
    assert calls["n"] == 1

    # POST maps the answer onto the cached AI question id.
    resp = client.post("/questions/7", data={"ai_0": "I led the cutover."})
    assert resp.status_code == 302
    from webapp import plan_store
    assert plan_store.load_answers(7)["ai_0"] == "I led the cutover."

    # ...and the plan page's recap shows the AI question with its answer.
    from ai_client import AIClientError
    monkeypatch.setattr(
        app_module, "generate_match_analysis",
        lambda *a, **kw: (_ for _ in ()).throw(AIClientError("off")),
    )
    resp = client.get("/recommendations/7")
    assert b"LAN rebuild at Acme Corp" in resp.data
    assert b"I led the cutover." in resp.data


def test_questions_fall_back_when_ai_fails(client, ai_client_on, monkeypatch):
    from ai_client import AIClientError

    def boom(settings, parsed, jobs=None):
        raise AIClientError("server asleep")

    monkeypatch.setattr(app_module, "generate_questions", boom)
    resp = client.get("/questions/7")
    assert resp.status_code == 200
    assert b"AI unavailable" in resp.data
    assert b"server asleep" in resp.data
    # Template questions still shown (grounded in the stubbed resume).
    assert b"Acme Corp" in resp.data


def test_recommendations_attach_ai_analysis_and_cache(client, ai_client_on, monkeypatch):
    calls = {"n": 0}

    def fake_analysis(settings, parsed, picks, answers=None):
        calls["n"] += 1
        return {"overall": "A healthy local market for network roles.",
                "per_index": {0: "Excellent skill and pay alignment."}}

    monkeypatch.setattr(app_module, "generate_match_analysis", fake_analysis)

    resp = client.get("/recommendations/7")
    assert resp.status_code == 200
    assert b"AI analysis by" in resp.data
    assert b"healthy local market" in resp.data
    assert b"Excellent skill and pay alignment" in resp.data
    assert calls["n"] == 1

    # Cached on revisit (same answers, same shortlist).
    client.get("/recommendations/7")
    assert calls["n"] == 1

    # ?regen=1 forces a fresh generation.
    client.get("/recommendations/7?regen=1")
    assert calls["n"] == 2


def test_recommendations_survive_ai_failure(client, ai_client_on, monkeypatch):
    from ai_client import AIClientError

    def boom(settings, parsed, picks, answers=None):
        raise AIClientError("model not found")

    monkeypatch.setattr(app_module, "generate_match_analysis", boom)
    resp = client.get("/recommendations/7")
    assert resp.status_code == 200
    assert b"AI analysis unavailable" in resp.data
    assert b"Top picks" in resp.data  # heuristic shortlist still renders


# ---------------------------------------------------------------------------
# Authentication + consent gate
# ---------------------------------------------------------------------------
def test_anonymous_home_redirects_to_login(anon_client):
    resp = anon_client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_anonymous_cannot_upload(anon_client):
    resp = anon_client.post("/upload")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_page_renders(anon_client):
    resp = anon_client.get("/login")
    assert resp.status_code == 200
    assert b"Welcome back" in resp.data


def test_register_requires_consent(anon_client, monkeypatch):
    created = {"n": 0}

    def fake_create(email, pw_hash, consent):
        created["n"] += 1
        return 5

    monkeypatch.setattr(app_module.db, "create_user", fake_create)
    resp = anon_client.post(
        "/register",
        data={"email": "new@example.com", "password": "hunter2hunter",
              "confirm": "hunter2hunter"},  # consent checkbox NOT sent
    )
    assert resp.status_code == 400
    assert b"must consent" in resp.data
    assert created["n"] == 0  # no account created without consent


def test_register_with_consent_creates_account_and_logs_in(anon_client, monkeypatch):
    captured = {}

    def fake_create(email, pw_hash, consent):
        captured["email"] = email
        captured["consent"] = consent
        return 5

    monkeypatch.setattr(app_module.db, "create_user", fake_create)
    monkeypatch.setattr(app_module.db, "get_user_by_id",
                        lambda uid: {"id": 5, "username": "new", "email": "new@example.com"})
    resp = anon_client.post(
        "/register",
        data={"email": "new@example.com", "password": "hunter2hunter",
              "confirm": "hunter2hunter", "consent": "1"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    assert captured["consent"] is True
    with anon_client.session_transaction() as sess:
        assert sess["user_id"] == 5


def test_register_rejects_short_password(anon_client, monkeypatch):
    monkeypatch.setattr(app_module.db, "create_user",
                        lambda *a, **kw: pytest.fail("should not be called"))
    resp = anon_client.post(
        "/register",
        data={"email": "new@example.com", "password": "short",
              "confirm": "short", "consent": "1"},
    )
    assert resp.status_code == 400
    assert b"at least 8 characters" in resp.data


def test_login_success_sets_session(anon_client, monkeypatch):
    from webapp import auth as auth_module
    pw_hash = auth_module.hash_password("hunter2hunter")
    monkeypatch.setattr(
        app_module.db, "get_user_by_email",
        lambda email: {"id": 9, "username": "u", "email": email,
                       "password_hash": pw_hash},
    )
    resp = anon_client.post(
        "/login", data={"email": "u@example.com", "password": "hunter2hunter"}
    )
    assert resp.status_code == 302
    with anon_client.session_transaction() as sess:
        assert sess["user_id"] == 9


def test_login_rejects_bad_password(anon_client, monkeypatch):
    from webapp import auth as auth_module
    pw_hash = auth_module.hash_password("correct-horse")
    monkeypatch.setattr(
        app_module.db, "get_user_by_email",
        lambda email: {"id": 9, "username": "u", "email": email,
                       "password_hash": pw_hash},
    )
    resp = anon_client.post(
        "/login", data={"email": "u@example.com", "password": "wrong"}
    )
    assert resp.status_code == 401
    assert b"Incorrect email or password" in resp.data


def test_cannot_view_another_users_search(client, monkeypatch):
    # Search owned by a different account (user_id 2, not the logged-in user 1).
    other = dict(SEARCH_ROW)
    other["user_id"] = 2
    monkeypatch.setattr(app_module.db, "get_job_search", lambda sid: other)
    assert client.get("/matches/7").status_code == 403


def test_logout_clears_session(client):
    resp = client.post("/logout")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess
