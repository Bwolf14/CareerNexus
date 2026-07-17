"""Unit tests for the AI client: tiered settings, options, JSON extraction,
model selection, and the product features.

No network involved — ``run_chat`` / ``test_connection`` are monkeypatched.
"""

from __future__ import annotations

import json

import pytest

from ai_client import features, tiered
from ai_client.client import AIClientError, extract_json
from ai_client.settings import (
    is_configured,
    load_settings,
    normalize_base_url,
    save_settings,
    slot_is_configured,
)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.1.50:11434", "http://192.168.1.50:11434"),
        ("http://192.168.1.50:11434", "http://192.168.1.50:11434"),
        ("http://192.168.1.50:11434/", "http://192.168.1.50:11434"),
        ("http://192.168.1.50:11434/v1", "http://192.168.1.50:11434"),  # path stripped
        ("https://gpu-box.local:1234/api", "https://gpu-box.local:1234"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


def test_settings_roundtrip_and_env_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_SETTINGS_FILE", str(tmp_path / "ai_settings.json"))
    monkeypatch.setenv("AI_BASE_URL", "10.0.0.9:11434")
    monkeypatch.setenv("AI_MODEL", "qwen3:4b")
    monkeypatch.setenv("AI_ENABLED", "true")

    # No file yet -> env seeds the primary slot, URL normalised.
    s = load_settings()
    assert s["slots"]["primary"]["enabled"] is True
    assert s["slots"]["primary"]["base_url"] == "http://10.0.0.9:11434"
    assert s["slots"]["primary"]["model"] == "qwen3:4b"
    assert is_configured(s)
    # Thinking is OFF by default.
    assert s["options"]["primary"]["think"]["on"] is False

    # Save a full config: disable primary, enable secondary; clamp timeouts.
    s["slots"]["primary"]["enabled"] = False
    s["slots"]["secondary"] = {"enabled": True, "base_url": "pc.lan:11434",
                               "model": "llama3.2:3b", "use_local": False}
    s["connect_timeout"] = 0.01
    s["read_timeout"] = 99999
    save_settings(s)

    s2 = load_settings()
    assert s2["slots"]["primary"]["enabled"] is False
    assert s2["slots"]["secondary"]["base_url"] == "http://pc.lan:11434"
    assert s2["connect_timeout"] == 1.0
    assert s2["read_timeout"] == 600.0
    assert is_configured(s2)  # secondary is configured


def test_only_one_slot_can_use_local(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_SETTINGS_FILE", str(tmp_path / "ai.json"))
    save_settings({
        "slots": {
            "primary": {"enabled": True, "model": "a", "use_local": True},
            "secondary": {"enabled": True, "model": "b", "use_local": True},
            "tertiary": {"enabled": False, "model": "", "use_local": False},
        }
    })
    s = load_settings()
    locals_on = [n for n in ("primary", "secondary", "tertiary")
                 if s["slots"][n]["use_local"]]
    assert locals_on == ["primary"]  # second local flag dropped


def test_local_slot_is_configured_without_base_url():
    slot = {"enabled": True, "model": "qwen2.5:3b", "use_local": True, "base_url": ""}
    assert slot_is_configured(slot)  # effective URL is the local engine


def test_settings_survive_corrupt_file(tmp_path, monkeypatch):
    path = tmp_path / "ai_settings.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("AI_SETTINGS_FILE", str(path))
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_ENABLED", raising=False)
    s = load_settings()
    assert s["slots"]["primary"]["enabled"] is False


# ---------------------------------------------------------------------------
# per-model options → request params
# ---------------------------------------------------------------------------
def test_build_options_thinking_off_by_default():
    think, keep_alive, options = tiered.build_options({})
    assert think is False           # default: think:false is sent
    assert keep_alive is None


def test_build_options_maps_matrix_to_payload():
    slot_opts = {
        "think": {"on": True},
        "temperature": {"on": True, "value": 0},
        "keep_alive": {"on": True, "value": 15},
        "num_predict": {"on": True, "value": 512},
        "num_ctx": {"on": False, "value": 4096},
        "stop": {"on": True, "value": "###"},
    }
    think, keep_alive, options = tiered.build_options(slot_opts)
    assert think is True
    assert keep_alive == "15m"
    assert options["temperature"] == 0
    assert options["num_predict"] == 512
    assert "num_ctx" not in options       # off -> omitted
    assert options["stop"] == ["###"]


# ---------------------------------------------------------------------------
# tiered slot resolution + safe mode
# ---------------------------------------------------------------------------
def _cfg(**slots):
    base = {"primary": {"enabled": False}, "secondary": {"enabled": False},
            "tertiary": {"enabled": False}}
    base.update(slots)
    return {"slots": base, "options": {}, "connect_timeout": 2, "read_timeout": 30}


def test_resolve_prefers_primary_then_falls_through(monkeypatch):
    cfg = _cfg(
        primary={"enabled": True, "base_url": "http://p:1", "model": "m1"},
        secondary={"enabled": True, "base_url": "http://s:2", "model": "m2"},
    )
    # primary unreachable, secondary ok.
    def fake_test(base, connect_timeout=4.0):
        return {"ok": base == "http://s:2", "models": ["m2"], "error": None}
    monkeypatch.setattr(tiered.client, "test_connection", fake_test)
    monkeypatch.setattr(tiered.client, "has_model", lambda b, m, **kw: True)

    active = tiered.resolve_slot(cfg, probe=True)
    assert active["name"] == "secondary"
    assert active["model"] == "m2"


def test_resolve_returns_none_when_all_fail(monkeypatch):
    cfg = _cfg(primary={"enabled": True, "base_url": "http://p:1", "model": "m1"})
    monkeypatch.setattr(tiered.client, "test_connection",
                        lambda base, connect_timeout=4.0: {"ok": False, "models": [], "error": "x"})
    assert tiered.resolve_slot(cfg, probe=True) is None


def test_run_chat_safe_mode_raises_when_unavailable(monkeypatch):
    cfg = _cfg()  # nothing enabled
    with pytest.raises(AIClientError):
        tiered.run_chat(cfg, [{"role": "user", "content": "hi"}])


def test_run_chat_uses_resolved_slot(monkeypatch):
    cfg = _cfg(primary={"enabled": True, "base_url": "http://p:1", "model": "m1"})
    monkeypatch.setattr(tiered, "resolve_slot",
                        lambda config, probe=True: {"name": "primary", "slot": {}, "options": {},
                                                    "base": "http://p:1", "model": "m1"})
    seen = {}

    def fake_chat(base, model, messages, **kw):
        seen.update(base=base, model=model, kw=kw)
        return "hello"

    monkeypatch.setattr(tiered.client, "chat", fake_chat)
    out = tiered.run_chat(cfg, [{"role": "user", "content": "hi"}])
    assert out == "hello"
    assert seen["model"] == "m1"
    assert seen["kw"]["think"] is False  # default thinking off


# ---------------------------------------------------------------------------
# Cloud (BYO key) routing + per-model timeouts
# ---------------------------------------------------------------------------
def test_run_chat_routes_to_cloud_when_enabled(monkeypatch):
    from ai_client import cloud as cloud_mod
    cfg = _cfg()  # no backend slots at all
    cfg["cloud"] = {"enabled": True, "provider": "openai",
                    "api_key": "sk-x", "model": "gpt-4o-mini"}
    seen = {}
    monkeypatch.setattr(tiered, "chat_cloud",
                        lambda cloud, messages: seen.update(model=cloud["model"]) or "cloud says hi")
    out = tiered.run_chat(cfg, [{"role": "user", "content": "hi"}])
    assert out == "cloud says hi"
    assert seen["model"] == "gpt-4o-mini"


def test_cloud_openai_payload_includes_speed_preprompt(monkeypatch):
    from ai_client import cloud
    captured = {}

    class FakeResp:
        status_code = 200
        ok = True
        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["url"] = url
        captured["body"] = json
        captured["headers"] = headers
        return FakeResp()

    monkeypatch.setattr(cloud.requests, "post", fake_post)
    out = cloud.chat_cloud(
        {"enabled": True, "provider": "openai", "api_key": "sk-x", "model": "gpt-4o-mini"},
        [{"role": "system", "content": "You are X."},
         {"role": "user", "content": "hi"}],
    )
    assert out == "ok"
    assert "openai.com" in captured["url"]
    first = captured["body"]["messages"][0]
    assert first["role"] == "system"
    # The hidden pre-prompt (no thinking, fast) leads the system message.
    assert first["content"].startswith(cloud.SPEED_PROMPT)
    assert "You are X." in first["content"]
    assert captured["headers"]["Authorization"] == "Bearer sk-x"


def test_cloud_anthropic_payload(monkeypatch):
    from ai_client import cloud
    captured = {}

    class FakeResp:
        status_code = 200
        ok = True
        def json(self):
            return {"content": [{"type": "text", "text": "claude ok"}]}

    monkeypatch.setattr(cloud.requests, "post",
                        lambda url, json=None, timeout=None, headers=None:
                        captured.update(url=url, body=json, headers=headers) or FakeResp())
    out = cloud.chat_cloud(
        {"enabled": True, "provider": "anthropic", "api_key": "sk-ant", "model": "claude-sonnet-5"},
        [{"role": "system", "content": "Sys."}, {"role": "user", "content": "hi"}],
    )
    assert out == "claude ok"
    assert "anthropic.com" in captured["url"]
    assert captured["body"]["system"].startswith(cloud.SPEED_PROMPT)
    assert all(m["role"] != "system" for m in captured["body"]["messages"])
    assert captured["headers"]["x-api-key"] == "sk-ant"


def test_per_model_timeouts_used(monkeypatch):
    cfg = _cfg(primary={"enabled": True, "base_url": "http://p:1", "model": "m1"})
    cfg["options"] = {"primary": {
        "connect_timeout": {"on": True, "value": 2},
        "read_timeout": {"on": True, "value": 60},
    }}
    monkeypatch.setattr(
        tiered, "resolve_slot",
        lambda config, probe=True: {"name": "primary", "slot": {},
                                    "options": cfg["options"]["primary"],
                                    "base": "http://p:1", "model": "m1"})
    seen = {}
    monkeypatch.setattr(tiered.client, "chat",
                        lambda base, model, messages, **kw: seen.update(kw) or "x")
    tiered.run_chat(cfg, [{"role": "user", "content": "hi"}])
    assert seen["connect_timeout"] == 2.0
    assert seen["read_timeout"] == 60.0
    # And timeouts never leak into the Ollama sampling options payload.
    assert "connect_timeout" not in (seen.get("options") or {})


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
def test_extract_json_handles_fences_prose_and_think_blocks():
    fenced = 'Sure!\n```json\n[{"prompt": "Q1", "hint": null}]\n```\nHope that helps.'
    assert extract_json(fenced, list) == [{"prompt": "Q1", "hint": None}]

    bare = 'Preamble {"overall": "ok", "jobs": []} trailing'
    assert extract_json(bare, dict)["overall"] == "ok"

    with pytest.raises(AIClientError):
        extract_json("no json here", dict)
    with pytest.raises(AIClientError):
        extract_json('{"a": 1}', list)


# ---------------------------------------------------------------------------
# features (run_chat monkeypatched)
# ---------------------------------------------------------------------------
RESUME = {
    "contact_info": {"name": "Alex", "location": "Calgary, AB", "links": {}},
    "summary": "Network tech.",
    "experience": [{"title": "Network Technician", "company": "Acme",
                    "dates": {"is_current": True}, "description": ["Ran the LAN"]}],
    "education": [], "skills": {"raw": ["Networking", "Python", "Linux", "VLANs", "Cisco IOS"]},
    "certifications": [], "projects": [],
}

CONFIG = {"slots": {"primary": {"enabled": True, "base_url": "http://x", "model": "m"}}}


def test_generate_questions_appends_structured(monkeypatch):
    reply = json.dumps([
        {"prompt": "Tell me about running the LAN at Acme.", "hint": "Specifics help."},
        {"prompt": "Where do you want to be in 5-10 years?", "hint": None},
    ])
    monkeypatch.setattr(features, "run_chat", lambda *a, **kw: reply)

    qs = features.generate_questions(CONFIG, RESUME, jobs=[])
    ids = [q["id"] for q in qs]
    assert ids[:2] == ["ai_0", "ai_1"]
    assert qs[0]["origin"] == "ai" and qs[0]["type"] == "textarea"
    for required in ("preferred_skills", "salary", "work_style"):
        assert required in ids
    assert len(qs) <= 8


def test_generate_questions_rejects_garbage(monkeypatch):
    monkeypatch.setattr(features, "run_chat", lambda *a, **kw: '["not-a-dict", 42]')
    with pytest.raises(AIClientError):
        features.generate_questions(CONFIG, RESUME)


def test_generate_match_analysis_maps_indices(monkeypatch):
    captured = {}

    def fake_run_chat(config, messages, **kw):
        captured["payload"] = json.loads(messages[1]["content"])
        return json.dumps({
            "overall": "Strong local networking market.",
            "jobs": [
                {"n": 1, "analysis": "Great skill fit."},
                {"n": 2, "analysis": "Pay below target."},
                {"n": 99, "analysis": "out of range, dropped"},
            ],
        })

    monkeypatch.setattr(features, "run_chat", fake_run_chat)
    picks = [
        {"job": {"title": "NetTech", "company": "A", "description": "d" * 1000},
         "reasons": ["r"], "matched_skills": ["Python"], "score": 80},
        {"job": {"title": "SysAdmin", "company": "B"}, "reasons": [],
         "matched_skills": [], "score": 40},
    ]
    result = features.generate_match_analysis(CONFIG, RESUME, picks, {"work_style": "Remote"})
    assert result["overall"].startswith("Strong")
    assert result["per_index"] == {0: "Great skill fit.", 1: "Pay below target."}
    assert len(captured["payload"]["shortlist"][0]["description"]) <= 400
    assert captured["payload"]["candidate_preferences"] == {"work_style": "Remote"}
