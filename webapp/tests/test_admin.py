"""
Tests for the admin portal (webapp/admin_app.py) — a separate Flask app.

DB is stubbed with monkeypatch. The bootstrap-admin hook is neutralised so it
doesn't try to reach a real database on each request.
"""

from __future__ import annotations

import json

import pytest

from webapp import admin_app as adm
from webapp.admin_app import admin_app

ADMIN = {"id": 1, "username": "admin", "email": "admin@careernexus.local",
         "password_hash": "x", "is_admin": 1}


@pytest.fixture(autouse=True)
def _no_bootstrap(monkeypatch):
    # Don't hit the DB to bootstrap the default admin during tests.
    monkeypatch.setattr(adm, "_bootstrap_admin", lambda: None)
    admin_app.config["TESTING"] = True


@pytest.fixture()
def admin_client(monkeypatch):
    monkeypatch.setattr(adm.db, "get_user_by_id",
                        lambda uid: dict(ADMIN) if uid == 1 else None)
    with admin_app.test_client() as c:
        with c.session_transaction() as sess:
            sess["admin_user_id"] = 1
        yield c


@pytest.fixture()
def anon_client(monkeypatch):
    monkeypatch.setattr(adm.db, "throttle_status", lambda ident: None)
    monkeypatch.setattr(adm.db, "record_login_failure", lambda *a, **kw: None)
    monkeypatch.setattr(adm.db, "clear_login_failures", lambda ident: None)
    with admin_app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_dashboard_requires_login(anon_client):
    resp = anon_client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_admin_login_success(anon_client, monkeypatch):
    from webapp import auth
    pw = auth.hash_password("admin")
    monkeypatch.setattr(
        adm.db, "get_admin_by_login",
        lambda ident: {"id": 1, "username": "admin", "email": "a@b.c",
                       "password_hash": pw, "is_admin": 1},
    )
    resp = anon_client.post("/login", data={"identifier": "admin", "password": "admin"})
    assert resp.status_code == 302
    with anon_client.session_transaction() as sess:
        assert sess["admin_user_id"] == 1


def test_admin_login_rejects_non_admin(anon_client, monkeypatch):
    # get_admin_by_login only returns admins; a normal account yields None.
    monkeypatch.setattr(adm.db, "get_admin_by_login", lambda ident: None)
    resp = anon_client.post("/login", data={"identifier": "joe@x.com", "password": "pw"})
    assert resp.status_code == 401


def test_non_admin_session_denied(anon_client, monkeypatch):
    # A session pointing at a non-admin user is treated as logged out.
    monkeypatch.setattr(adm.db, "get_user_by_id",
                        lambda uid: {"id": 2, "username": "joe", "is_admin": 0})
    with anon_client.session_transaction() as sess:
        sess["admin_user_id"] = 2
    resp = anon_client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# Dashboard + users
# ---------------------------------------------------------------------------
def test_dashboard_shows_user_count(admin_client, monkeypatch):
    monkeypatch.setattr(adm.db, "count_users", lambda: 42)
    monkeypatch.setattr(adm.db, "count_admins", lambda: 3)
    monkeypatch.setattr(adm.db, "list_resumes", lambda limit=100000: [])
    monkeypatch.setattr(adm.db, "list_job_searches", lambda limit=100000: [])
    resp = admin_client.get("/")
    assert resp.status_code == 200
    assert b"42" in resp.data
    assert b"registered accounts" in resp.data


def test_users_list_and_search(admin_client, monkeypatch):
    captured = {}

    def fake_list(search=None, limit=500):
        captured["search"] = search
        return [{"id": 2, "username": "joe", "email": "joe@x.com",
                 "first_name": "Joe", "last_name": "Bloggs", "is_admin": 0,
                 "created_at": "2026-07-01"}]

    monkeypatch.setattr(adm.db, "list_all_users", fake_list)
    resp = admin_client.get("/users?q=joe")
    assert resp.status_code == 200
    assert b"joe@x.com" in resp.data
    assert b"Joe Bloggs" in resp.data
    assert captured["search"] == "joe"


def test_promote_user_to_admin(admin_client, monkeypatch):
    captured = {}
    monkeypatch.setattr(adm.db, "set_user_admin",
                        lambda uid, val: captured.update(uid=uid, val=val) or True)
    resp = admin_client.post("/users/2/admin", data={"is_admin": "1"})
    assert resp.status_code == 302
    assert captured == {"uid": 2, "val": True}


