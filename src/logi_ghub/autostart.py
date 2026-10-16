"""Starting the panel icon and the logi-ghubd service at login, and telling whether they run."""

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
    return shutil.which("logi-ghub-tray") or str(Path.home() / ".local" / "bin" / "logi-ghub-tray")


def _systemctl(*args: str) -> bool:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def is_enabled() -> bool:
    return _autostart_file().exists()


def service_installed() -> bool:
    return (config_dir().parent / "systemd" / "user" / "logi-ghubd.service").exists()


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


def process_running(*needles: bytes) -> bool:
    """True if a process's command line (arguments joined by spaces) contains one of ``needles``."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if any(n in cmd for n in needles):
            return True
    return False


def tray_running() -> bool:
    return process_running(b"logi-ghub-tray", b"logi_ghub.tray")


def daemon_running() -> bool:
    """The logi-ghubd service, or the daemon started by hand (python -m, logi-ghub-cli daemon)."""
    return process_running(b"logi-ghubd", b"logi_ghub.daemon", b"logi-ghub-cli daemon")


# OpenRGB's SDK server, which the whole-computer sync talks to
OPENRGB_UNIT = "openrgb-server.service"


def openrgb_unit_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / OPENRGB_UNIT


def set_openrgb(on: bool) -> list[str]:
    """Run OpenRGB's server in the background at login (or stop doing so)."""
    exe = shutil.which("openrgb")
    if on and exe is None:
        return ["OpenRGB isn't installed (sudo apt install openrgb)"]
    unit = openrgb_unit_path()
    if on:
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Unit]\nDescription=OpenRGB SDK server (for logi-ghub-linux's whole-computer sync)\n"
            "After=graphical-session.target\n\n"
            f"[Service]\nExecStart={exe} --server --noautoconnect\nRestart=on-failure\nRestartSec=5\n\n"
            "[Install]\nWantedBy=graphical-session.target\n"
        )
        _systemctl("daemon-reload")
        if not _systemctl("enable", "--now", OPENRGB_UNIT):
            return ["couldn't start OpenRGB's server"]
        return []
    _systemctl("disable", "--now", OPENRGB_UNIT)
    return []
