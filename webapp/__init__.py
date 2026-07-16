"""Career Nexus web UI — upload resumes, parse them, store results in the DB."""

from __future__ import annotations

import logging
import os
import sys

_LOG_CONFIGURED = False


def configure_logging() -> None:
    """Send app logs to stdout so they stream via ``docker compose logs -f``.

    Idempotent. Level is controlled by the ``LOG_LEVEL`` env var (default INFO).
    Called from each entrypoint (web app, admin app, worker) so the same,
    real-time log stream is available over SSH for every service.
    """
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if not any(getattr(h, "_careernexus", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"
        ))
        handler._careernexus = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    _LOG_CONFIGURED = True
