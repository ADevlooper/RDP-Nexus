"""State-machine tests for the automation engine using a fake clock and fake I/O.

No real input is ever sent: keyboard/pointer/inspector/gate are fakes that
record what the engine asked for.
"""

import threading

import pytest

from input_safety import Verdict
from keyboard_navigation import SHUFFLE_APPS, ActionResult
from pointer_controller import GlideOutcome
from scheduler import AutomationEngine, DisplayState, EngineConfig, PointerState
from window_detection import CLOSED, DISCONNECTED, MINIMIZED, NOT_FOREGROUND, OK, RdpWindow, status

TARGET = RdpWindow(42, 7, "mstsc.exe", "TscShellContainerClass", "host - Remote Desktop Connection", True)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeInspector:
    def __init__(self):
        self.code = OK

    def check(self, target, require_foreground=True):
        if target is None:
            return status("no_target")
        if self.code == NOT_FOREGROUND and not require_foreground:
            return status(OK)
        return status(self.code)


class FakeGate:
    def __init__(self, clock):
        self.clock = clock
        self.last_activity = None

    def observe(self):
        pass

    def session_verdict(self):
        return Verdict(True, "ok", "OK")

    def seconds_since_user_activity(self):
        return None if self.last_activity is None else self.clock() - self.last_activity

    def mark_user_activity(self):
        self.last_activity = self.clock()


class FakeKeyboard:
    def __init__(self):
        self.sent = []
        self.next_result = None

    def send_chord(self, target, keys, guard, repeat=1):
        if self.next_result is not None:
            r, self.next_result = self.next_result, None
            return r
        self.sent.append((keys, repeat))
        return ActionResult(True, "ok", f"Sent {keys}x{repeat}")


class FakeMonitor:
    def __init__(self):
        self.armed = False
        self.triggered = False
        self.reason = ""
        self.running = False
        self.install_error = ""

    def start(self):
        self.running = True
        return True

    def stop(self):
        self.running = False

    def arm(self):
        self.armed, self.triggered = True, False

    def disarm(self):
        self.armed, self.triggered = False, False


class FakePointer:
    def __init__(self, monitor):
        self.monitor = monitor
        self.glides = 0
        self.ready = True
        self.pos = (500, 400)
        self.outcome = None

    def _cursor(self):
        return self.pos

    def reset_anchor(self):
        pass

    def cursor_ready(self, target):
        return (self.ready, "" if self.ready else "outside")

    def glide(self, target, range_px, guard, cancel):
        self.glides += 1
        if self.outcome is not None:
            return self.outcome
        self.pos = (self.pos[0] + 5, self.pos[1])
        return GlideOutcome(ActionResult(True, "ok", "moved"), self.pos)


CFG = EngineConfig(interval_s=13 * 60, switch_shortcut="alt_pageup", scroll_key="page", scroll_direction="alternate",
                   pointer_enabled=False, action_gap_s=0)


@pytest.fixture
def env():
    clock = Clock()
    insp = FakeInspector()
    gate = FakeGate(clock)
    kb = FakeKeyboard()
    mon = FakeMonitor()
    ptr = FakePointer(mon)
    eng = AutomationEngine(insp, gate, kb, ptr, mon, CFG, clock=clock, cursor=ptr._cursor, start_thread=False)
    return eng, clock, insp, gate, kb, mon, ptr


def run_for(eng, clock, seconds, step=1.0):
    t = 0.0
    while t < seconds:
        clock.advance(step)
        t += step
        eng.step()


def test_start_requires_target(env):
    eng, *_ = env
    assert not eng.start()
    assert eng.snapshot().state == DisplayState.STOPPED


def test_start_validates_target(env):
    eng, clock, insp, *_ = env
    insp.code = MINIMIZED
    eng.set_target(TARGET)
    assert not eng.start()
    assert "minimized" in eng.snapshot().detail


def test_default_interval_is_13_minutes():
    assert EngineConfig().interval_s == 13 * 60


def test_cycle_runs_after_interval_then_alternates_scroll(env):
    eng, clock, insp, gate, kb, *_ = env
    eng.set_target(TARGET)
    assert eng.start()
    eng.step()
    snap = eng.snapshot()
    assert snap.state == DisplayState.RUNNING and snap.countdown_s == pytest.approx(780)
    run_for(eng, clock, 779)
    assert kb.sent == []
    run_for(eng, clock, 2)
    assert kb.sent == [(("alt", "pageup"), 1), (("pagedown",), 1)]
    run_for(eng, clock, 781)
    assert kb.sent[2:] == [(("alt", "pageup"), 1), (("pageup",), 1)]  # direction alternated
    assert eng.snapshot().cycles == 2