def test_cannot_revoke_last_admin(admin_client, monkeypatch):
    monkeypatch.setattr(adm.db, "count_admins", lambda: 1)
    monkeypatch.setattr(adm.db, "is_user_admin", lambda uid: True)
    called = {"n": 0}
    monkeypatch.setattr(adm.db, "set_user_admin",
                        lambda uid, val: called.__setitem__("n", called["n"] + 1))
    resp = admin_client.post("/users/1/admin", data={"is_admin": "0"},
                             follow_redirects=True)
    assert b"last remaining admin" in resp.data
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Settings (tiered AI + local models + email)
# ---------------------------------------------------------------------------
@pytest.fixture()
def _settings_stubs(monkeypatch):
    # Keep the settings page off the network + off the DB.
    monkeypatch.setattr(adm.db, "get_app_setting", lambda k: None)
    monkeypatch.setattr(adm, "list_models", lambda base: [])
    monkeypatch.setattr(adm, "resources",
                        lambda: {"ram_total_gb": 16.0, "ram_available_gb": 12.0, "cpu_count": 8})


def test_settings_page_renders(admin_client, _settings_stubs):
    resp = admin_client.get("/settings")
    assert resp.status_code == 200
    assert b"Model slots" in resp.data
    assert b"Per-model options" in resp.data
    assert b"Local models" in resp.data
    assert b"Email (SMTP)" in resp.data
    assert b"Thinking" in resp.data              # matrix row
    assert b"12.0 GB" in resp.data               # detected RAM


def test_save_email_settings(admin_client, monkeypatch):
    saved = {}
    monkeypatch.setattr(adm.db, "set_app_settings", lambda vals: saved.update(vals))
    resp = admin_client.post(
        "/settings",
        data={"section": "email", "smtp_host": "smtp.example.com",
              "smtp_port": "587", "smtp_from": "no-reply@example.com",
              "smtp_use_tls": "1", "app_base_url": "http://host:8000"},
    )
    assert resp.status_code == 302
    assert saved["smtp_host"] == "smtp.example.com"
    assert saved["smtp_use_tls"] == "1"


def test_save_ai_settings_persists_slots(admin_client, monkeypatch, tmp_path):
    monkeypatch.setenv("AI_SETTINGS_FILE", str(tmp_path / "ai.json"))
    saved = {}
    monkeypatch.setattr(adm, "save_settings",
                        lambda cfg: saved.update(cfg) or "path")
    resp = admin_client.post("/settings", data={
        "section": "ai",
        "slot_primary_enabled": "1",
        "slot_primary_base_url": "192.168.1.9:11434",
        "slot_primary_model": "qwen2.5:3b",
        "opt_primary_think_on": "",          # thinking off
        "opt_primary_temperature_on": "1",
        "opt_primary_temperature_value": "0",
    })
    assert resp.status_code == 302
    assert saved["slots"]["primary"]["enabled"] is True
    assert saved["slots"]["primary"]["model"] == "qwen2.5:3b"
    assert saved["options"]["primary"]["temperature"]["on"] is True
    assert saved["options"]["primary"]["think"]["on"] is False


def test_admin_ai_test_endpoint(admin_client, monkeypatch):
    monkeypatch.setattr(
        adm, "test_connection",
        lambda base: {"ok": True, "latency_ms": 10, "models": ["qwen2.5:3b"], "error": None},
    )
    resp = admin_client.post("/api/ai/test", json={"base_url": "pc.lan:11434"})
    data = json.loads(resp.data)
    assert data["ok"] is True
    assert data["normalized_url"] == "http://pc.lan:11434"   # native API, no /v1


def test_models_pull_streams_progress(admin_client, monkeypatch):
    monkeypatch.setattr(
        adm, "pull_model",
        lambda base, model: iter([
            {"status": "pulling", "total": 100, "completed": 50},
            {"status": "success"},
        ]),
    )
    resp = admin_client.post("/api/models/pull", json={"model": "qwen2.5:3b"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '"completed": 50' in body
    assert '"status": "success"' in body


def test_models_installed(admin_client, monkeypatch):
    monkeypatch.setattr(adm, "list_models", lambda base: ["qwen2.5:3b", "llama3.2:1b"])
    resp = admin_client.get("/api/models/installed")
    data = json.loads(resp.data)
    assert "qwen2.5:3b" in data["models"]


# ---------------------------------------------------------------------------
# Job-search settings (default depth)
# ---------------------------------------------------------------------------
def test_save_search_depth_default(admin_client, monkeypatch):
    saved = {}
    monkeypatch.setattr(adm.db, "set_app_settings", lambda values: saved.update(values))
    resp = admin_client.post(
        "/settings", data={"section": "search", "search_depth_default": "deep"}
    )
    assert resp.status_code == 302
    assert saved == {"search_depth_default": "deep"}


def test_save_search_depth_rejects_unknown_value(admin_client, monkeypatch):
    def boom(values):
        raise AssertionError("should not be saved")
    monkeypatch.setattr(adm.db, "set_app_settings", boom)
    resp = admin_client.post(
        "/settings", data={"section": "search", "search_depth_default": "bogus"}
    )
    assert resp.status_code == 302  # redirects back with an error flash
