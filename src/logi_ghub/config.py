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


def _app_dir(env: str, default: Path) -> Path:
    d = Path(os.environ.get(env) or default) / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    return _app_dir("XDG_CONFIG_HOME", Path.home() / ".config")


def data_dir() -> Path:
    return _app_dir("XDG_DATA_HOME", Path.home() / ".local" / "share")
