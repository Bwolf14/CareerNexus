"""
Tiered model selection: try primary → secondary → tertiary, use the first that
passes a live connectivity test, and fall back to safe mode (no AI) if none do.

``run_chat`` is the single entry point the product features call. It resolves an
available slot *at call time* (per your requirement that every prompt is
preceded by a connectivity test), builds the per-model request options from the
settings matrix, and sends the prompt via :mod:`ai_client.client`.
"""

from __future__ import annotations

from typing import Any, Optional

from . import client
from .client import AIClientError
from .cloud import chat_cloud, cloud_is_configured
from .settings import (
    OPTION_SPECS,
    SLOTS,
    effective_base_url,
    slot_is_configured,
)


def build_options(slot_options: dict[str, Any]) -> tuple[bool, Optional[str], dict[str, Any]]:
    """Translate a slot's option matrix into (think, keep_alive, options).

    * ``think`` — the Thinking row (default OFF → the request sends think:false).
    * ``keep_alive`` — "<n>m" when the Keep-model-loaded row is on, else None.
    * ``options`` — Ollama sampling options for every other enabled value row.
    """
    slot_options = slot_options or {}

    def entry(key: str) -> dict[str, Any]:
        return slot_options.get(key) or {}

    think = bool(entry("think").get("on"))

    keep_alive = None
    ka = entry("keep_alive")
    if ka.get("on"):
        keep_alive = f"{int(ka.get('value', 0))}m"

    options: dict[str, Any] = {}
    # Map each value-row (except keep_alive, handled above) to an Ollama option.
    numeric = ("temperature", "num_predict", "num_ctx", "top_p", "top_k", "seed")
    for key in numeric:
        e = entry(key)
        if e.get("on") and e.get("value") is not None:
            options[key] = e["value"]
    stop = entry("stop")
    if stop.get("on") and str(stop.get("value") or "").strip():
        options["stop"] = [str(stop["value"])]

    return think, keep_alive, options


def resolve_slot(config: dict[str, Any], *, probe: bool = True) -> Optional[dict[str, Any]]:
    """First usable slot in priority order, or None (→ safe mode).

    With ``probe`` (the default) each candidate is connectivity-tested and its
    model must be installed; without it, the first configured slot is returned
    without touching the network (used for cheap "is anything set up?" checks).
    """
    config = config or {}
    slots = config.get("slots") or {}
    options = config.get("options") or {}
    connect_timeout = float(config.get("connect_timeout") or 4.0)

    for name in SLOTS:
        slot = slots.get(name) or {}
        if not slot_is_configured(slot):
            continue
        base = effective_base_url(slot)
        model = slot.get("model")
        if probe:
            result = client.test_connection(base, connect_timeout=connect_timeout)
            if not result.get("ok"):
                continue
            if not client.has_model(base, model, connect_timeout=connect_timeout):
                continue
        return {
            "name": name,
            "slot": slot,
            "options": options.get(name) or {},
            "base": base,
            "model": model,
        }
    return None


def slot_timeouts(slot_options: dict[str, Any], config: dict[str, Any]) -> tuple[float, float]:
    """(connect, read) timeouts for one slot — per-model matrix values when on,
    else the global defaults."""

    def pick(key: str, fallback: float) -> float:
        entry = (slot_options or {}).get(key) or {}
        if entry.get("on") and entry.get("value") is not None:
            try:
                return float(entry["value"])
            except (TypeError, ValueError):
                pass
        return fallback

    return (
        pick("connect_timeout", float((config or {}).get("connect_timeout") or 4.0)),
        pick("read_timeout", float((config or {}).get("read_timeout") or 180.0)),
    )


def configured_model(config: dict[str, Any]) -> Optional[str]:
    """The model that would serve a prompt (no network) — for UI labels.

    A user's enabled cloud key takes precedence over the backend slots.
    """
    cloud = (config or {}).get("cloud") or {}
    if cloud_is_configured(cloud):
        return cloud.get("model")
    slots = (config or {}).get("slots") or {}
    for name in SLOTS:
        slot = slots.get(name) or {}
        if slot_is_configured(slot):
            return slot.get("model")
    return None


def active_status(config: dict[str, Any]) -> dict[str, Any]:
    """For the UI/banner: which model (if any) would serve the next prompt."""
    cloud = (config or {}).get("cloud") or {}
    if cloud_is_configured(cloud):
        return {"available": True, "slot": "cloud", "model": cloud.get("model")}
    active = resolve_slot(config, probe=True)
    if active is None:
        return {"available": False, "slot": None, "model": None}
    return {"available": True, "slot": active["name"], "model": active["model"]}


def run_chat(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    min_predict: Optional[int] = None,
) -> str:
    """Route one chat: the user's cloud key (if enabled) wins, else the first
    reachable backend slot. Raise if nothing is usable.

    ``min_predict`` is a floor on the reply's token budget for callers that
    know the answer must contain a certain amount of content (e.g. one
    analysis paragraph per posting) — it overrides an admin-configured
    "max response tokens" that's too low to be *technically capable* of
    finishing the reply, rather than silently truncating valid JSON mid-field
    and failing. It never lowers a higher admin-configured cap, and a cap the
    admin explicitly turned off (no limit) is left off.

    Callers treat :class:`AIClientError` as "fall back to the deterministic
    (safe-mode) behaviour".
    """
    cloud = (config or {}).get("cloud") or {}
    if cloud_is_configured(cloud):
        return chat_cloud(cloud, messages, min_tokens=min_predict)

    active = resolve_slot(config, probe=True)
    if active is None:
        raise AIClientError(
            "No AI model is available (none enabled/reachable) — running in safe mode."
        )
    think, keep_alive, options = build_options(active["options"])
    if min_predict and options.get("num_predict") is not None:
        options["num_predict"] = max(int(options["num_predict"]), min_predict)
    connect_timeout, read_timeout = slot_timeouts(active["options"], config)
    return client.chat(
        active["base"],
        active["model"],
        messages,
        think=think,
        keep_alive=keep_alive,
        options=options,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )
