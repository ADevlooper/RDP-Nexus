import random

import pytest

import winapi as w
from input_safety import INJECTION_SIGNATURE, Verdict
from keyboard_navigation import (
    KEYS, MODE_SWITCH_OPTIONS, PACING, SCROLL_KEYS, SHUFFLE_APPS, SWITCH_SHORTCUTS, AppShuffler, KeyboardNavigator,
    KeyEvent, build_chord, chord_text, paced_steps, to_inputs,
)

# --- allow-list ------------------------------------------------------------------


def test_only_allow_listed_keys_exist():
    # No Win key, no Esc/Del/F-keys/letters: nothing that can act on the local shell.
    assert set(KEYS) == {"ctrl", "shift", "alt", "tab", "pageup", "pagedown", "up", "down"}


def test_no_local_shell_shortcuts():
    for sc in SWITCH_SHORTCUTS.values():
        # Alt+Tab is never a static shortcut: the engine uses it only when the RDP client forwards it.
        assert not ({"alt", "tab"} <= set(sc.keys)), "Alt+Tab would switch LOCAL apps in windowed RDP"
        assert "win" not in sc.keys


def test_remote_app_keys_follow_rdp_keyboard_mode():
    from keyboard_navigation import remote_app_keys
    assert remote_app_keys(SHUFFLE_APPS, True) == ("alt", "tab")
    assert remote_app_keys("alt_pagedown", True) == ("alt", "shift", "tab")
    assert remote_app_keys(SHUFFLE_APPS, False) == ("alt", "pageup")
    assert remote_app_keys("alt_pagedown", False) == ("alt", "pagedown")


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


# --- paced sending ------------------------------------------------------------------


def test_paced_steps_hold_alt_across_taps():
    p = PACING["normal"]
    steps = paced_steps(("alt", "pageup"), 2, p)
    assert [(s.event.key, s.event.down, s.delay_ms) for s in steps] == [
        ("alt", True, p.lead_ms),
        ("pageup", True, p.hold_ms), ("pageup", False, p.gap_ms),
        ("pageup", True, p.hold_ms), ("pageup", False, p.tail_ms),
        ("alt", False, 0),
    ]


def test_paced_single_key_tap():
    p = PACING["slow"]
    steps = paced_steps(("pagedown",), 1, p)
    assert [(s.event.key, s.event.down, s.delay_ms) for s in steps] == [("pagedown", True, p.hold_ms),
                                                                         ("pagedown", False, 0)]


def test_slow_is_slower_than_normal():
    n, s = PACING["normal"], PACING["slow"]
    assert all(getattr(s, f) > getattr(n, f) for f in ("lead_ms", "hold_ms", "gap_ms", "tail_ms"))


class FakeGate:
    def __init__(self, verdict=Verdict(True, "ok", "OK")):
        self.verdict = verdict
        self.foreground = True
        self.activity_at = None  # clock time of the last user input
        self.held = []
        self.buttons = []
        self.clock = None

    def check_keyboard(self, target, idle_guard_s):
        return self.verdict

    def still_foreground(self, target):
        return self.foreground

    def observe(self):
        pass

    def seconds_since_user_activity(self):
        return None if self.activity_at is None else self.clock() - self.activity_at

    def held_keys(self):
        return list(self.held)

    def held_buttons(self):
        return list(self.buttons)


class FakeLedger:
    def __init__(self):
        self.records = 0

    def record(self):
        self.records += 1


class Rig:
    """Navigator with a fake clock; `on_send(n)` lets a test change the world after the n-th event."""

    def __init__(self, gate=None, accept=True):
        self.t = 0.0
        self.gate = gate or FakeGate()
        self.gate.clock = lambda: self.t
        self.sent = []  # (key name, down)
        self.delays = []
        self.on_send = lambda n: None
        self.accept = accept
        scan_to_key = {k.scan: name for name, k in KEYS.items()}

        def send(inputs):
            if not self.accept:
                return 0
            for i in inputs:
                self.sent.append((scan_to_key[i.ki.wScan], not i.ki.dwFlags & w.KEYEVENTF_KEYUP))
            self.on_send(len(self.sent))
            return len(inputs)

        def sleep(s):
            self.delays.append(round(s * 1000))
            self.t += s

        self.nav = KeyboardNavigator(self.gate, FakeLedger(), send=send, sleep=sleep, clock=lambda: self.t)


