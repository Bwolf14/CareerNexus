"""
Career Nexus AI client — talks to self-hosted Ollama models over the network or
a bundled local engine.

Configuration is **tiered**: primary / secondary / tertiary model slots, each
with its own Ollama connection and a matrix of per-model options (thinking off
by default, temperature, keep-alive, context size, …). Before every prompt the
app connectivity-tests the slots in order and uses the first that answers; if
none are enabled or reachable it falls back to **safe mode** (deterministic,
no AI). All of this is configured from the **admin portal**.

See ``docs/OLLAMA_SETUP.md`` for the server-side Ollama setup.
"""

from __future__ import annotations

from .catalog import CATALOG
from .client import AIClientError, chat, extract_json, list_models, pull_model, test_connection
from .features import (
    generate_match_analysis,
    generate_questions,
    generate_resume_tailoring,
)
from .settings import (
    OPTION_SPECS,
    SLOTS,
    is_configured,
    load_settings,
    normalize_base_url,
    save_settings,
)
from .system import resources
from .tiered import active_status, resolve_slot, run_chat

__all__ = [
    "AIClientError",
    "chat",
    "run_chat",
    "test_connection",
    "list_models",
    "pull_model",
    "extract_json",
    "generate_questions",
    "generate_match_analysis",
    "generate_resume_tailoring",
    "load_settings",
    "save_settings",
    "normalize_base_url",
    "is_configured",
    "active_status",
    "resolve_slot",
    "OPTION_SPECS",
    "SLOTS",
    "CATALOG",
    "resources",
]
