"""XDG locations for profiles, macros, backups and daemon state."""

from __future__ import annotations

import os
from pathlib import Path

APP = "logi-ghub-linux"


def use_sandbox() -> None:
    """Keep simulated-device sessions (--fake) away from real profiles and backups."""
    root = Path(f"/tmp/{APP}-fake-{os.getuid()}")
    os.environ["XDG_CONFIG_HOME"] = str(root / "config")
    os.environ["XDG_DATA_HOME"] = str(root / "data")


def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    d = base / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def runtime_dir() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{APP}-{os.getuid()}")
    d = base / APP
    d.mkdir(parents=True, exist_ok=True)
    return d