def test_shuffle_uses_alt_pageup_with_steps(env):
    eng, clock, insp, gate, kb, *_ = env
    eng.set_config(EngineConfig(interval_s=60, switch_shortcut=SHUFFLE_APPS, app_count=5, scroll_key="off",
                                pointer_enabled=False))
    eng.set_target(TARGET)
    eng.start()
    run_for(eng, clock, 60 * 10 + 5)
    assert len(kb.sent) == 10
    assert all(keys == ("alt", "pageup") and 1 <= rep <= 4 for keys, rep in kb.sent)


def test_focus_loss_suspends_and_never_sends(env):
    eng, clock, insp, gate, kb, *_ = env
    eng.set_target(TARGET)
    eng.start()
    run_for(eng, clock, 100)
    insp.code = NOT_FOREGROUND
    eng.step()
    snap = eng.snapshot()
    assert snap.state == DisplayState.UNAVAILABLE and snap.countdown_s is None
    run_for(eng, clock, 3600)
    assert kb.sent == []


def test_return_restarts_full_countdown_no_missed_actions(env):
    eng, clock, insp, gate, kb, *_ = env
    eng.set_target(TARGET)
    eng.start()
    run_for(eng, clock, 700)
    insp.code = MINIMIZED
    run_for(eng, clock, 3000)
    insp.code = OK
    eng.step()
    assert kb.sent == []
    assert eng.snapshot().countdown_s == pytest.approx(780)


def test_manual_resume_policy_pauses_on_return(env):
    eng, clock, insp, gate, kb, *_ = env
    eng.set_config(EngineConfig(**{**CFG.__dict__, "resume_policy": "manual"}))
    eng.set_target(TARGET)
    eng.start()
    eng.step()
    insp.code = DISCONNECTED
    eng.step()
    assert eng.snapshot().state == DisplayState.UNAVAILABLE
    insp.code = OK
    eng.step()
    assert eng.snapshot().state == DisplayState.PAUSED
    run_for(eng, clock, 3600)
    assert kb.sent == []
    eng.resume()
    eng.step()
    assert eng.snapshot().state == DisplayState.RUNNING


def test_pause_and_stop_cancel(env):
    eng, clock, insp, gate, kb, *_ = env
    eng.set_target(TARGET)
    eng.start()
    run_for(eng, clock, 10)
    cancel = eng._cancel
    eng.pause()
    assert cancel.is_set()
    run_for(eng, clock, 5000)
    assert kb.sent == [] and eng.snapshot().countdown_s is None
    eng.resume()
    eng.step()
    assert eng.snapshot().countdown_s == pytest.approx(780)
    eng.stop()
    run_for(eng, clock, 5000)
    assert kb.sent == []
    assert eng.snapshot().state == DisplayState.STOPPED


def test_double_start_is_a_noop(env):
    eng, clock, insp, gate, kb, *_ = env
    eng.set_target(TARGET)
    assert eng.start()
    eng.step()
    run_for(eng, clock, 400)
    assert eng.start()  # must not reset or duplicate the countdown
    assert eng.snapshot().countdown_s == pytest.approx(380)


def test_only_one_worker_thread():
    from scheduler import AutomationEngine as AE
    clock = Clock()
    mon = FakeMonitor()
    eng = AE(FakeInspector(), FakeGate(clock), FakeKeyboard(), FakePointer(mon), mon, CFG, tick_s=0.01)
    try:
        eng.set_target(TARGET)
        for _ in range(5):
            eng.start()
        names = [t.name for t in threading.enumerate() if t.name == "rdpnexus-engine"]
        assert len(names) == 1
    finally:
        eng.shutdown()
    assert not any(t.name == "rdpnexus-engine" and t.is_alive() for t in threading.enumerate())


def test_long_gap_does_not_fire_immediately(env):
    eng, clock, insp, gate, kb, *_ = env
    eng.set_target(TARGET)
    eng.start()
    run_for(eng, clock, 700)
    clock.advance(4000)  # e.g. laptop slept
    eng.step()
    assert kb.sent == []
    assert eng.snapshot().countdown_s == pytest.approx(780)


def test_closed_window_stops_automation(env):
    eng, clock, insp, gate, kb, *_ = env
    eng.set_target(TARGET)
    eng.start()
    insp.code = CLOSED
    eng.step()
    snap = eng.snapshot()
    assert snap.state == DisplayState.STOPPED and "closed" in snap.detail


def test_target_cannot_change_while_running(env):
    eng, *_ = env
    eng.set_target(TARGET)
    eng.start()
    assert not eng.set_target(None)
    assert eng.target == TARGET


