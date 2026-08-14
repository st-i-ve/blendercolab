"""The ONLY module permitted to branch on operating system.

Everything else in blendfleet must be platform-neutral so the Linux build is
a packaging job, not a port. Task 11 greps for violations.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _base() -> Path:
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "BlendFleet"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "blendfleet"
    return Path(os.environ["HOME"]) / ".config" / "blendfleet"


def config_dir() -> Path:
    p = _base()
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_dir() -> Path:
    p = _base() / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = _base() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_dir() -> Path:
    """Where crash and diagnostic logs go.

    Under the same user-data base as the rest, deliberately: the app has
    died three times with nothing written down anywhere, and a log inside
    the PyInstaller bundle directory would be wiped or read-only depending
    on where the user installed it. This one survives the process dying
    and survives reinstalling the app.
    """
    p = _base() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p
