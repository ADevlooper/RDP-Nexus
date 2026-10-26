import random

import pytest

import winapi as w
from input_safety import INJECTION_SIGNATURE, Verdict
from keyboard_navigation import (
    KEYS, MODE_SWITCH_OPTIONS, SCROLL_KEYS, SHUFFLE_APPS, SWITCH_SHORTCUTS, AppShuffler, KeyboardNavigator,
    KeyEvent, build_chord, chord_text, to_inputs,
)

# --- allow-list ------------------------------------------------------------------


def test_only_allow_listed_keys_exist():
    # No Win key, no Esc/Del/F-keys/letters: nothing that can act on the local shell.
    assert set(KEYS) == {"ctrl", "shift", "alt", "tab", "pageup", "pagedown", "up", "down"}


def test_no_local_shell_shortcuts():
    for sc in SWITCH_SHORTCUTS.values():
        assert not ({"alt", "tab"} <= set(sc.keys)), "Alt+Tab would switch LOCAL apps in windowed RDP"
        assert "win" not in sc.keys


def test_mode_options_reference_existing_shortcuts():
    for options in MODE_SWITCH_OPTIONS.values():
        assert all(o in SWITCH_SHORTCUTS for o in options)


def test_build_chord_order():
    assert build_chord(("ctrl", "shift", "tab")) == [
        KeyEvent("ctrl", True), KeyEvent("shift", True), KeyEvent("tab", True), KeyEvent("tab", False),
        KeyEvent("shift", False), KeyEvent("ctrl", False),
    ]


def test_build_chord_repeat_holds_modifier():
    ev = build_chord(("alt", "pageup"), repeat=3)
    assert ev[0] == KeyEvent("alt", True) and ev[-1] == KeyEvent("alt", False)
    assert [e for e in ev[1:-1]] == [KeyEvent("pageup", True), KeyEvent("pageup", False)] * 3


@pytest.mark.parametrize("keys", [("ctrl", "f4"), ("tab", "ctrl"), ("ctrl", "alt"), ("pagedown", "tab")])
def test_build_chord_rejects_bad_chords(keys):
    with pytest.raises(ValueError):
        build_chord(keys)


def test_empty_chord_is_nothing():
    assert build_chord(()) == []
    assert chord_text(()) == "nothing"


def test_to_inputs_uses_scancodes_and_signature():
    inputs = to_inputs(build_chord(("ctrl", "pagedown")))
    assert len(inputs) == 4
    for inp in inputs:
        assert inp.type == w.INPUT_KEYBOARD
        assert inp.ki.dwFlags & w.KEYEVENTF_SCANCODE
        assert inp.ki.wVk == 0
        assert inp.ki.dwExtraInfo == INJECTION_SIGNATURE
    ctrl_down, pd_down, pd_up, ctrl_up = inputs
    assert ctrl_down.ki.wScan == 0x1D and not ctrl_down.ki.dwFlags & w.KEYEVENTF_KEYUP
    assert pd_down.ki.wScan == 0x51 and pd_down.ki.dwFlags & w.KEYEVENTF_EXTENDEDKEY
    assert pd_up.ki.dwFlags & w.KEYEVENTF_KEYUP
    assert ctrl_up.ki.dwFlags & w.KEYEVENTF_KEYUP


def test_scroll_keys_are_single_navigation_keys():
    for _label, down, up in SCROLL_KEYS.values():
        for k in (down, up):
            assert k is None or k in ("pagedown", "pageup", "down", "up")


# --- app shuffler ---------------------------------------------------------------------


def simulate_remote(shuffler, steps_needed_log, rounds, n):
    """Reference model of the remote Alt+Tab list, independent of the shuffler's own model."""
    remote = [f"app{i}" for i in range(n)]  # remote[0] is the active app
    visited = [remote[0]]
    for _ in range(rounds * n):
        _target, k = shuffler.peek()
        steps_needed_log.append(k)
        chosen = remote.pop(k)  # holding Alt and pressing Page Up k times selects position k
        remote.insert(0, chosen)
        shuffler.commit()
        visited.append(chosen)
    return visited


