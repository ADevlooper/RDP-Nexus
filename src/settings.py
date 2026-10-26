"""Per-user settings stored as JSON in %APPDATA%\\RDP Nexus\\settings.json.

Every value is validated on load; anything missing, malformed or out of range
falls back to its default, so a damaged file can never produce unsafe values.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, fields, replace

import app_info
from keyboard_navigation import (
    BROWSER, MAX_APPS, MIN_APPS, MODE_DEFAULT_SWITCH, MODE_SWITCH_OPTIONS, MODES, REMOTE_APP,
    SCROLL_DIRECTIONS, SCROLL_KEYS,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

INTERVAL_MIN, INTERVAL_MAX = 1.0, 240.0          # minutes
POINTER_INTERVAL_MIN, POINTER_INTERVAL_MAX = 10, 900  # seconds
POINTER_RANGE_MIN, POINTER_RANGE_MAX = 4, 150     # pixels
RESUME_POLICIES = {"auto": "Automatically (fresh countdown)", "manual": "Wait for me to press Resume"}


@dataclass(frozen=True)
class Settings:
    schema_version: int = SCHEMA_VERSION
    interval_minutes: float = 13.0
    keyboard_enabled: bool = True
    mode: str = REMOTE_APP
    switch_shortcut: str = MODE_DEFAULT_SWITCH[REMOTE_APP]
    app_count: int = 4
    scroll_key: str = "page"
    scroll_direction: str = "alternate"
    pointer_enabled: bool = True
    pointer_interval_seconds: int = 60
    pointer_range_px: int = 20
    resume_policy: str = "auto"
    always_on_top: bool = True
    expanded: bool = False
    window_x: int | None = None
    window_y: int | None = None


DEFAULTS = Settings()


def _num(value, default, lo, hi, cast):
    try:
        if isinstance(value, bool):
            raise TypeError
        v = cast(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return min(max(v, lo), hi)


def _flag(value, default):
    return value if isinstance(value, bool) else default


def _choice(value, default, allowed):
    return value if isinstance(value, str) and value in allowed else default


def _opt_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if -100_000 < v < 100_000 else None


def validate(raw: dict) -> Settings:
    d = DEFAULTS
    mode = _choice(raw.get("mode"), d.mode, MODES)
    switch = _choice(raw.get("switch_shortcut"), MODE_DEFAULT_SWITCH[mode], MODE_SWITCH_OPTIONS[mode])
    return Settings(
        schema_version=SCHEMA_VERSION,
        interval_minutes=round(_num(raw.get("interval_minutes"), d.interval_minutes, INTERVAL_MIN, INTERVAL_MAX,
                                    float), 2),
        keyboard_enabled=_flag(raw.get("keyboard_enabled"), d.keyboard_enabled),
        mode=mode,
        switch_shortcut=switch,
        app_count=_num(raw.get("app_count"), d.app_count, MIN_APPS, MAX_APPS, int),
        scroll_key=_choice(raw.get("scroll_key"), d.scroll_key, SCROLL_KEYS),
        scroll_direction=_choice(raw.get("scroll_direction"), d.scroll_direction, SCROLL_DIRECTIONS),
        pointer_enabled=_flag(raw.get("pointer_enabled"), d.pointer_enabled),
        pointer_interval_seconds=_num(raw.get("pointer_interval_seconds"), d.pointer_interval_seconds,
                                      POINTER_INTERVAL_MIN, POINTER_INTERVAL_MAX, int),
        pointer_range_px=_num(raw.get("pointer_range_px"), d.pointer_range_px, POINTER_RANGE_MIN,
                              POINTER_RANGE_MAX, int),
        resume_policy=_choice(raw.get("resume_policy"), d.resume_policy, RESUME_POLICIES),
        always_on_top=_flag(raw.get("always_on_top"), d.always_on_top),
        expanded=_flag(raw.get("expanded"), d.expanded),
        window_x=_opt_int(raw.get("window_x")),
        window_y=_opt_int(raw.get("window_y")),
    )


def default_path() -> str:
    return os.path.join(app_info.roaming_data_dir(), "settings.json")


class SettingsStore:
    """Thread-safe holder of the current settings with atomic persistence."""

    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        self._lock = threading.Lock()
        self._settings = DEFAULTS

    @property
    def current(self) -> Settings:
        with self._lock:
            return self._settings

    def load(self) -> Settings:
        settings = DEFAULTS
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                settings = validate(raw)
            else:
                log.warning("Settings file is not a JSON object; using defaults")
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("Could not read settings (%s); using defaults", exc)
        with self._lock:
            self._settings = settings
        return settings

    def update(self, **changes) -> Settings:
        known = {f.name for f in fields(Settings)}
        unknown = set(changes) - known
        if unknown:
            raise KeyError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
        with self._lock:
            merged = asdict(replace(self._settings, **changes))
            if "mode" in changes and "switch_shortcut" not in changes:
                merged["switch_shortcut"] = MODE_DEFAULT_SWITCH.get(merged["mode"], merged["switch_shortcut"])
            self._settings = validate(merged)
            return self._settings

    def save(self) -> bool:
        data = asdict(self.current)
        folder = os.path.dirname(self.path)
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="settings.", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except OSError as exc:
            log.error("Could not save settings: %s", exc)
            return False


__all__ = ["Settings", "SettingsStore", "DEFAULTS", "validate", "RESUME_POLICIES", "BROWSER", "REMOTE_APP"]
