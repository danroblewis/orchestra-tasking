"""Whether a recorded pid is a live process."""

from __future__ import annotations

import psutil


def alive(pid) -> bool:
    """A recorded pid proves liveness only if that process exists and is not a zombie."""
    try:
        return bool(pid) and psutil.Process(int(pid)).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False
