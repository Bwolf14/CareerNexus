"""
Career Nexus AI client — talks to a self-hosted LLM over the network.

The app speaks the **OpenAI-compatible chat API**, which Ollama, LM Studio,
and vLLM all expose. Point it at an Ollama server on another machine (e.g. a
Windows PC with a big GPU) and the AI features light up:

* the follow-up questionnaire is written by the model instead of templates
  (:func:`ai_client.features.generate_questions`), and
* the career plan gains per-job analysis and an overall summary
  (:func:`ai_client.features.generate_match_analysis`).

Connection settings live in a JSON file managed from the web UI's
``/settings`` page (env vars provide the defaults — see
:mod:`ai_client.settings`). Every feature call is wrapped so that a missing,
unreachable, or misbehaving model server degrades back to the deterministic
behaviour — the AI is an enhancement, never a dependency.

See ``docs/OLLAMA_SETUP.md`` for the Windows + Ollama setup guide.
"""

from __future__ import annotations

from .client import AIClientError, chat, test_connection
from .features import (
    generate_match_analysis,
    generate_questions,
    generate_resume_tailoring,
)
from .settings import load_settings, normalize_base_url, save_settings

__all__ = [
    "AIClientError",
    "chat",
    "test_connection",
    "generate_questions",
    "generate_match_analysis",
    "generate_resume_tailoring",
    "load_settings",
    "save_settings",
    "normalize_base_url",
]
