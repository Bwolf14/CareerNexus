"""
AI connection settings — env-var defaults, overridden by the web UI.

Settings are stored as JSON in the job-results directory (already
volume-mounted in Docker, so they survive container restarts). The
``/settings`` page reads and writes them; environment variables provide the
initial defaults so a deployment can also be configured entirely from
``docker-compose.yml``:

    AI_ENABLED         "1"/"true" to enable on boot (default off)
    AI_BASE_URL        e.g. http://192.168.1.50:11434  (the Ollama PC)
    AI_MODEL           e.g. qwen3:32b
    AI_CONNECT_TIMEOUT seconds to wait for a TCP connect   (default 4)
    AI_READ_TIMEOUT    seconds to wait for a full response (default 180)
    AI_SETTINGS_FILE   where the JSON file lives (default
                       <JOB_RESULTS_DIR>/ai_settings.json)

Precedence: a value saved from the settings page wins over its env default.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional
from urllib.parse import urlparse

from job_scraper.output import results_dir

_TRUTHY = {"1", "true", "yes", "on"}

# Bounds for the timeout fields (seconds) — keeps typos from hanging a worker.
CONNECT_TIMEOUT_RANGE = (1.0, 30.0)
READ_TIMEOUT_RANGE = (10.0, 600.0)


def normalize_base_url(raw: Optional[str]) -> str:
    """Canonicalise a user-entered server address to ``http://host:port/v1``.

    Accepts anything reasonable — ``192.168.1.50:11434``,
    ``http://pc.local:11434/``, ``http://host:1234/v1`` — and returns the
    OpenAI-compatible base (scheme + host + ``/v1``). Returns ``""`` for
    empty input.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        return ""
    path = (parsed.path or "").rstrip("/")
    if not path.endswith("/v1"):
        path = path + "/v1"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def settings_path() -> str:
    return os.environ.get(
        "AI_SETTINGS_FILE", os.path.join(results_dir(), "ai_settings.json")
    )


def _clamp(value: Any, lo: float, hi: float, fallback: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return fallback


def _defaults() -> dict[str, Any]:
    return {
        "enabled": os.environ.get("AI_ENABLED", "").strip().lower() in _TRUTHY,
        "base_url": normalize_base_url(os.environ.get("AI_BASE_URL", "")),
        "model": os.environ.get("AI_MODEL", "").strip(),
        "connect_timeout": _clamp(
            os.environ.get("AI_CONNECT_TIMEOUT"), *CONNECT_TIMEOUT_RANGE, fallback=4.0
        ),
        "read_timeout": _clamp(
            os.environ.get("AI_READ_TIMEOUT"), *READ_TIMEOUT_RANGE, fallback=180.0
        ),
    }


def load_settings() -> dict[str, Any]:
    """Current settings: env defaults overlaid with the saved file (if any)."""
    settings = _defaults()
    try:
        with open(settings_path(), encoding="utf-8") as fh:
            saved = json.load(fh)
    except FileNotFoundError:
        return settings
    except Exception:
        return settings  # unreadable file -> behave like a fresh install

    if isinstance(saved, dict):
        if "enabled" in saved:
            settings["enabled"] = bool(saved["enabled"])
        if "base_url" in saved:
            settings["base_url"] = normalize_base_url(saved["base_url"])
        if "model" in saved:
            settings["model"] = str(saved["model"]).strip()
        if "connect_timeout" in saved:
            settings["connect_timeout"] = _clamp(
                saved["connect_timeout"], *CONNECT_TIMEOUT_RANGE, fallback=4.0
            )
        if "read_timeout" in saved:
            settings["read_timeout"] = _clamp(
                saved["read_timeout"], *READ_TIMEOUT_RANGE, fallback=180.0
            )
    return settings


def save_settings(settings: dict[str, Any]) -> str:
    """Validate + persist settings; returns the path written."""
    clean = {
        "enabled": bool(settings.get("enabled")),
        "base_url": normalize_base_url(settings.get("base_url")),
        "model": str(settings.get("model") or "").strip(),
        "connect_timeout": _clamp(
            settings.get("connect_timeout"), *CONNECT_TIMEOUT_RANGE, fallback=4.0
        ),
        "read_timeout": _clamp(
            settings.get("read_timeout"), *READ_TIMEOUT_RANGE, fallback=180.0
        ),
    }
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2)
    return path


def is_configured(settings: dict[str, Any]) -> bool:
    """True when the AI can actually be called (enabled + URL + model)."""
    return bool(
        settings.get("enabled") and settings.get("base_url") and settings.get("model")
    )