def test_keyboard_disabled_means_no_countdown(env):
    eng, clock, insp, gate, kb, *_ = env
    eng.set_config(EngineConfig(**{**CFG.__dict__, "keyboard_enabled": False}))
    eng.set_target(TARGET)
    eng.start()
    run_for(eng, clock, 3000)
    assert kb.sent == [] and eng.snapshot().countdown_s is None


def test_skipped_action_reports_reason_and_restarts(env):
    eng, clock, insp, gate, kb, *_ = env
    eng.set_target(TARGET)
    eng.start()
    run_for(eng, clock, 779)
    kb.next_result = ActionResult(False, "user_active", "Skipped Alt+Page Up: You used the keyboard")
    run_for(eng, clock, 2)
    snap = eng.snapshot()
    assert "Skipped" in snap.detail and kb.sent == []
    assert snap.countdown_s == pytest.approx(779, abs=1)


def test_focus_change_during_send_suspends(env):
    eng, clock, insp, gate, kb, *_ = env
    eng.set_target(TARGET)
    eng.start()
    run_for(eng, clock, 779)
    kb.next_result = ActionResult(False, "focus_changed", "Focus left the RDP window")
    run_for(eng, clock, 2)
    assert eng.snapshot().state == DisplayState.UNAVAILABLE


# --- pointer --------------------------------------------------------------------------

PCFG = EngineConfig(keyboard_enabled=False, pointer_enabled=True, pointer_interval_s=60, pointer_rest_s=3)


def start_pointer(env):
    eng, clock, insp, gate, kb, mon, ptr = env
    eng.set_config(PCFG)
    eng.set_target(TARGET)
    eng.start()
    return eng, clock, insp, gate, kb, mon, ptr


def test_pointer_arms_after_rest_and_moves(env):
    eng, clock, insp, gate, kb, mon, ptr = start_pointer(env)
    gate.mark_user_activity()
    eng.step()
    assert eng.snapshot().pointer_state == PointerState.ARMING
    run_for(eng, clock, 4)
    assert eng.snapshot().pointer_state == PointerState.ACTIVE and mon.armed
    run_for(eng, clock, 61)
    assert ptr.glides == 1


def test_pointer_waits_while_cursor_outside(env):
    eng, clock, insp, gate, kb, mon, ptr = start_pointer(env)
    ptr.ready = False
    run_for(eng, clock, 300)
    assert eng.snapshot().pointer_state == PointerState.ARMING and ptr.glides == 0


def test_manual_movement_suspends_until_explicit_resume(env):
    eng, clock, insp, gate, kb, mon, ptr = start_pointer(env)
    run_for(eng, clock, 2)
    assert eng.snapshot().pointer_state == PointerState.ACTIVE
    mon.triggered, mon.reason = True, "mouse moved"
    eng.step()
    assert eng.snapshot().pointer_state == PointerState.MANUAL
    run_for(eng, clock, 3600)
    assert ptr.glides == 0 and eng.snapshot().pointer_state == PointerState.MANUAL
    eng.resume_pointer()
    assert eng.snapshot().pointer_state == PointerState.ARMING


def test_polling_detects_cursor_moved_by_user(env):
    eng, clock, insp, gate, kb, mon, ptr = start_pointer(env)
    run_for(eng, clock, 2)
    ptr.pos = (700, 400)  # moved without the hook noticing
    eng.step()
    assert eng.snapshot().pointer_state == PointerState.MANUAL


def test_user_movement_while_unavailable_counts_as_manual(env):
    eng, clock, insp, gate, kb, mon, ptr = start_pointer(env)
    run_for(eng, clock, 2)
    insp.code = NOT_FOREGROUND
    eng.step()
    assert eng.snapshot().pointer_state == PointerState.SUSPENDED
    mon.triggered, mon.reason = True, "mouse moved"
    eng.step()
    insp.code = OK
    run_for(eng, clock, 300)
    assert eng.snapshot().pointer_state == PointerState.MANUAL and ptr.glides == 0


def test_pointer_fault_disables_feature(env):
    eng, clock, insp, gate, kb, mon, ptr = start_pointer(env)
    run_for(eng, clock, 2)
    ptr.outcome = GlideOutcome(ActionResult(False, "fault", "outside"), (1, 1))
    run_for(eng, clock, 61)
    assert eng.snapshot().pointer_state == PointerState.FAULT
    run_for(eng, clock, 600)
    assert ptr.glides == 1


def test_pause_disarms_pointer(env):
    eng, clock, insp, gate, kb, mon, ptr = start_pointer(env)
    run_for(eng, clock, 2)
    eng.pause()
    assert not mon.armed
    run_for(eng, clock, 600)
    assert ptr.glides == 0
    eng.stop()
    eng.step()
    assert not mon.running  # hook removed when stopped
