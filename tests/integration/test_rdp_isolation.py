"""Interactive integration tests: real windows, real focus, real SendInput, real mouse hook.

They take over this desktop for about a minute (focus changes, the pointer
moves, and keystrokes are sent to the *fake* RDP window).  Do not touch the
mouse or keyboard while they run.  Opt in with:

    set RDPNEXUS_INTERACTIVE_TESTS=1
    python -m pytest tests/integration -v

The fake RDP window is a stand-in with the real mstsc window classes; it lets
us verify isolation (which window receives input) end to end.  It does not
replace a test against a real Remote Desktop session (see docs/TESTING.md).
"""

from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import threading
import time

import pytest

pytestmark = [
    pytest.mark.interactive,
    pytest.mark.skipif(os.environ.get("RDPNEXUS_INTERACTIVE_TESTS") != "1",
                       reason="interactive desktop test; set RDPNEXUS_INTERACTIVE_TESTS=1 to run"),
]

if sys.platform == "win32":
    import winapi as w
    from input_safety import InjectionLedger, SafetyGate
    from keyboard_navigation import SHUFFLE_APPS, KeyboardNavigator
    from pointer_controller import ManualMovementMonitor, PointerController, permitted_area
    from scheduler import AutomationEngine, DisplayState, EngineConfig, PointerState
    from window_detection import RDP_PROCESS_NAMES, WindowInspector

    w.enable_dpi_awareness()

HERE = os.path.dirname(os.path.abspath(__file__))


class FakeApp:
    def __init__(self):
        self.proc = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_rdp_app.py")], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, bufsize=1)
        self.events: queue.Queue = queue.Queue()
        self.log: list[dict] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()
        self.ready = self.wait_for("ready", 10)

    def _pump(self):
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            with self._lock:
                self.log.append(ev)
            self.events.put(ev)

    def wait_for(self, kind, timeout=5.0, **match):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ev = self.events.get(timeout=0.1)
            except queue.Empty:
                continue
            if ev.get("event") == kind and all(ev.get(k) == v for k, v in match.items()):
                return ev
        raise TimeoutError(f"no {kind} {match}")

    def command(self, cmd):
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()
        return self.wait_for("done", 5, command=cmd)

    def focus(self, which):
        self.proc.stdin.write(f"focus {which}\n")
        self.proc.stdin.flush()
        ev = self.wait_for("focus", 5, target=which)
        self.wait_for("done", 5)
        return ev

    def mark(self):
        with self._lock:
            return len(self.log)

    def since(self, mark, kind=None, window=None):
        with self._lock:
            items = self.log[mark:]
        return [e for e in items if (kind is None or e["event"] == kind) and (window is None or e["window"] == window)]

    def close(self):
        try:
            self.proc.stdin.write("quit\n")
            self.proc.stdin.flush()
            self.proc.wait(5)
        except Exception:
            self.proc.kill()


@pytest.fixture
def app():
    saved = w.cursor_pos()
    fake = FakeApp()
    for _attempt in range(5):  # Windows' foreground lock can refuse the first request
        if fake.focus("rdp")["foreground"]:
            break
        time.sleep(0.4)
    else:
        fake.close()
        pytest.skip("Windows refused to give the test window the foreground (foreground lock); "
                    "run the tests from a terminal you are actively using")
    yield fake
    fake.close()
    w.user32.SetCursorPos(*saved)


def build(fake, **cfg):
    inspector = WindowInspector(process_names=RDP_PROCESS_NAMES | {fake.ready["process"]})
    ledger = InjectionLedger()
    gate = SafetyGate(inspector, ledger)
    monitor = ManualMovementMonitor()
    keyboard = KeyboardNavigator(gate, ledger)
    pointer = PointerController(inspector, gate, ledger, monitor)
    base = dict(interval_s=1.5, switch_shortcut=SHUFFLE_APPS, app_count=3, scroll_key="page",
                scroll_direction="alternate", pointer_enabled=False, idle_guard_s=0.3, pointer_rest_s=0.3,
                action_gap_s=0.2, pointer_interval_s=0.8, pointer_range_px=20)
    base.update(cfg)
    engine = AutomationEngine(inspector, gate, keyboard, pointer, monitor, EngineConfig(**base), tick_s=0.05)
    target = inspector.describe(fake.ready["rdp"])
    assert target is not None, "fake RDP window should be recognised as an RDP session window"
    assert engine.set_target(target)
    return engine, inspector, target