def test_paced_send_one_event_per_call_in_order():
    rig = Rig()
    r = rig.nav.send_chord(object(), ("alt", "pageup"), 10, repeat=3)
    assert r.ok, r.message
    assert rig.sent == [("alt", True)] + [("pageup", True), ("pageup", False)] * 3 + [("alt", False)]
    p = PACING["normal"]
    assert rig.delays == [p.lead_ms, p.hold_ms, p.gap_ms, p.hold_ms, p.gap_ms, p.hold_ms, p.tail_ms]


def test_gate_denial_sends_nothing():
    rig = Rig(FakeGate(verdict=Verdict(False, "not_foreground", "RDP window is not the active window")))
    r = rig.nav.send_chord(object(), ("ctrl", "tab"), 10)
    assert not r.ok and r.code == "not_foreground" and rig.sent == []


def test_focus_lost_mid_sequence_releases_alt():
    rig = Rig()
    rig.on_send = lambda n: setattr(rig.gate, "foreground", n < 1)  # focus moves right after Alt down
    r = rig.nav.send_chord(object(), ("alt", "pageup"), 10, repeat=2)
    assert not r.ok and r.code == "focus_changed" and "released" in r.message
    assert rig.sent == [("alt", True), ("alt", False)]  # no Page Up after focus loss; Alt not left down


def test_cancel_mid_sequence_releases_keys():
    import threading
    cancel = threading.Event()
    rig = Rig()
    rig.on_send = lambda n: cancel.set() if n == 2 else None  # after Alt down + Page Up down
    r = rig.nav.send_chord(object(), ("alt", "pageup"), 10, repeat=3, cancel=cancel)
    assert r.code == "cancelled"
    assert rig.sent == [("alt", True), ("pageup", True), ("pageup", False), ("alt", False)]


def test_user_activity_mid_sequence_aborts():
    rig = Rig()

    def user_types(n):
        if n == 1:
            rig.gate.activity_at = rig.t + 0.01  # real input after the sequence began
    rig.on_send = user_types
    r = rig.nav.send_chord(object(), ("alt", "pageup"), 10, repeat=2)
    assert r.code == "user_active"
    assert rig.sent[-1] == ("alt", False) and ("pageup", True) not in rig.sent


def test_activity_before_sequence_does_not_abort():
    rig = Rig()
    rig.gate.activity_at = -30.0
    assert rig.nav.send_chord(object(), ("alt", "pageup"), 10).ok


def test_our_own_alt_is_not_a_foreign_held_key():
    rig = Rig()
    rig.on_send = lambda n: setattr(rig.gate, "held", ["Alt"] if n == 1 else [])
    assert rig.nav.send_chord(object(), ("alt", "pageup"), 10).ok


def test_foreign_key_pressed_mid_sequence_aborts():
    rig = Rig()
    rig.on_send = lambda n: setattr(rig.gate, "held", ["Alt", "Shift"] if n >= 1 else [])
    r = rig.nav.send_chord(object(), ("alt", "pageup"), 10)
    assert r.code == "held" and "Shift" in r.message
    assert rig.sent == [("alt", True), ("alt", False)]


def test_blocked_send_reports_failure():
    rig = Rig(accept=False)
    r = rig.nav.send_chord(object(), ("ctrl", "tab"), 10)
    assert r.code == "send_failed" and rig.sent == []


def test_focus_change_right_after_sequence_is_reported():
    rig = Rig()
    rig.on_send = lambda n: setattr(rig.gate, "foreground", n < 6)
    r = rig.nav.send_chord(object(), ("alt", "pageup"), 10, repeat=2)
    assert r.code == "focus_changed"
    assert rig.sent[-1] == ("alt", False)


def test_shuffle_shortcut_is_alt_pageup():
    assert SWITCH_SHORTCUTS[SHUFFLE_APPS].keys == ("alt", "pageup")
