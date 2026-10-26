import json
import os

import pytest

from keyboard_navigation import BROWSER, REMOTE_APP, SHUFFLE_APPS
from settings import DEFAULTS, SettingsStore, validate


def test_defaults_match_specification():
    assert DEFAULTS.interval_minutes == 13.0
    assert DEFAULTS.mode == REMOTE_APP
    assert DEFAULTS.switch_shortcut == SHUFFLE_APPS
    assert DEFAULTS.keyboard_enabled is True
    assert DEFAULTS.resume_policy == "auto"


def test_validate_clamps_and_rejects_bad_types():
    s = validate({
        "interval_minutes": 0.01, "pointer_range_px": 10_000, "pointer_interval_seconds": "abc",
        "app_count": 99, "keyboard_enabled": "yes", "mode": "evil", "scroll_key": 3,
        "window_x": True, "window_y": "12",
    })
    assert s.interval_minutes == 1.0
    assert s.pointer_range_px == 150
    assert s.pointer_interval_seconds == DEFAULTS.pointer_interval_seconds
    assert s.app_count == 20
    assert s.keyboard_enabled is True  # non-bool ignored
    assert s.mode == DEFAULTS.mode
    assert s.scroll_key == DEFAULTS.scroll_key
    assert s.window_x is None and s.window_y == 12


def test_nan_and_infinite_values_fall_back():
    s = validate({"interval_minutes": float("nan")})
    assert s.interval_minutes == DEFAULTS.interval_minutes
    assert validate({"interval_minutes": float("inf")}).interval_minutes == 240.0


def test_switch_shortcut_must_belong_to_mode():
    s = validate({"mode": BROWSER, "switch_shortcut": SHUFFLE_APPS})
    assert s.switch_shortcut == "ctrl_tab"
    s = validate({"mode": REMOTE_APP, "switch_shortcut": "ctrl_tab"})
    assert s.switch_shortcut == SHUFFLE_APPS


def test_mode_change_resets_shortcut(tmp_path):
    store = SettingsStore(str(tmp_path / "s.json"))
    store.load()
    assert store.update(mode=BROWSER).switch_shortcut == "ctrl_tab"
    assert store.update(switch_shortcut="ctrl_shift_tab").switch_shortcut == "ctrl_shift_tab"
    assert store.update(mode=REMOTE_APP).switch_shortcut == SHUFFLE_APPS


def test_unknown_setting_rejected(tmp_path):
    store = SettingsStore(str(tmp_path / "s.json"))
    with pytest.raises(KeyError):
        store.update(not_a_setting=1)


def test_roundtrip_and_atomic_save(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    store = SettingsStore(str(path))
    store.load()
    store.update(interval_minutes=7, app_count=6, pointer_enabled=False, window_x=-500, window_y=40)
    assert store.save()
    assert sorted(os.listdir(path.parent)) == ["settings.json"]  # no temp files left behind
    again = SettingsStore(str(path))
    s = again.load()
    assert (s.interval_minutes, s.app_count, s.pointer_enabled, s.window_x, s.window_y) == (7, 6, False, -500, 40)


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", ""])
def test_corrupt_file_gives_defaults(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    assert SettingsStore(str(path)).load() == DEFAULTS


def test_saved_file_is_plain_json(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(str(path))
    store.load()
    store.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["interval_minutes"] == 13.0
    assert "password" not in json.dumps(data).lower()
