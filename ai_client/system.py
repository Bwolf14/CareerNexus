"""
Host resource detection — used to warn before downloading a too-big model.

``psutil`` is optional: if it isn't installed the functions return ``None`` and
the UI simply skips the RAM check rather than blocking downloads.
"""

from __future__ import annotations

import os
from typing import Any


def resources() -> dict[str, Any]:
    """Best-effort host stats: total/available RAM (GB) and CPU count.

    Values are ``None`` when they can't be determined (e.g. psutil missing).
    """
    info: dict[str, Any] = {
        "ram_total_gb": None,
        "ram_available_gb": None,
        "cpu_count": os.cpu_count(),
    }
    try:
        import psutil

        vm = psutil.virtual_memory()
        info["ram_total_gb"] = round(vm.total / (1024 ** 3), 1)
        info["ram_available_gb"] = round(vm.available / (1024 ** 3), 1)
    except Exception:
        pass
    return info
