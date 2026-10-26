"""Application identity and resource-path helpers shared by every module."""

from __future__ import annotations

import os
import sys

APP_NAME = "RDP Nexus"
APP_VERSION = "1.0.0"
APP_PUBLISHER = "ADevlooper"
APP_URL = "https://github.com/ADevlooper/RDP-Nexus"
# Explicit AppUserModelID so the taskbar groups the window under our own icon
# (instead of python.exe's) when running from source.
APP_USER_MODEL_ID = "ADevlooper.RDPNexus"
# Named in the per-session namespace; the installer's AppMutex uses the same name.
SINGLE_INSTANCE_MUTEX = "RDPNexus.SingleInstance"
# Folder name under %APPDATA% / %LOCALAPPDATA%.
DATA_DIR_NAME = "RDP Nexus"


def is_frozen() -> bool:
    """True when running from a PyInstaller build."""
    return bool(getattr(sys, "frozen", False))


def resource_path(*parts: str) -> str:
    """Absolute path to a bundled resource (works from source and PyInstaller)."""
    if is_frozen():
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def roaming_data_dir() -> str:
    root = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(root, DATA_DIR_NAME)


def local_data_dir() -> str:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(root, DATA_DIR_NAME)
