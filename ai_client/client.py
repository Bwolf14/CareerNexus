"""
Native Ollama HTTP client.

Talks to Ollama's own API (not the OpenAI-compatible shim), which is what lets
us control thinking and per-model options:

* :func:`test_connection` — ``GET <base>/api/tags``; returns a status dict and
  the installed model names. Never raises (feeds the admin "Test" button and
  the per-request connectivity check).
* :func:`chat` — ``POST <base>/api/chat`` with ``think`` / ``keep_alive`` /
  ``options``; returns the assistant text (any ``<think>`` block stripped as a
  belt-and-suspenders in case a model ignores ``think:false``).
* :func:`pull_model` — ``POST <base>/api/pull`` (streaming); yields progress
  dicts so the admin UI can show a download bar with percent + speed.

Also home to the tolerant JSON extractors used to parse model output that may
be wrapped in code fences or prose.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator, Optional

import requests

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class AIClientError(Exception):
    """The model server is unreachable, errored, or returned garbage."""


def _timeouts(connect_timeout: float, read_timeout: float) -> tuple[float, float]:
    return (float(connect_timeout or 4.0), float(read_timeout or 180.0))


def test_connection(base_url: str, *, connect_timeout: float = 4.0) -> dict[str, Any]:
    """Probe ``<base>/api/tags``. Returns ``{ok, latency_ms, models, error}``.

    Never raises — the error string is meant to be shown verbatim in the UI and
    the boolean drives the per-request slot selection.
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return {"ok": False, "error": "No server address configured.", "models": []}

    started = time.monotonic()
    try:
        resp = requests.get(
            f"{base}/api/tags", timeout=(float(connect_timeout or 4.0), 15.0)
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.ConnectionError:
        return {
            "ok": False, "models": [],
            "error": "Connection refused or host unreachable — is Ollama running, "
            "listening on 0.0.0.0 (OLLAMA_HOST), and allowed through the firewall?",
        }
    except requests.exceptions.Timeout:
        return {
            "ok": False, "models": [],
            "error": "Timed out — check the address, and that the machine isn't asleep.",
        }
    except Exception as exc:
        return {"ok": False, "models": [], "error": str(exc)}

    latency_ms = round((time.monotonic() - started) * 1000)
    models = []
    for entry in (data or {}).get("models") or []:
        name = entry.get("name") or entry.get("model")
        if name:
            models.append(str(name))
    return {"ok": True, "latency_ms": latency_ms, "models": sorted(models), "error": None}


def list_models(base_url: str, *, connect_timeout: float = 4.0) -> list[str]:
    """Just the installed model names (empty on any failure)."""
    return test_connection(base_url, connect_timeout=connect_timeout).get("models") or []


def has_model(base_url: str, model: str, *, connect_timeout: float = 4.0) -> bool:
    """True if ``model`` (matched loosely on tag) is installed on the server."""
    models = list_models(base_url, connect_timeout=connect_timeout)
    if model in models:
        return True
    # Tolerate an omitted ":latest" tag on either side.
    base = model.split(":")[0]
    return any(m == model or m.split(":")[0] == base for m in models)


def chat(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    think: bool = False,
    keep_alive: Optional[str] = None,
    options: Optional[dict[str, Any]] = None,
    connect_timeout: float = 4.0,
    read_timeout: float = 180.0,
) -> str:
    """One ``/api/chat`` completion; returns the assistant text (thinking stripped)."""
    base = (base_url or "").rstrip("/")
    if not base or not model:
        raise AIClientError("AI server address or model not configured.")

    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "think": bool(think),
        "stream": False,
    }
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    if options:
        body["options"] = options

    try:
        resp = requests.post(
            f"{base}/api/chat", json=body,
            timeout=_timeouts(connect_timeout, read_timeout),
        )
    except requests.exceptions.ConnectionError as exc:
        raise AIClientError(f"Could not reach the AI server at {base}: {exc}") from exc
    except requests.exceptions.Timeout as exc:
        raise AIClientError(
            f"The AI server took longer than {read_timeout}s to respond "
            "(model still loading, or the machine is busy/asleep)."
        ) from exc

    if resp.status_code == 404:
        raise AIClientError(
            f"Model {model!r} not found on the server — pull it first "
            "(admin portal → Settings → download, or `ollama pull`)."
        )
    if not resp.ok:
        raise AIClientError(f"AI server returned HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        content = resp.json()["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AIClientError(f"Unexpected response shape from the AI server: {exc}") from exc

    return _THINK_RE.sub("", content or "").strip()


def pull_model(base_url: str, model: str, *, connect_timeout: float = 4.0) -> Iterator[dict[str, Any]]:
    """Stream ``/api/pull`` progress lines for a model download.

    Yields the raw Ollama status dicts, e.g.
    ``{"status": "pulling …", "total": 12345, "completed": 6789}`` and finally
    ``{"status": "success"}``. Raises :class:`AIClientError` if the request
    can't be started.
    """
    base = (base_url or "").rstrip("/")
    if not base or not model:
        raise AIClientError("AI server address or model not set.")
    try:
        resp = requests.post(
            f"{base}/api/pull",
            json={"model": model, "stream": True},
            stream=True,
            timeout=(float(connect_timeout or 4.0), 600.0),
        )
    except requests.exceptions.RequestException as exc:
        raise AIClientError(f"Could not start the download: {exc}") from exc

    if not resp.ok:
        raise AIClientError(f"Download failed: HTTP {resp.status_code}: {resp.text[:200]}")

    for line in resp.iter_lines():
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


# ---------------------------------------------------------------------------
# Tolerant JSON extraction (models love code fences and preambles)
# ---------------------------------------------------------------------------
def _candidates(text: str) -> list[str]:
    text = text.strip()
    out: list[str] = []
    for match in _FENCE_RE.finditer(text):
        out.append(match.group(1).strip())
    out.append(text)
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            out.append(text[start : end + 1])
    return out


def extract_json(text: str, expect: type) -> Any:
    """Parse the first JSON value of type ``expect`` found in model output."""
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