@pytest.mark.parametrize("n", [2, 3, 4, 7, 12, 20])
def test_shuffle_never_repeats_and_visits_everything(n):
    sh = AppShuffler(n, random.Random(n))
    ks = []
    visited = simulate_remote(sh, ks, rounds=30, n=n)
    assert all(1 <= k <= n - 1 for k in ks)
    assert all(a != b for a, b in zip(visited, visited[1:])), "same app twice in a row"
    # Every round of n switches visits every app exactly once.
    switches = visited[1:]
    for r in range(30):
        assert sorted(switches[r * n:(r + 1) * n]) == sorted(f"app{i}" for i in range(n))


def test_shuffle_order_changes_between_rounds():
    n = 5
    sh = AppShuffler(n, random.Random(1))
    visited = simulate_remote(sh, [], rounds=40, n=n)[1:]
    rounds = [tuple(visited[i * n:(i + 1) * n]) for i in range(40)]
    assert all(a != b for a, b in zip(rounds, rounds[1:]))
    assert len(set(rounds)) > 10  # genuinely shuffled, not a fixed rotation


def test_shuffle_peek_does_not_advance():
    sh = AppShuffler(4, random.Random(3))
    assert sh.peek() == sh.peek()


def test_shuffle_count_bounds():
    with pytest.raises(ValueError):
        AppShuffler(1)
    with pytest.raises(ValueError):
        AppShuffler(21)


# --- navigator ---------------------------------------------------------------------


class FakeGate:
    def __init__(self, verdict=Verdict(True, "ok", "OK"), foreground=(True, True)):
        self.verdict = verdict
        self.fg = list(foreground)

    def check_keyboard(self, target, idle_guard_s):
        return self.verdict

    def still_foreground(self, target):
        return self.fg.pop(0) if self.fg else True


class FakeLedger:
    def __init__(self):
        self.records = 0

    def record(self):
        self.records += 1


def test_navigator_sends_whole_chord_in_one_call():
    calls = []
    nav = KeyboardNavigator(FakeGate(), FakeLedger(), send=lambda inputs: calls.append(inputs) or len(inputs))
    r = nav.send_chord(object(), ("alt", "pageup"), 10, repeat=2)
    assert r.ok
    assert len(calls) == 1 and len(calls[0]) == 6


def test_navigator_does_not_send_when_gate_denies():
    calls = []
    gate = FakeGate(verdict=Verdict(False, "not_foreground", "RDP window is not the active window"))
    nav = KeyboardNavigator(gate, FakeLedger(), send=lambda inputs: calls.append(inputs) or len(inputs))
    r = nav.send_chord(object(), ("ctrl", "tab"), 10)
    assert not r.ok and r.code == "not_foreground"
    assert calls == []


def test_navigator_last_moment_focus_check():
    calls = []
    nav = KeyboardNavigator(FakeGate(foreground=(False,)), FakeLedger(),
                            send=lambda inputs: calls.append(inputs) or len(inputs))
    assert not nav.send_chord(object(), ("ctrl", "tab"), 10).ok
    assert calls == []


def test_navigator_reports_focus_change_during_send():
    nav = KeyboardNavigator(FakeGate(foreground=(True, False)), FakeLedger(), send=lambda inputs: len(inputs))
    r = nav.send_chord(object(), ("ctrl", "tab"), 10)
    assert not r.ok and r.code == "focus_changed"


def test_navigator_releases_keys_after_partial_send():
    calls = []

    def send(inputs):
        calls.append(inputs)
        return 2 if len(calls) == 1 else len(inputs)  # first call: only Ctrl down + Tab down accepted

    nav = KeyboardNavigator(FakeGate(foreground=(True, True, True)), FakeLedger(), send=send)
    r = nav.send_chord(object(), ("ctrl", "tab"), 10)
    assert r.code == "send_failed"
    release = calls[1]
    assert [i.ki.wScan for i in release] == [0x0F, 0x1D]
    assert all(i.ki.dwFlags & w.KEYEVENTF_KEYUP for i in release)


def test_shuffle_shortcut_is_alt_pageup():
    assert SWITCH_SHORTCUTS[SHUFFLE_APPS].keys == ("alt", "pageup")
