"""Optional launch-at-startup via the per-user Run key (no admin rights needed).

Only offered for the packaged build: a source checkout has no stable command to
register.  The installer removes the value on uninstall.
"""

from __future__ import annotations

import logging
import sys
import winreg

import app_info

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = app_info.APP_NAME


def supported() -> bool:
    return app_info.is_frozen()


def command() -> str:
    return f'"{sys.executable}" --minimized'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _type = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    if not supported():
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        log.error("Could not update launch-at-startup: %s", exc)
        return False
