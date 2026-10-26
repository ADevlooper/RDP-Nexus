import pytest

import winapi as w
from input_safety import ALLOWED, InjectionLedger, SafetyGate
from window_detection import OK, RdpWindow, status

TARGET = RdpWindow(42, 7, "mstsc.exe", "TscShellContainerClass", "host", True)


class Ticks:
    def __init__(self, now=10_000):
        self.now = now
        self.last_input = now

    def tick(self):
        return self.now

    def last(self):
        return self.last_input


class Insp:
    def __init__(self, code=OK):
        self.code = code

    def check(self, target, require_foreground=True):
        return status(self.code)

    def is_foreground(self, target):
        return self.code == OK


def make(code=OK, keys=()):
    t = Ticks()
    clock = [100.0]
    ledger = InjectionLedger(tick=t.tick)
    gate = SafetyGate(Insp(code), ledger, tick=t.tick, last_input=t.last, key_down=lambda vk: vk in keys,
                      clock=lambda: clock[0])
    gate.session_verdict = lambda: ALLOWED  # no real desktop query in unit tests
    return gate, ledger, t, clock


def test_own_injection_is_not_user_activity():
    gate, ledger, t, clock = make()
    t.now = 20_000
    ledger.record()
    t.last_input = 20_005  # processed a few ms after our SendInput
    gate.observe()
    assert gate.seconds_since_user_activity() is None


def test_real_input_is_user_activity():
    gate, ledger, t, clock = make()
    t.now = 20_000
    ledger.record()
    t.now = 25_000
    t.last_input = 24_000
    gate.observe()
    assert gate.seconds_since_user_activity() == pytest.approx(1.0)
    assert gate.user_active_within(10)
    clock[0] += 20
    assert not gate.user_active_within(10)


def test_tick_wraparound():
    assert w.tick_delta(5, 0xFFFFFFF0) == 21
    assert w.tick_delta(0xFFFFFFF0, 5) == -21


def test_keyboard_blocked_when_modifier_held():
    gate, *_ = make(keys=(w.VK_SHIFT,))
    v = gate.check_keyboard(TARGET, 10)
    assert not v.ok and v.code == "held" and "Shift" in v.message


def test_pointer_blocked_when_button_held():
    gate, *_ = make(keys=(w.VK_LBUTTON,))
    v = gate.check_pointer(TARGET, 10)
    assert not v.ok and v.code == "held"


def test_blocked_when_not_foreground():
    gate, *_ = make(code="not_foreground")
    assert gate.check_keyboard(TARGET, 10).code == "not_foreground"
    assert gate.check_pointer(TARGET, 10).code == "not_foreground"


def test_blocked_after_recent_user_activity():
    gate, ledger, t, clock = make()
    t.now, t.last_input = 50_000, 49_000
    v = gate.check_keyboard(TARGET, 10)
    assert not v.ok and v.code == "user_active"


def test_allowed_when_idle():
    gate, ledger, t, clock = make()
    t.now, t.last_input = 90_000, 10_000
    assert gate.check_keyboard(TARGET, 10).ok
    assert gate.check_pointer(TARGET, 10).ok
