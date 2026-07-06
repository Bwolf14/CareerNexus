"""
Minimal OpenAI-compatible chat client (Ollama, LM Studio, vLLM, …).

Two operations, both plain HTTP via ``requests``:

* :func:`test_connection` — ``GET <base>/models``; returns a status dict and
  the model tags the server offers (never raises — it feeds the settings
  page's "Test connection" button).
* :func:`chat` — ``POST <base>/chat/completions``; returns the assistant
  text, with reasoning blocks (``<think>…</think>``, emitted by thinking
  models like Qwen3) stripped. Raises :class:`AIClientError` on any failure
  so callers can fall back to the deterministic behaviour.

Also home to the tolerant JSON extractors used to parse model output that
may be wrapped in code fences or prose.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

import requests

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class AIClientError(Exception):
    """The model server is unreachable, errored, or returned garbage."""


def _timeouts(settings: dict[str, Any]) -> tuple[float, float]:
    return (
        float(settings.get("connect_timeout") or 4.0),
        float(settings.get("read_timeout") or 180.0),
    )


def test_connection(settings: dict[str, Any]) -> dict[str, Any]:
    """Probe ``<base>/models``. Returns ``{ok, latency_ms, models, error}``.

    Never raises — the error string is meant to be shown verbatim in the UI.
    """
    base = (settings.get("base_url") or "").rstrip("/")
    if not base:
        return {"ok": False, "error": "No server address configured.", "models": []}

    started = time.monotonic()
    try:
        resp = requests.get(
            f"{base}/models",
            timeout=(float(settings.get("connect_timeout") or 4.0), 15.0),
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.ConnectionError:
        return {
            "ok": False,
            "models": [],
            "error": "Connection refused or host unreachable — is Ollama running, "
            "listening on 0.0.0.0 (OLLAMA_HOST), and allowed through the firewall?",
        }
    except requests.exceptions.Timeout:
        # A sleeping Windows box silently drops packets, which looks like this.
        return {
            "ok": False,
            "models": [],
            "error": "Timed out — check the address, and that the PC isn't asleep.",
        }
    except Exception as exc:
        return {"ok": False, "models": [], "error": str(exc)}

    latency_ms = round((time.monotonic() - started) * 1000)
    models = []
    for entry in (data or {}).get("data") or []:
        model_id = entry.get("id")
        if model_id:
            models.append(str(model_id))
    return {"ok": True, "latency_ms": latency_ms, "models": sorted(models), "error": None}


def chat(
    settings: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.4,
) -> str:
    """One chat completion; returns the assistant's text (thinking stripped)."""
    base = (settings.get("base_url") or "").rstrip("/")
    model = settings.get("model") or ""
    if not base or not model:
        raise AIClientError("AI server address or model not configured.")

    try:
        resp = requests.post(
            f"{base}/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "stream": False,
            },
            timeout=_timeouts(settings),
        )
    except requests.exceptions.ConnectionError as exc:
        raise AIClientError(f"Could not reach the AI server at {base}: {exc}") from exc
    except requests.exceptions.Timeout as exc:
        raise AIClientError(
            f"The AI server took longer than {settings.get('read_timeout')}s to "
            "respond (model still loading, or the machine is busy/asleep)."
        ) from exc

    if resp.status_code == 404:
        raise AIClientError(
            f"Model {model!r} not found on the server — run `ollama pull {model}` "
            "on the PC, or pick a model from the list on the settings page."
        )
    if not resp.ok:
        raise AIClientError(f"AI server returned HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AIClientError(f"Unexpected response shape from the AI server: {exc}") from exc

    return _THINK_RE.sub("", content or "").strip()


# ---------------------------------------------------------------------------
# Tolerant JSON extraction (models love code fences and preambles)
# ---------------------------------------------------------------------------
def _candidates(text: str) -> list[str]:
    """Strings worth attempting to parse, most-specific first."""
    text = text.strip()
    out: list[str] = []
    for match in _FENCE_RE.finditer(text):
        out.append(match.group(1).strip())
    out.append(text)
    # First balanced-looking slice from the first bracket to the last.
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            out.append(text[start : end + 1])
    return out


def extract_json(text: str, expect: type) -> Any:
    """Parse the first JSON value of type ``expect`` findable in model output.

    ``expect`` is ``list`` or ``dict``. Raises :class:`AIClientError` when
    nothing parseable of that type is present.
    """
    for candidate in _candidates(text or ""):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, expect):
            return value
    raise AIClientError(
        f"The model's reply did not contain the expected JSON {expect.__name__}."
    )
