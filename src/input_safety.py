"""The safety gate every automated input must pass immediately before it is sent.

Design notes (see README "How input isolation works"):

* SendInput delivers events to whichever window owns the foreground *when the
  events are processed*, so a foreground check can never fully remove the race
  between "check" and "deliver".  We shrink that window as far as the API
  allows: full validation, then a final foreground check, then ONE SendInput
  call for the whole chord (SendInput guarantees the events of one call are not
  interleaved with other input), then a post-delivery check.  If focus changed
  around the send, automation is suspended (fail closed).
* We never send while the user holds a modifier key or mouse button (it would
  combine with our keys), while the workstation is locked or a screen saver is
  running, or shortly after real user activity.
"""

from __future__ import annotations

import ctypes
import threading
import time
from dataclasses import dataclass
from ctypes import wintypes

import winapi as w
from window_detection import RdpWindow, WindowInspector

# Stamped on dwExtraInfo of every event we inject ("RNXS"), so our own events
# can be told apart from the user's (and other software's) in the mouse hook.
INJECTION_SIGNATURE = 0x524E5853

# A GetLastInputInfo timestamp this close to our own injection is treated as ours.
_OWN_INPUT_TOLERANCE_MS = 300

_HELD_KEYS = (
    (w.VK_SHIFT, "Shift"),
    (w.VK_CONTROL, "Ctrl"),
    (w.VK_MENU, "Alt"),
    (w.VK_LWIN, "Win"),
    (w.VK_RWIN, "Win"),
)
_HELD_BUTTONS = (
    (w.VK_LBUTTON, "left mouse button"),
    (w.VK_RBUTTON, "right mouse button"),
    (w.VK_MBUTTON, "middle mouse button"),
    (w.VK_XBUTTON1, "mouse X1 button"),
    (w.VK_XBUTTON2, "mouse X2 button"),
)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    code: str
    message: str


ALLOWED = Verdict(True, "ok", "OK")


class InjectionLedger:
    """Remembers when RDP Nexus itself last injected input (GetTickCount ms)."""

    def __init__(self, tick=w.tick_count):
        self._tick = tick
        self._lock = threading.Lock()
        self._last: int | None = None

    def record(self) -> None:
        with self._lock:
            self._last = self._tick()

    @property
    def last(self) -> int | None:
        with self._lock:
            return self._last


class SafetyGate:
    def __init__(self, inspector: WindowInspector, ledger: InjectionLedger,
                 tick=w.tick_count, last_input=w.last_input_tick, key_down=w.key_is_down,
                 clock=time.monotonic):
        self.inspector = inspector
        self.ledger = ledger
        self._tick = tick
        self._last_input = last_input
        self._key_down = key_down
        self._clock = clock
        self._lock = threading.Lock()
        self._last_user_activity: float | None = None
        self._seen_input_tick: int | None = None

    # -- user activity -----------------------------------------------------------
    def observe(self) -> None:
        """Sample GetLastInputInfo; call frequently (every scheduler tick).

        Any input whose timestamp is not within a few ms of our own last
        injection is attributed to the user (or to other software) and
        remembered as user activity.
        """
        last = self._last_input()
        with self._lock:
            if last == self._seen_input_tick:
                return
            self._seen_input_tick = last
        own = self.ledger.last
        if own is not None and 0 <= w.tick_delta(last, own) <= _OWN_INPUT_TOLERANCE_MS:
            return
        if own is not None and -50 <= w.tick_delta(last, own) < 0:
            return  # processed a hair before our record() call
        age_ms = max(0, w.tick_delta(self._tick(), last))
        with self._lock:
            self._last_user_activity = self._clock() - age_ms / 1000.0

    def seconds_since_user_activity(self) -> float | None:
        with self._lock:
            if self._last_user_activity is None:
                return None
            return max(0.0, self._clock() - self._last_user_activity)

    def user_active_within(self, seconds: float) -> bool:
        since = self.seconds_since_user_activity()
        return since is not None and since < seconds

    def mark_user_activity(self) -> None:
        with self._lock:
            self._last_user_activity = self._clock()

    # -- physical state -------------------------------------------------------------
    def held_keys(self) -> list[str]:
        return sorted({name for vk, name in _HELD_KEYS if self._key_down(vk)})

    def held_buttons(self) -> list[str]:
        return [name for vk, name in _HELD_BUTTONS if self._key_down(vk)]

    @staticmethod
    def session_verdict() -> Verdict:
        """Fail closed when the local input desktop is not the normal desktop."""
        running = wintypes.BOOL(False)
        if w.SystemParametersInfoW(w.SPI_GETSCREENSAVERRUNNING, 0, ctypes.byref(running), 0) and running.value:
            return Verdict(False, "screensaver", "Screen saver is running")
        desk = w.OpenInputDesktop(0, False, w.DESKTOP_READOBJECTS)
        if not desk:
            return Verdict(False, "locked", "Workstation is locked or a secure desktop is active")
        try:
            buf = ctypes.create_unicode_buffer(64)
            needed = wintypes.DWORD(0)
            if not w.GetUserObjectInformationW(desk, w.UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
                return Verdict(False, "locked", "Cannot read the active input desktop")
            if buf.value.lower() != "default":
                return Verdict(False, "locked", "Workstation is locked or a secure desktop is active")
        finally:
            w.CloseDesktop(desk)
        return ALLOWED

    # -- composite checks -------------------------------------------------------------
    def check_target(self, target: RdpWindow | None) -> Verdict:
        st = self.inspector.check(target, require_foreground=True)
        if not st.ok:
            return Verdict(False, st.code, st.message)
        return self.session_verdict()

    def check_keyboard(self, target: RdpWindow | None, idle_guard_s: float) -> Verdict:
        v = self.check_target(target)
        if not v.ok:
            return v
        held = self.held_keys() + self.held_buttons()
        if held:
            return Verdict(False, "held", f"{', '.join(held)} is held down")
        self.observe()
        if self.user_active_within(idle_guard_s):
            return Verdict(False, "user_active", f"You used the keyboard or mouse in the last {idle_guard_s:.0f} s")
        return ALLOWED

    def check_pointer(self, target: RdpWindow | None, idle_guard_s: float) -> Verdict:
        v = self.check_target(target)
        if not v.ok:
            return v
        held = self.held_buttons() + self.held_keys()
        if held:
            return Verdict(False, "held", f"{', '.join(held)} is held down")
        self.observe()
        if self.user_active_within(idle_guard_s):
            return Verdict(False, "user_active", f"You used the keyboard or mouse in the last {idle_guard_s:.0f} s")
        return ALLOWED

    def still_foreground(self, target: RdpWindow) -> bool:
        return self.inspector.is_foreground(target)