def wait_state(engine, state, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if engine.snapshot().state == state:
            return True
        time.sleep(0.02)
    return False


def keys_of(events):
    return [(e["scan"], e["extended"], e["down"]) for e in events]


ALT, PGUP, PGDN = (0x38, False), (0x49, True), (0x51, True)


def test_connection_dialog_and_local_windows_are_rejected(app):
    inspector = WindowInspector(process_names=RDP_PROCESS_NAMES | {app.ready["process"]})
    assert inspector.describe(app.ready["local"]) is None
    assert "not an RDP session window" in inspector.explain_rejection(app.ready["local"])
    assert WindowInspector().describe(app.ready["rdp"]) is None  # python.exe is not an RDP client


def test_keys_reach_only_the_rdp_window_with_exact_sequence(app):
    engine, _insp, _t = build(app)
    try:
        mark = app.mark()
        assert engine.start()
        assert wait_state(engine, DisplayState.RUNNING)
        time.sleep(3.6)  # two cycles at 1.5 s
        engine.stop()
        time.sleep(0.3)
        rdp_keys = keys_of(app.since(mark, "key", "rdp"))
        assert app.since(mark, "key", "local") == []
        assert app.since(mark, "click") == []
        # Parse cycles: Alt down, (PgUp down/up) x k, Alt up, then one scroll key down/up.
        cycles, i = [], 0
        while i < len(rdp_keys):
            assert rdp_keys[i] == (*ALT, True), rdp_keys
            i += 1
            k = 0
            while rdp_keys[i] == (*PGUP, True):
                assert rdp_keys[i + 1] == (*PGUP, False)
                k += 1
                i += 2
            assert rdp_keys[i] == (*ALT, False)
            i += 1
            scroll_down, scroll_up = rdp_keys[i], rdp_keys[i + 1]
            assert scroll_down[2] and not scroll_up[2] and scroll_down[:2] == scroll_up[:2]
            cycles.append((k, scroll_down[:2]))
            i += 2
        assert len(cycles) == 2, cycles
        assert all(1 <= k <= 2 for k, _ in cycles)            # app_count=3 -> 1 or 2 presses, never 0
        assert [s for _, s in cycles] == [PGDN, PGUP]           # alternating scroll
    finally:
        engine.shutdown()


def test_switching_to_local_app_suspends_and_sends_nothing(app):
    engine, _insp, _t = build(app)
    try:
        assert engine.start()
        assert wait_state(engine, DisplayState.RUNNING)
        assert app.focus("local")["foreground"]
        assert wait_state(engine, DisplayState.UNAVAILABLE, 1.0)
        mark = app.mark()
        time.sleep(4.0)  # well past the 1.5 s interval
        assert app.since(mark, "key") == []
        assert "not the active window" in engine.snapshot().detail
        # Back to RDP: fresh countdown, nothing sent immediately.
        mark = app.mark()
        assert app.focus("rdp")["foreground"]
        assert wait_state(engine, DisplayState.RUNNING, 1.0)
        time.sleep(1.0)
        assert app.since(mark, "key") == []
        time.sleep(1.2)
        assert app.since(mark, "key", "rdp") != []
        assert app.since(mark, "key", "local") == []
    finally:
        engine.shutdown()


def test_no_input_when_local_app_is_active_from_the_start(app):
    engine, _insp, _t = build(app, pointer_enabled=True)
    try:
        assert app.focus("local")["foreground"]
        before = w.cursor_pos()
        mark = app.mark()
        assert engine.start()
        time.sleep(3.5)
        assert engine.snapshot().state == DisplayState.UNAVAILABLE
        assert app.since(mark, "key") == [] and app.since(mark, "click") == []
        assert w.cursor_pos() == before
    finally:
        engine.shutdown()


@pytest.mark.parametrize("command,expected", [
    ("minimize rdp", "minimized"),
    ("dialog", "dialog"),
    ("hide input", "disconnected"),
])
def test_unavailable_conditions_suspend(app, command, expected):
    engine, _insp, _t = build(app)
    try:
        assert engine.start()
        assert wait_state(engine, DisplayState.RUNNING)
        app.command(command)
        assert wait_state(engine, DisplayState.UNAVAILABLE, 1.0)
        assert expected in engine.snapshot().detail.lower()
        mark = app.mark()
        time.sleep(2.5)
        assert app.since(mark, "key") == []
    finally:
        engine.shutdown()


def test_closing_rdp_window_stops(app):
    engine, _insp, _t = build(app)
    try:
        assert engine.start()
        assert wait_state(engine, DisplayState.RUNNING)
        app.command("close rdp")
        assert wait_state(engine, DisplayState.STOPPED, 1.0)
        assert "closed" in engine.snapshot().detail
    finally:
        engine.shutdown()


def _center_cursor(inspector, target):
    area = permitted_area(inspector.input_area(target))
    w.user32.SetCursorPos(*area.center)
    time.sleep(0.3)  # let Windows deliver the mouse-move it synthesises for windows under the cursor
    return area


def _user_nudge(dx):
    """Simulate a user's mouse movement: injected WITHOUT RDP Nexus's signature."""
    inp = w.INPUT(type=w.INPUT_MOUSE)
    inp.mi = w.MOUSEINPUT(dx=dx, dy=0, mouseData=0, dwFlags=w.MOUSEEVENTF_MOVE, time=0, dwExtraInfo=0)
    assert w.send_inputs([inp]) == 1


def test_pointer_stays_inside_area_and_manual_movement_wins(app):
    engine, inspector, target = build(app, keyboard_enabled=False, pointer_enabled=True)
    try:
        area = _center_cursor(inspector, target)
        mark = app.mark()
        assert engine.start()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and len(app.since(mark, "move", "rdp")) < 25:
            time.sleep(0.05)
        moves = app.since(mark, "move")
        assert len(moves) >= 10, "pointer automation did not move"
        assert all(m["window"] == "rdp" and area.contains(m["x"], m["y"]) for m in moves)
        assert max(math.dist((m["x"], m["y"]), area.center) for m in moves) <= 21
        assert app.since(mark, "click") == []
        assert engine.snapshot().pointer_state == PointerState.ACTIVE

        _user_nudge(+7)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and engine.snapshot().pointer_state != PointerState.MANUAL:
            time.sleep(0.01)
        assert engine.snapshot().pointer_state == PointerState.MANUAL
        parked = w.cursor_pos()
        time.sleep(2.5)  # several pointer intervals
        assert w.cursor_pos() == parked, "automation moved the pointer after manual control"

        engine.resume_pointer()
        assert engine.snapshot().pointer_state == PointerState.ARMING
    finally:
        engine.shutdown()


def test_focus_loss_stops_pointer(app):
    engine, inspector, target = build(app, keyboard_enabled=False, pointer_enabled=True, pointer_interval_s=0.4)
    try:
        _center_cursor(inspector, target)
        assert engine.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and engine.snapshot().pointer_state != PointerState.ACTIVE:
            time.sleep(0.02)
        assert app.focus("local")["foreground"]
        time.sleep(0.2)
        parked = w.cursor_pos()
        time.sleep(2.0)
        assert w.cursor_pos() == parked
        assert engine.snapshot().state == DisplayState.UNAVAILABLE
    finally:
        engine.shutdown()


def test_stop_cancels_a_glide_in_progress(app):
    engine, inspector, target = build(app, keyboard_enabled=False, pointer_enabled=True, pointer_interval_s=0.3,
                                      pointer_range_px=60)
    try:
        _center_cursor(inspector, target)
        assert engine.start()
        deadline = time.monotonic() + 5
        start_pos = w.cursor_pos()
        while time.monotonic() < deadline and w.cursor_pos() == start_pos:
            time.sleep(0.005)
        assert w.cursor_pos() != start_pos, "glide never started"
        engine.stop()
        time.sleep(0.06)
        stopped_at = w.cursor_pos()
        time.sleep(1.0)
        assert w.cursor_pos() == stopped_at
        assert engine.snapshot().state == DisplayState.STOPPED
    finally:
        engine.shutdown()
