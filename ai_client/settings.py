"""
Tiered AI settings — primary / secondary / tertiary Ollama model slots, each
with its own connection and a matrix of per-model options.

Before each prompt the app tries the slots in order (primary → secondary →
tertiary), runs a quick connectivity test, and uses the first that answers; if
none are enabled/reachable it falls back to safe mode (no AI). See
:mod:`ai_client.tiered`.

Settings are stored as JSON in the job-results directory (volume-mounted in
Docker, so they survive restarts) and edited from the **admin portal** only.
Environment variables seed the *primary* slot's defaults so a deployment can be
pre-configured from ``docker-compose.yml``:

    AI_ENABLED      "1"/"true" to enable the primary slot on boot (default off)
    AI_BASE_URL     e.g. http://192.168.1.50:11434  (a networked Ollama server)
    AI_MODEL        e.g. qwen3:4b
    OLLAMA_LOCAL_URL  base URL of the bundled local Ollama (default the
                      ``ollama`` compose service at http://ollama:11434)
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional
from urllib.parse import urlparse

from job_scraper.output import results_dir

_TRUTHY = {"1", "true", "yes", "on"}

SLOTS = ("primary", "secondary", "tertiary")

# The bundled local Ollama engine (the ``ollama`` service in docker-compose).
# A slot with use_local=True talks to this instead of its own base_url.
LOCAL_OLLAMA_URL = os.environ.get("OLLAMA_LOCAL_URL", "http://ollama:11434")

CONNECT_TIMEOUT_RANGE = (1.0, 30.0)
READ_TIMEOUT_RANGE = (10.0, 600.0)

# The per-model options matrix. Each row is one setting; the admin toggles it
# per slot, and value-type rows expose a number/text box. ``on`` maps to the
# request payload as described in ``apply`` (see ai_client.tiered).
#
# kind: "toggle" (no value) or "value" (needs a value when on)
OPTION_SPECS: list[dict[str, Any]] = [
    {
        "key": "think", "label": "Thinking", "kind": "toggle",
        "default_on": False,
        "desc": "Lets 'reasoning' models (Qwen3, DeepSeek-R1, …) think before "
                "answering. When OFF we send \"think\": false so the model "
                "replies directly.",
        "recommend": "Leave OFF. Career Nexus only needs short JSON/text answers; "
                     "thinking is much slower and can wrap replies in reasoning "
                     "that breaks parsing. Turn ON only to debug odd answers.",
    },
    {
        "key": "temperature", "label": "Temperature", "kind": "value",
        "value_type": "float", "default_on": True, "default_value": 0.0,
        "min": 0.0, "max": 2.0, "step": 0.1,
        "desc": "Randomness of the output. 0 = deterministic and focused; higher "
                "= more varied/creative.",
        "recommend": "Keep ON at 0 (or ≤0.3). Low temperature gives reliable, "
                     "consistent JSON. Raise it only if you want more varied "
                     "wording in the written analysis.",
    },
    {
        "key": "keep_alive", "label": "Keep model loaded", "kind": "value",
        "value_type": "int", "default_on": False, "default_value": 10,
        "min": 0, "max": 1440, "unit": "minutes",
        "desc": "How long the model stays in memory after a request, avoiding a "
                "slow reload on the next prompt.",
        "recommend": "Turn ON (10–30 min) on a dedicated server so the first "
                     "request after idle isn't slow. Leave OFF on a shared or "
                     "low-RAM machine so the model unloads and frees memory.",
    },
    {
        "key": "num_predict", "label": "Max response tokens", "kind": "value",
        "value_type": "int", "default_on": True, "default_value": 1024,
        "min": 64, "max": 8192, "unit": "tokens",
        "desc": "Hard cap on how many tokens the model generates in one reply.",
        "recommend": "Keep ON (~1024). Caps runaway generations so a stuck model "
                     "can't hang a request. Raise it only if analyses get cut off.",
    },
    {
        "key": "num_ctx", "label": "Context window", "kind": "value",
        "value_type": "int", "default_on": False, "default_value": 4096,
        "min": 512, "max": 32768, "unit": "tokens",
        "desc": "Size of the model's context window — how much of the resume + "
                "postings it can read at once.",
        "recommend": "Leave OFF (use the model default) unless long resumes get "
                     "truncated. Bigger contexts use noticeably more RAM — costly "
                     "on a CPU-only server.",
    },
    {
        "key": "top_p", "label": "Top-p (nucleus)", "kind": "value",
        "value_type": "float", "default_on": False, "default_value": 0.9,
        "min": 0.0, "max": 1.0, "step": 0.05,
        "desc": "Samples only from the smallest set of tokens whose probability "
                "adds up to p. Lower = more focused.",
        "recommend": "Leave OFF unless you're tuning output style. Temperature is "
                     "the simpler knob; only touch top-p if you know why.",
    },
    {
        "key": "top_k", "label": "Top-k", "kind": "value",
        "value_type": "int", "default_on": False, "default_value": 40,
        "min": 1, "max": 200,
        "desc": "Considers only the k most-likely next tokens at each step. "
                "Lower = more focused/repetitive.",
        "recommend": "Leave OFF (model default). Advanced tuning only.",
    },
    {
        "key": "seed", "label": "Fixed seed", "kind": "value",
        "value_type": "int", "default_on": False, "default_value": 0,
        "min": 0, "max": 2147483647,
        "desc": "Fixes the random seed so the same input gives the same output "
                "(with temperature 0).",
        "recommend": "Leave OFF in normal use. Turn ON only to reproduce a "
                     "specific result while debugging.",
    },
    {
        "key": "stop", "label": "Stop sequence", "kind": "value",
        "value_type": "str", "default_on": False, "default_value": "",
        "desc": "Text that, once generated, stops the response immediately.",
        "recommend": "Leave OFF. Advanced use only — a wrong value can truncate "
                     "answers.",
    },
]

_SPEC_BY_KEY = {s["key"]: s for s in OPTION_SPECS}


def normalize_base_url(raw: Optional[str]) -> str:
    """Canonicalise a user-entered Ollama address to ``http://host:port``.

    Accepts ``192.168.1.50:11434``, ``http://pc.local:11434/``, etc. Strips any
    trailing ``/v1`` or ``/api`` path (we call the native ``/api/*`` endpoints).
    Returns ``""`` for empty input.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def settings_path() -> str:
    return os.environ.get(
        "AI_SETTINGS_FILE", os.path.join(results_dir(), "ai_settings.json")
    )


def _clamp(value: Any, lo: float, hi: float, fallback: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return fallback


def _default_options() -> dict[str, dict[str, Any]]:
    opts: dict[str, dict[str, Any]] = {}
    for spec in OPTION_SPECS:
        entry: dict[str, Any] = {"on": bool(spec["default_on"])}
        if spec["kind"] == "value":
            entry["value"] = spec["default_value"]
        opts[spec["key"]] = entry
    return opts


def _default_slot(name: str) -> dict[str, Any]:
    if name == "primary":
        return {
            "enabled": os.environ.get("AI_ENABLED", "").strip().lower() in _TRUTHY,
            "base_url": normalize_base_url(os.environ.get("AI_BASE_URL", "")),
            "model": os.environ.get("AI_MODEL", "").strip(),
            "use_local": False,
        }
    return {"enabled": False, "base_url": "", "model": "", "use_local": False}


def _defaults() -> dict[str, Any]:
    return {
        "slots": {name: _default_slot(name) for name in SLOTS},
        "options": {name: _default_options() for name in SLOTS},
        "connect_timeout": _clamp(
            os.environ.get("AI_CONNECT_TIMEOUT"), *CONNECT_TIMEOUT_RANGE, fallback=4.0
        ),
        "read_timeout": _clamp(
            os.environ.get("AI_READ_TIMEOUT"), *READ_TIMEOUT_RANGE, fallback=180.0
        ),
    }


def _coerce_option(spec: dict[str, Any], raw: Any) -> dict[str, Any]:
    entry = {"on": bool((raw or {}).get("on"))}
    if spec["kind"] == "value":
        value = (raw or {}).get("value", spec["default_value"])
        vtype = spec.get("value_type")
        try:
            if vtype == "int":
                value = int(float(value))
            elif vtype == "float":
                value = float(value)
            else:
                value = str(value)
        except (TypeError, ValueError):
            value = spec["default_value"]
        if "min" in spec and vtype in ("int", "float"):
            value = max(spec["min"], min(spec.get("max", value), value))
        entry["value"] = value
    return entry


def _merge(saved: dict[str, Any]) -> dict[str, Any]:
    """Overlay a saved config on the defaults, coercing every field."""
    cfg = _defaults()
    if not isinstance(saved, dict):
        return cfg

    # Back-compat: an old flat {enabled,base_url,model} maps to the primary slot.
    if "slots" not in saved and ("base_url" in saved or "model" in saved):
        cfg["slots"]["primary"].update({
            "enabled": bool(saved.get("enabled")),
            "base_url": normalize_base_url(saved.get("base_url")),
            "model": str(saved.get("model") or "").strip(),
        })
        return cfg

    for name in SLOTS:
        slot = (saved.get("slots") or {}).get(name) or {}
        cfg["slots"][name] = {
            "enabled": bool(slot.get("enabled")),
            "base_url": normalize_base_url(slot.get("base_url")),
            "model": str(slot.get("model") or "").strip(),
            "use_local": bool(slot.get("use_local")),
        }
        saved_opts = (saved.get("options") or {}).get(name) or {}
        cfg["options"][name] = {
            spec["key"]: _coerce_option(spec, saved_opts.get(spec["key"]))
            for spec in OPTION_SPECS
        }

    # Only one slot may use the bundled local model.
    seen_local = False
    for name in SLOTS:
        if cfg["slots"][name]["use_local"]:
            if seen_local:
                cfg["slots"][name]["use_local"] = False
            seen_local = True

    if "connect_timeout" in saved:
        cfg["connect_timeout"] = _clamp(
            saved["connect_timeout"], *CONNECT_TIMEOUT_RANGE, fallback=4.0
        )
    if "read_timeout" in saved:
        cfg["read_timeout"] = _clamp(
            saved["read_timeout"], *READ_TIMEOUT_RANGE, fallback=180.0
        )
    return cfg


def load_settings() -> dict[str, Any]:
    """Current config: env defaults overlaid with the saved file (if any)."""
    try:
        with open(settings_path(), encoding="utf-8") as fh:
            saved = json.load(fh)
    except FileNotFoundError:
        return _defaults()
    except Exception:
        return _defaults()
    return _merge(saved)


def save_settings(config: dict[str, Any]) -> str:
    """Validate + persist a full config; returns the path written."""
    clean = _merge(config)
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2)
    return path


def effective_base_url(slot: dict[str, Any]) -> str:
    """The URL a slot actually talks to (the local engine when use_local)."""
    if slot.get("use_local"):
        return LOCAL_OLLAMA_URL
    return slot.get("base_url") or ""


def slot_is_configured(slot: dict[str, Any]) -> bool:
    return bool(
        slot.get("enabled") and slot.get("model") and effective_base_url(slot)
    )


def is_configured(config: dict[str, Any]) -> bool:
    """True when at least one slot is enabled with a base URL + model.

    This is a cheap check (no network) used to decide whether to *offer* AI in
    the UI; actual reachability is tested per request (see ai_client.tiered).
    """
    slots = (config or {}).get("slots") or {}
    return any(slot_is_configured(slots.get(name) or {}) for name in SLOTS)
