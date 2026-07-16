"""
Curated catalog of local models the admin can download and run.

Tuned for a **CPU-only** server (the Career Nexus deployment target): the list
favours small instruct models that answer Career Nexus's short JSON/text prompts
acceptably without a GPU. ``ram_gb`` is a *recommended minimum* free RAM to run
the model comfortably; the admin UI compares it against detected memory and
warns before a too-big download.

Sizes/notes are approximate — exact download size is whatever Ollama reports at
pull time.
"""

from __future__ import annotations

from typing import Any

# Each entry: ollama tag, label, params, download size, recommended free RAM,
# what it's good for, and a CPU performance note.
CATALOG: list[dict[str, Any]] = [
    {
        "model": "qwen2.5:0.5b", "label": "Qwen2.5 0.5B", "params": "0.5B",
        "storage_gb": 0.4, "ram_gb": 1,
        "good_for": "Smoke-testing the pipeline; very short answers.",
        "perf": "Extremely fast on CPU, but the weakest quality — fine for trying the flow.",
    },
    {
        "model": "llama3.2:1b", "label": "Llama 3.2 1B", "params": "1B",
        "storage_gb": 1.3, "ram_gb": 2,
        "good_for": "Fast, lightweight questions/analysis on modest hardware.",
        "perf": "Snappy on CPU; decent for short structured replies.",
    },
    {
        "model": "qwen2.5:1.5b", "label": "Qwen2.5 1.5B", "params": "1.5B",
        "storage_gb": 1.0, "ram_gb": 2,
        "good_for": "Good quality-for-size on JSON/structured output.",
        "perf": "Fast on CPU; a solid default for a low-RAM box.",
    },
    {
        "model": "gemma2:2b", "label": "Gemma 2 2B", "params": "2B",
        "storage_gb": 1.6, "ram_gb": 3,
        "good_for": "Well-rounded small model for reasoning-lite tasks.",
        "perf": "Comfortable on CPU with a few GB free.",
    },
    {
        "model": "llama3.2:3b", "label": "Llama 3.2 3B", "params": "3B",
        "storage_gb": 2.0, "ram_gb": 4,
        "good_for": "Noticeably better written analysis and tailoring.",
        "perf": "Usable on CPU (a few seconds/response); the sweet spot on a 8GB box.",
    },
    {
        "model": "qwen2.5:3b", "label": "Qwen2.5 3B", "params": "3B",
        "storage_gb": 1.9, "ram_gb": 4,
        "good_for": "Strong instruction-following and clean JSON at 3B.",
        "perf": "Usable on CPU; recommended default when you have ~6–8GB free.",
    },
    {
        "model": "phi3.5:3.8b", "label": "Phi-3.5 Mini 3.8B", "params": "3.8B",
        "storage_gb": 2.2, "ram_gb": 5,
        "good_for": "Reasoning-leaning small model; good at concise advice.",
        "perf": "Usable on CPU; a little slower than the 3B models.",
    },
    {
        "model": "qwen2.5:7b", "label": "Qwen2.5 7B", "params": "7B",
        "storage_gb": 4.7, "ram_gb": 8,
        "good_for": "Best quality here — richer match analysis and tailoring.",
        "perf": "Slow on CPU (tens of seconds/response). Only on a 12GB+ box, "
                "ideally with Keep-model-loaded on.",
    },
    {
        "model": "llama3.1:8b", "label": "Llama 3.1 8B", "params": "8B",
        "storage_gb": 4.7, "ram_gb": 10,
        "good_for": "High-quality general model.",
        "perf": "Quite slow on CPU. Recommended only with lots of RAM or a GPU.",
    },
]

_BY_MODEL = {m["model"]: m for m in CATALOG}


def get(model: str) -> dict[str, Any] | None:
    return _BY_MODEL.get(model)


def fits(model: str, available_ram_gb: float | None) -> bool:
    """Whether a catalog model comfortably fits the detected free RAM."""
    entry = _BY_MODEL.get(model)
    if not entry or available_ram_gb is None:
        return True
    return available_ram_gb >= entry["ram_gb"]
