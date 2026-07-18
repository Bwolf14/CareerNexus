"""
Bring-your-own cloud AI: OpenAI / Anthropic chat, using the *user's own* key.

When a user enables this in their account (behind a prominent privacy warning),
the server sends their AI requests to the internet provider **instead of** the
locally-configured Ollama slots. The key never leaves the server — the browser
never talks to the provider directly.

Every request carries a hidden system pre-prompt instructing the model not to
use extended thinking and to answer as fast as possible while upholding
quality — cloud reasoning models are otherwise slow and expensive for the
short, structured answers Career Nexus needs.
"""

from __future__ import annotations

from typing import Any, Optional

import requests

from .client import _THINK_RE, AIClientError

PROVIDERS = ("openai", "anthropic")

# Hidden pre-prompt sent with every cloud request (per product requirement).
SPEED_PROMPT = (
    "Do not use extended thinking, hidden reasoning, or chain-of-thought "
    "preambles. Respond as quickly as possible while upholding quality. Keep "
    "the answer concise and in exactly the format requested."
)

# Fixed — this only bounds "can we even reach the provider", not how long a
# model may think. The response timeout is user-configurable (see chat_cloud):
# None (the default) means wait as long as it takes, by explicit product
# choice — a slow answer is still a correct one, and internet models have no
# local safe-mode fallback worth racing against.
_CONNECT_TIMEOUT = 10.0
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def _split_system(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    """(joined system text, non-system messages)."""
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    return "\n\n".join(system_parts), rest


def _chat_openai(
    cloud: dict[str, Any], messages: list[dict[str, str]], read_timeout: Optional[float]
) -> str:
    system_text, rest = _split_system(messages)
    body = {
        "model": cloud["model"],
        "messages": (
            [{"role": "system", "content": SPEED_PROMPT
              + ("\n\n" + system_text if system_text else "")}] + rest
        ),
    }
    try:
        resp = requests.post(
            _OPENAI_URL, json=body, timeout=(_CONNECT_TIMEOUT, read_timeout),
            headers={"Authorization": f"Bearer {cloud['api_key']}"},
        )
    except requests.exceptions.Timeout as exc:
        raise AIClientError(
            f"OpenAI didn't respond within {read_timeout}s — raise or clear the "
            "response timeout in your account settings if this keeps happening."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise AIClientError(f"Could not reach OpenAI: {exc}") from exc
    if resp.status_code in (401, 403):
        raise AIClientError("OpenAI rejected the API key — check it in your account settings.")
    if not resp.ok:
        raise AIClientError(f"OpenAI returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AIClientError(f"Unexpected OpenAI response shape: {exc}") from exc


def _chat_anthropic(
    cloud: dict[str, Any], messages: list[dict[str, str]], read_timeout: Optional[float]
) -> str:
    system_text, rest = _split_system(messages)
    body = {
        "model": cloud["model"],
        "max_tokens": 2048,
        "system": SPEED_PROMPT + ("\n\n" + system_text if system_text else ""),
        "messages": rest,
    }
    try:
        resp = requests.post(
            _ANTHROPIC_URL, json=body, timeout=(_CONNECT_TIMEOUT, read_timeout),
            headers={"x-api-key": cloud["api_key"],
                     "anthropic-version": "2023-06-01"},
        )
    except requests.exceptions.Timeout as exc:
        raise AIClientError(
            f"Anthropic didn't respond within {read_timeout}s — raise or clear the "
            "response timeout in your account settings if this keeps happening."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise AIClientError(f"Could not reach Anthropic: {exc}") from exc
    if resp.status_code in (401, 403):
        raise AIClientError("Anthropic rejected the API key — check it in your account settings.")
    if not resp.ok:
        raise AIClientError(f"Anthropic returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        blocks = resp.json().get("content") or []
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    except (ValueError, AttributeError, TypeError) as exc:
        raise AIClientError(f"Unexpected Anthropic response shape: {exc}") from exc


def cloud_is_configured(cloud: dict[str, Any] | None) -> bool:
    cloud = cloud or {}
    return bool(
        cloud.get("enabled") and cloud.get("api_key") and cloud.get("model")
        and (cloud.get("provider") or "openai") in PROVIDERS
    )


def chat_cloud(cloud: dict[str, Any], messages: list[dict[str, str]]) -> str:
    """One completion against the user's chosen internet provider.

    ``cloud["timeout_seconds"]`` is the user's configured response timeout —
    None (unset, the default) means no read timeout at all: the request waits
    as long as the provider takes. Only the connection itself is bounded
    (``_CONNECT_TIMEOUT``), so an unreachable host still fails fast.
    """
    if not cloud_is_configured(cloud):
        raise AIClientError("Cloud AI is not fully configured.")
    read_timeout = cloud.get("timeout_seconds")
    provider = cloud.get("provider") or "openai"
    if provider == "anthropic":
        text = _chat_anthropic(cloud, messages, read_timeout)
    else:
        text = _chat_openai(cloud, messages, read_timeout)
    return _THINK_RE.sub("", text or "").strip()
