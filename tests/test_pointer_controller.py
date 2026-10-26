import math
import random
import threading

import pytest

import winapi as w
from input_safety import INJECTION_SIGNATURE, Verdict
from pointer_controller import PointerController, glide_path, permitted_area, pick_destination, to_absolute
from window_detection import Rect, RdpWindow

AREA = Rect(100, 100, 900, 700)


def test_permitted_area_is_inset_and_inside():
    inner = permitted_area(AREA)
    assert inner.left > AREA.left and inner.top > AREA.top
    assert inner.right < AREA.right and inner.bottom < AREA.bottom
    assert permitted_area(Rect(0, 0, 20, 20)) is None  # too small to move safely
    assert permitted_area(None) is None


@pytest.mark.parametrize("seed", range(50))
def test_destination_within_range_and_area(seed):
    rng = random.Random(seed)
    area = permitted_area(AREA)
    anchor = (rng.randint(area.left, area.right - 1), rng.randint(area.top, area.bottom - 1))
    for _ in range(20):
        dest = pick_destination(anchor, anchor, 20, area, rng)
        assert area.contains(*dest)
        assert math.dist(dest, anchor) <= 20 + 1


def test_destination_clamped_near_edge():
    area = permitted_area(AREA)
    corner = (area.left, area.top)
    rng = random.Random(0)
    for _ in range(100):
        assert area.contains(*pick_destination(corner, corner, 150, area, rng))


def test_glide_is_smooth_and_ends_at_destination():
    path = glide_path((300, 300), (318, 288))
    assert path[-1] == (318, 288)
    assert (300, 300) not in path
    steps = [(300, 300)] + path
    assert max(math.dist(a, b) for a, b in zip(steps, steps[1:])) <= 4  # gentle, no jumps
    assert 6 <= len(path) <= 45


def test_to_absolute_maps_virtual_desktop_corners():
    vs = (-1366, 0, 3286, 1080)
    assert to_absolute(-1366, 0, vs) == (0, 0)
    assert to_absolute(1919, 1079, vs) == (65535, 65535)
    x, y = to_absolute(0, 540, vs)
    assert abs(x - 1366 * 65535 / 3285) < 1 and abs(y - 540 * 65535 / 1079) < 1


# --- controller with fakes --------------------------------------------------------------

TARGET = RdpWindow(hwnd=42, pid=7, process_name="mstsc.exe", class_name="TscShellContainerClass",
                   title="host - Remote Desktop Connection", has_input_child=True)


class FakeInspector:
    def __init__(self, area=AREA, root=42):
        self.area = area
        self.root = root

    def input_area(self, target):
        return self.area

    def root_at(self, x, y):
        return self.root

    def point_on_monitor(self, x, y):
        return True


class FakeGate:
    def __init__(self, verdict=Verdict(True, "ok", "OK"), foreground=True):
        self.verdict = verdict
        self.foreground = foreground

    def check_pointer(self, target, guard):
        return self.verdict

    def still_foreground(self, target):
        return self.foreground


class FakeMonitor:
    def __init__(self, sees_own=True):
        self.triggered = False
        self.reason = ""
        self.own_events_seen = 0
        self.sees_own = sees_own


class FakeLedger:
    def record(self):
        pass


class Mouse:
    """Fake cursor + SendInput that moves the cursor like Windows would."""

    def __init__(self, pos, monitor, vs=(0, 0, 1920, 1080)):
        self.pos = pos
        self.monitor = monitor
        self.vs = vs
        self.sent = []

    def cursor(self):
        return self.pos

    def send(self, inputs):
        for inp in inputs:
            assert inp.type == w.INPUT_MOUSE
            assert inp.mi.dwExtraInfo == INJECTION_SIGNATURE
            assert not inp.mi.dwFlags & ~(w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE | w.MOUSEEVENTF_VIRTUALDESK), \
                "only move flags allowed (never clicks)"
            left, top, width, height = self.vs
            self.pos = (round(inp.mi.dx * (width - 1) / 65535) + left, round(inp.mi.dy * (height - 1) / 65535) + top)
            self.sent.append(self.pos)
            if self.monitor.sees_own:
                self.monitor.own_events_seen += 1
        return len(inputs)


def make(pos=(500, 400), inspector=None, gate=None, sees_own=True):
    mon = FakeMonitor(sees_own)
    mouse = Mouse(pos, mon)
    pc = PointerController(inspector or FakeInspector(), gate or FakeGate(), FakeLedger(), mon, send=mouse.send,
                           cursor=mouse.cursor, vscreen=lambda: mouse.vs, rng=random.Random(5), sleep_step=0)
    return pc, mouse, mon


def test_glide_moves_inside_area_only():
    pc, mouse, _ = make()
    out = pc.glide(TARGET, 20, 10, threading.Event())
    assert out.result.ok, out.result.message
    area = permitted_area(AREA)
    assert mouse.sent and all(area.contains(*p) for p in mouse.sent)
    assert max(math.dist(p, (500, 400)) for p in mouse.sent) <= 21


def test_glide_skips_when_cursor_outside_area():
    pc, mouse, _ = make(pos=(50, 50))
    out = pc.glide(TARGET, 20, 10, threading.Event())
    assert not out.result.ok and out.result.code == "outside"
    assert mouse.sent == []


def test_glide_skips_when_other_window_covers_cursor():
    pc, mouse, _ = make(inspector=FakeInspector(root=99))
    assert pc.glide(TARGET, 20, 10, threading.Event()).result.code == "outside"
    assert mouse.sent == []


def test_glide_respects_gate():
    pc, mouse, _ = make(gate=FakeGate(verdict=Verdict(False, "not_foreground", "not active")))
    assert pc.glide(TARGET, 20, 10, threading.Event()).result.code == "not_foreground"
    assert mouse.sent == []


def test_glide_cancel_stops_immediately():
    pc, mouse, _ = make()
    cancel = threading.Event()
    cancel.set()
    assert pc.glide(TARGET, 20, 10, cancel).result.code == "cancelled"
    assert mouse.sent == []


def test_glide_aborts_on_manual_movement():
    pc, mouse, mon = make()
    original = mouse.send

    def send(inputs):
        n = original(inputs)
        if len(mouse.sent) == 3:
            mon.triggered, mon.reason = True, "mouse moved"
        return n

    pc._send = send
    out = pc.glide(TARGET, 20, 10, threading.Event())
    assert out.result.code == "manual"
    assert len(mouse.sent) == 3


def test_glide_aborts_when_focus_lost_mid_move():
    gate = FakeGate()
    pc, mouse, _ = make(gate=gate)
    original = mouse.send

    def send(inputs):
        n = original(inputs)
        gate.foreground = False
        return n

    pc._send = send
    assert pc.glide(TARGET, 20, 10, threading.Event()).result.code == "aborted"
    assert len(mouse.sent) == 1


def test_glide_fault_when_hook_is_blind():
    pc, _mouse, _ = make(sees_own=False)
    assert pc.glide(TARGET, 20, 10, threading.Event()).result.code == "fault"
