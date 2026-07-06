"""Unit tests for the AI client: settings, JSON extraction, and features.

No network involved — ``chat`` is monkeypatched for the feature tests.
"""

from __future__ import annotations

import json

import pytest

from ai_client import features
from ai_client.client import AIClientError, extract_json
from ai_client.settings import (
    is_configured,
    load_settings,
    normalize_base_url,
    save_settings,
)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.1.50:11434", "http://192.168.1.50:11434/v1"),
        ("http://192.168.1.50:11434", "http://192.168.1.50:11434/v1"),
        ("http://192.168.1.50:11434/", "http://192.168.1.50:11434/v1"),
        ("http://192.168.1.50:11434/v1", "http://192.168.1.50:11434/v1"),
        ("http://192.168.1.50:11434/v1/", "http://192.168.1.50:11434/v1"),
        ("https://gpu-box.local:1234/v1", "https://gpu-box.local:1234/v1"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


def test_settings_roundtrip_and_env_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_SETTINGS_FILE", str(tmp_path / "ai_settings.json"))
    monkeypatch.setenv("AI_BASE_URL", "10.0.0.9:11434")
    monkeypatch.setenv("AI_MODEL", "qwen3:32b")
    monkeypatch.setenv("AI_ENABLED", "true")

    # No file yet -> env defaults, with URL normalised.
    s = load_settings()
    assert s["enabled"] is True
    assert s["base_url"] == "http://10.0.0.9:11434/v1"
    assert s["model"] == "qwen3:32b"

    # Saved values win over env defaults; timeouts are clamped.
    save_settings(
        {"enabled": False, "base_url": "pc.lan:11434", "model": "llama3:8b",
         "connect_timeout": 0.01, "read_timeout": 99999}
    )
    s = load_settings()
    assert s["enabled"] is False
    assert s["base_url"] == "http://pc.lan:11434/v1"
    assert s["model"] == "llama3:8b"
    assert s["connect_timeout"] == 1.0
    assert s["read_timeout"] == 600.0
    assert not is_configured(s)  # disabled
    s["enabled"] = True
    assert is_configured(s)


def test_settings_survive_corrupt_file(tmp_path, monkeypatch):
    path = tmp_path / "ai_settings.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("AI_SETTINGS_FILE", str(path))
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_ENABLED", raising=False)
    s = load_settings()
    assert s["enabled"] is False


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
def test_extract_json_handles_fences_prose_and_think_blocks():
    fenced = 'Sure! Here you go:\n```json\n[{"prompt": "Q1", "hint": null}]\n```\nHope that helps.'
    assert extract_json(fenced, list) == [{"prompt": "Q1", "hint": None}]

    bare = 'Preamble text {"overall": "ok", "jobs": []} trailing words'
    assert extract_json(bare, dict)["overall"] == "ok"

    with pytest.raises(AIClientError):
        extract_json("no json here at all", dict)
    with pytest.raises(AIClientError):
        extract_json('{"a": 1}', list)  # right JSON, wrong type


# ---------------------------------------------------------------------------
# features (chat monkeypatched)
# ---------------------------------------------------------------------------
RESUME = {
    "contact_info": {"name": "Alex", "location": "Calgary, AB", "links": {}},
    "summary": "Network tech.",
    "experience": [{"title": "Network Technician", "company": "Acme",
                    "dates": {"is_current": True}, "description": ["Ran the LAN"]}],
    "education": [],
    "skills": {"raw": ["Networking", "Python", "Linux", "VLANs", "Cisco IOS"]},
    "certifications": [],
    "projects": [],
}

SETTINGS = {"enabled": True, "base_url": "http://x/v1", "model": "m",
            "connect_timeout": 2, "read_timeout": 30}


def test_generate_questions_appends_structured(monkeypatch):
    reply = json.dumps([
        {"prompt": "Tell me about running the LAN at Acme.", "hint": "Specifics help."},
        {"prompt": "Where do you want to be in 5-10 years?", "hint": None},
    ])
    monkeypatch.setattr(features, "chat", lambda *a, **kw: reply)

    qs = features.generate_questions(SETTINGS, RESUME, jobs=[])
    ids = [q["id"] for q in qs]
    assert ids[:2] == ["ai_0", "ai_1"]
    assert qs[0]["origin"] == "ai" and qs[0]["type"] == "textarea"
    # Machine-usable questions the ranking depends on are always appended:
    for required in ("preferred_skills", "salary", "work_style"):
        assert required in ids
    assert len(qs) <= 8


def test_generate_questions_rejects_garbage(monkeypatch):
    monkeypatch.setattr(features, "chat", lambda *a, **kw: '["not-a-dict", 42]')
    with pytest.raises(AIClientError):
        features.generate_questions(SETTINGS, RESUME)


def test_generate_match_analysis_maps_indices(monkeypatch):
    captured = {}

    def fake_chat(settings, messages, **kw):
        captured["payload"] = json.loads(messages[1]["content"])
        return json.dumps({
            "overall": "Strong local networking market.",
            "jobs": [
                {"n": 1, "analysis": "Great skill fit."},
                {"n": 2, "analysis": "Pay below target."},
                {"n": 99, "analysis": "out of range, dropped"},
            ],
        })

    monkeypatch.setattr(features, "chat", fake_chat)
    picks = [
        {"job": {"title": "NetTech", "company": "A", "description": "d" * 1000},
         "reasons": ["r"], "matched_skills": ["Python"], "score": 80},
        {"job": {"title": "SysAdmin", "company": "B"}, "reasons": [],
         "matched_skills": [], "score": 40},
    ]
    result = features.generate_match_analysis(SETTINGS, RESUME, picks, {"work_style": "Remote"})
    assert result["overall"].startswith("Strong")
    assert result["per_index"] == {0: "Great skill fit.", 1: "Pay below target."}
    # description is trimmed before prompting
    assert len(captured["payload"]["shortlist"][0]["description"]) <= 400
    assert captured["payload"]["candidate_preferences"] == {"work_style": "Remote"}
