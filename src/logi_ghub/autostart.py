"""Starting the panel icon and the logi-ghubd service at login."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .config import config_dir

TRAY_DESKTOP = "logi-ghub-tray.desktop"


def _autostart_file() -> Path:
    return config_dir().parent / "autostart" / TRAY_DESKTOP


def _tray_command() -> str:
    exe = shutil.which("logi-ghub-tray") or str(Path.home() / ".local" / "bin" / "logi-ghub-tray")
    return exe


def _systemctl(*args: str) -> bool:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def is_enabled() -> bool:
    return _autostart_file().exists()


def service_installed() -> bool:
    unit = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / "logi-ghubd.service"
    return unit.exists()


def set_enabled(on: bool) -> list[str]:
    """Turn background running on or off. Returns problems worth telling the user about."""
    problems = []
    path = _autostart_file()
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[Desktop Entry]\nType=Application\nName=Logitech G devices (panel icon)\n"
            f"Exec={_tray_command()}\nIcon=input-gaming\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n"
        )
        if not service_installed():
            problems.append("the logi-ghubd service isn't installed; run scripts/install.sh")
        elif not _systemctl("enable", "--now", "logi-ghubd"):
            problems.append("couldn't start the logi-ghubd service")
        if not tray_running():
            try:
                subprocess.Popen([_tray_command()], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                problems.append(f"couldn't start the panel icon: {e}")
    else:
        path.unlink(missing_ok=True)
        _systemctl("disable", "--now", "logi-ghubd")
    return problems


def tray_running() -> bool:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if b"logi-ghub-tray" in cmd or b"logi_ghub.tray" in cmd:
            return True
    return False
