"""Gentle pointer movement inside the RDP client area, and manual-movement detection.

Manual movement is detected two independent ways:

1. A low-level mouse hook (WH_MOUSE_LL) on a dedicated thread.  Every event we
   inject carries INJECTION_SIGNATURE in dwExtraInfo; any mouse event that is
   not injected-with-our-signature (a real mouse, a touchpad, or other
   software) counts as manual control.
2. Cursor-position polling: between our own moves the cursor must stay exactly
   where we left it.

Either one suspends pointer automation immediately and it stays suspended
until the user explicitly resumes it.  If the hook stops observing our own
events, detection is considered broken and pointer automation is disabled.
"""

from __future__ import annotations

import ctypes
import logging
import math
import random
import threading
import time
from dataclasses import dataclass
from ctypes import wintypes

import winapi as w
from input_safety import INJECTION_SIGNATURE, InjectionLedger, SafetyGate
from keyboard_navigation import ActionResult
from window_detection import Rect, RdpWindow, WindowInspector

log = logging.getLogger(__name__)

STEP_INTERVAL_S = 0.016  # ~60 Hz
MIN_STEPS, MAX_STEPS = 12, 45


# --- geometry (pure) ---------------------------------------------------------------
def permitted_area(input_rect: Rect | None) -> Rect | None:
    """Inset the RDP input area so we stay clear of edges (remote taskbar,
    full-screen connection bar, hot corners) and of rounding at the border."""
    if input_rect is None:
        return None
    margin = max(12, min(48, int(min(input_rect.width, input_rect.height) * 0.06)))
    return input_rect.inset(margin)


def clamp(x: int, y: int, rect: Rect) -> tuple[int, int]:
    return (min(max(x, rect.left), rect.right - 1), min(max(y, rect.top), rect.bottom - 1))


def pick_destination(anchor: tuple[int, int], current: tuple[int, int], range_px: int,
                     area: Rect, rng: random.Random) -> tuple[int, int]:
    """A random point within `range_px` of the anchor, inside `area`, and visibly
    different from the current position (so the move is not a no-op)."""
    min_move = max(2, range_px // 3)
    best = current
    for _ in range(40):
        angle = rng.uniform(0, 2 * math.pi)
        radius = rng.uniform(range_px * 0.4, range_px)
        x, y = clamp(round(anchor[0] + radius * math.cos(angle)),
                     round(anchor[1] + radius * math.sin(angle)), area)
        if math.dist((x, y), current) >= min_move:
            return x, y
        best = (x, y)
    return best


def glide_path(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """Smooth (ease-in-out) path from start to end, excluding the start point."""
    distance = math.dist(start, end)
    steps = int(min(MAX_STEPS, max(MIN_STEPS, distance / 1.5)))
    points: list[tuple[int, int]] = []
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)  # smoothstep
        p = (round(start[0] + (end[0] - start[0]) * e), round(start[1] + (end[1] - start[1]) * e))
        if not points or points[-1] != p:
            points.append(p)
    if points and points[-1] != end:
        points.append(end)
    return [p for p in points if p != start] or [end]


def to_absolute(x: int, y: int, vscreen: tuple[int, int, int, int]) -> tuple[int, int]:
    """Map a physical pixel to SendInput's 0..65535 virtual-desktop coordinates."""
    left, top, width, height = vscreen
    nx = round((x - left) * 65535 / max(1, width - 1))
    ny = round((y - top) * 65535 / max(1, height - 1))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


# --- manual movement monitor ----------------------------------------------------------
class ManualMovementMonitor:
    """Owns the low-level mouse hook thread.  The hook callback only sets flags."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._hook = None
        self._proc = None  # keep the ctypes callback alive
        self._armed = False
        self._triggered = threading.Event()
        self._reason = ""
        self._own_events = 0
        self._install_error = ""

    # lifecycle -----------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and bool(self._hook)

    @property
    def install_error(self) -> str:
        return self._install_error

    def start(self) -> bool:
        if self.running:
            return True
        self._ready.clear()
        self._install_error = ""
        self._thread = threading.Thread(target=self._run, name="rdpnexus-mouse-hook", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)
        return self.running

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        if self._thread_id:
            w.PostThreadMessageW(self._thread_id, w.WM_QUIT, 0, 0)
        thread.join(2.0)
        self._thread = None
        self._thread_id = 0
        self.disarm()

    def _run(self) -> None:
        self._thread_id = int(w.GetCurrentThreadId())
        msg = wintypes.MSG()
        w.PeekMessageW(ctypes.byref(msg), None, 0, 0, w.PM_NOREMOVE)  # create the queue
        self._proc = w.HOOKPROC(self._callback)
        self._hook = w.SetWindowsHookExW(w.WH_MOUSE_LL, self._proc, w.GetModuleHandleW(None), 0)
        if not self._hook:
            self._install_error = f"SetWindowsHookEx failed (error {ctypes.get_last_error()})"
            log.error("Mouse hook: %s", self._install_error)
            self._ready.set()
            return
        self._ready.set()
        try:
            while w.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
        finally:
            w.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def _callback(self, code, wparam, lparam):
        try:
            if code == w.HC_ACTION:
                info = ctypes.cast(lparam, ctypes.POINTER(w.MSLLHOOKSTRUCT)).contents
                ours = bool(info.flags & w.LLMHF_INJECTED) and info.dwExtraInfo == INJECTION_SIGNATURE
                if ours:
                    self._own_events += 1
                elif self._armed:
                    self._reason = ("mouse moved" if wparam == w.WM_MOUSEMOVE else
                                    "mouse wheel used" if wparam in (w.WM_MOUSEWHEEL, w.WM_MOUSEHWHEEL) else
                                    "mouse button used")
                    self._triggered.set()
        except Exception:  # never let an exception escape into the hook chain
            pass
        return w.CallNextHookEx(None, code, wparam, lparam)

    # detection state ------------------------------------------------------------------
    def arm(self) -> None:
        with self._lock:
            self._triggered.clear()
            self._reason = ""
            self._armed = True

    def disarm(self) -> None:
        with self._lock:
            self._armed = False
            self._triggered.clear()

    @property
    def armed(self) -> bool:
        return self._armed

    def trigger(self, reason: str) -> None:
        if self._armed:
            self._reason = reason
            self._triggered.set()

    @property
    def triggered(self) -> bool:
        return self._triggered.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def own_events_seen(self) -> int:
        return self._own_events


# --- controller --------------------------------------------------------------------
@dataclass(frozen=True)
class GlideOutcome:
    result: ActionResult
    final_pos: tuple[int, int] | None = None


class PointerController:
    def __init__(self, inspector: WindowInspector, gate: SafetyGate, ledger: InjectionLedger,
                 monitor: ManualMovementMonitor, send=w.send_inputs, cursor=w.cursor_pos,
                 vscreen=w.virtual_screen, rng: random.Random | None = None, sleep_step=STEP_INTERVAL_S):
        self.inspector = inspector
        self.gate = gate
        self.ledger = ledger
        self.monitor = monitor
        self._send = send
        self._cursor = cursor
        self._vscreen = vscreen
        self._rng = rng or random.Random()
        self._step = sleep_step
        self.anchor: tuple[int, int] | None = None

    def reset_anchor(self) -> None:
        self.anchor = None

    def area(self, target: RdpWindow) -> Rect | None:
        return permitted_area(self.inspector.input_area(target))

    def cursor_ready(self, target: RdpWindow) -> tuple[bool, str]:
        """Is the cursor resting somewhere we are allowed to move it from?"""
        area = self.area(target)
        if area is None:
            return False, "RDP input area is not available"
        x, y = self._cursor()
        if not area.contains(x, y):
            return False, "Pointer is outside the RDP window — rest it over the remote desktop"
        if self.inspector.root_at(x, y) != target.hwnd:
            return False, "Another window covers the pointer position"
        return True, ""

    def _point_allowed(self, target: RdpWindow, area: Rect, x: int, y: int) -> bool:
        return (area.contains(x, y)
                and self.inspector.point_on_monitor(x, y)
                and self.inspector.root_at(x, y) == target.hwnd)

    def _move_input(self, x: int, y: int) -> w.INPUT:
        nx, ny = to_absolute(x, y, self._vscreen())
        inp = w.INPUT(type=w.INPUT_MOUSE)
        inp.mi = w.MOUSEINPUT(dx=nx, dy=ny, mouseData=0,
                              dwFlags=w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE | w.MOUSEEVENTF_VIRTUALDESK,
                              time=0, dwExtraInfo=INJECTION_SIGNATURE)
        return inp

    def glide(self, target: RdpWindow, range_px: int, idle_guard_s: float,
              cancel: threading.Event) -> GlideOutcome:
        verdict = self.gate.check_pointer(target, idle_guard_s)
        if not verdict.ok:
            return GlideOutcome(ActionResult(False, verdict.code, f"Pointer move skipped: {verdict.message}"))
        ready, why = self.cursor_ready(target)
        if not ready:
            return GlideOutcome(ActionResult(False, "outside", f"Pointer move skipped: {why}"))
        area = self.area(target)
        start = self._cursor()
        if self.anchor is None or not area.contains(*self.anchor):
            self.anchor = start
        dest = pick_destination(self.anchor, start, range_px, area, self._rng)
        path = glide_path(start, dest)
        own_before = self.monitor.own_events_seen

        for x, y in path:
            if cancel.is_set():
                return GlideOutcome(ActionResult(False, "cancelled", "Pointer move cancelled"), self._cursor())
            if self.monitor.triggered:
                return GlideOutcome(ActionResult(False, "manual", f"Manual control: {self.monitor.reason}"))
            if not self._point_allowed(target, area, x, y) or not self.gate.still_foreground(target):
                return GlideOutcome(ActionResult(False, "aborted",
                                                 "Pointer move stopped: RDP window no longer valid at the next point"),
                                    self._cursor())
            if self._send([self._move_input(x, y)]) != 1:
                return GlideOutcome(ActionResult(False, "send_failed", "Pointer move was blocked by Windows"))
            self.ledger.record()
            if cancel.wait(self._step):
                return GlideOutcome(ActionResult(False, "cancelled", "Pointer move cancelled"), self._cursor())

        # Let the hook thread see the last events before judging it.
        time.sleep(0.05)
        final = self._cursor()
        if self.monitor.triggered:
            return GlideOutcome(ActionResult(False, "manual", f"Manual control: {self.monitor.reason}"))
        if not area.contains(*final):
            return GlideOutcome(ActionResult(False, "fault",
                                             f"Pointer ended at {final}, outside the permitted area; "
                                             "pointer automation disabled for safety"), final)
        if math.dist(final, path[-1]) > 2:
            return GlideOutcome(ActionResult(False, "manual", "Manual control: pointer position changed"), final)
        if self.monitor.own_events_seen == own_before:
            return GlideOutcome(ActionResult(False, "fault",
                                             "Manual-movement detection stopped responding; "
                                             "pointer automation disabled for safety"), final)
        return GlideOutcome(ActionResult(True, "ok", f"Pointer moved gently to {final[0]}, {final[1]}"), final)
