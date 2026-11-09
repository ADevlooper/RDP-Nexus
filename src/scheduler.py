"""Automation engine: one worker thread, explicit states, everything cancellable.

The engine owns all timing.  The UI only calls the public command methods
(thread-safe) and polls `snapshot()`; it never runs automation itself, so the
GUI thread is never blocked.

Rules enforced here:
* Exactly one worker thread exists; Start while running is a no-op.
* Countdowns only run while the RDP target is valid and in the foreground.
  When it becomes unavailable they are cancelled; when it returns they restart
  from the full interval (or wait for the user, per the resume policy).
  Missed actions are never sent late.
* Pause/Stop cancel in-flight pointer glides and pending waits immediately.
* Manual pointer movement moves the pointer feature to MANUAL; only an explicit
  user command (Resume pointer / Resume / Start) re-arms it.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from enum import Enum

from input_safety import SafetyGate
from keyboard_navigation import (
    CYCLE_APPS, PACING, REMOTE_APP_SWITCHES, SHUFFLE_APPS, SWITCH_SHORTCUTS, ActionResult, AppShuffler,
    KeyboardNavigator,
    chord_text, remote_app_keys, scroll_key_for,
)
from pointer_controller import ManualMovementMonitor, PointerController
from rdp_settings import HOOK_FULLSCREEN, HOOK_REMOTE, RdpKeyboardSettings, windows_keys_go_remote
from screen_watch import SwitchWatcher
from settings import Settings
from window_detection import CLOSED, REPLACED, RdpWindow, TargetStatus, WindowInspector

log = logging.getLogger(__name__)


class RunState(Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


class DisplayState(str, Enum):
    STOPPED = "Stopped"
    RUNNING = "Running"
    PAUSED = "Paused"
    UNAVAILABLE = "RDP Unavailable"


class PointerState(str, Enum):
    OFF = "Off"
    IDLE = "Idle"
    ARMING = "Arming"
    ACTIVE = "Active"
    SUSPENDED = "Suspended"
    MANUAL = "Manual control"
    FAULT = "Disabled (safety)"


@dataclass(frozen=True)
class EngineConfig:
    keyboard_enabled: bool = True
    interval_s: float = 13 * 60
    switch_shortcut: str = CYCLE_APPS
    app_count: int = 4
    scroll_key: str = "page"
    scroll_direction: str = "alternate"
    pointer_enabled: bool = True
    pointer_interval_s: float = 60
    pointer_range_px: int = 20
    resume_policy: str = "auto"
    key_timing: str = "normal"      # "normal" | "slow" (see keyboard_navigation.PACING)
    idle_guard_s: float = 10.0      # no automated input within this long after real user input
    pointer_rest_s: float = 3.0     # mouse must rest this long over the RDP window before arming
    action_gap_s: float = 0.5       # pause between the switch and the scroll action
    long_gap_s: float = 5.0         # a worker stall longer than this restarts countdowns

    @classmethod
    def from_settings(cls, s: Settings, **overrides) -> "EngineConfig":
        cfg = cls(
            keyboard_enabled=s.keyboard_enabled,
            interval_s=s.interval_minutes * 60.0,
            switch_shortcut=s.switch_shortcut,
            app_count=s.app_count,
            scroll_key=s.scroll_key,
            scroll_direction=s.scroll_direction,
            pointer_enabled=s.pointer_enabled,
            pointer_interval_s=float(s.pointer_interval_seconds),
            pointer_range_px=s.pointer_range_px,
            resume_policy=s.resume_policy,
            key_timing=s.key_timing,
        )
        return replace(cfg, **overrides) if overrides else cfg


@dataclass(frozen=True)
class LogEntry:
    when: float
    level: str
    message: str


@dataclass(frozen=True)
class Snapshot:
    state: DisplayState
    detail: str
    countdown_s: float | None
    keyboard_enabled: bool
    pointer_enabled: bool
    pointer_state: PointerState
    pointer_detail: str
    pointer_countdown_s: float | None
    target: RdpWindow | None
    target_status: TargetStatus | None
    cycles: int
    events: tuple[LogEntry, ...]
    switch_note: str = ""


_TERMINAL_TARGET_CODES = {CLOSED, REPLACED}


class AutomationEngine:
    def __init__(self, inspector: WindowInspector, gate: SafetyGate, keyboard: KeyboardNavigator,
                 pointer: PointerController, monitor: ManualMovementMonitor,
                 config: EngineConfig | None = None, clock=time.monotonic, cursor=None,
                 tick_s: float = 0.1, start_thread: bool = True, rdp_keys: RdpKeyboardSettings | None = None,
                 screen_watcher: SwitchWatcher | None = None):
        self.inspector = inspector
        self.screen = screen_watcher or SwitchWatcher(inspector)
        self.rdp_keys = rdp_keys or RdpKeyboardSettings()
        # RDP windows where Alt+Tab was seen reaching the local computer: never use Alt+Tab there again.
        self._alt_tab_unsafe: set[int] = set()
        self._switch_note = ""
        self.gate = gate
        self.keyboard = keyboard
        self.pointer = pointer
        self.monitor = monitor
        self._clock = clock
        self._cursor = cursor or pointer._cursor
        self._tick_s = tick_s

        self._lock = threading.RLock()
        self._cfg = config or EngineConfig()
        self._target: RdpWindow | None = None
        self._run = RunState.STOPPED
        self._detail = "Select your RDP window, then press Start"
        self._unavailable = False
        self._needs_fresh = False
        self._kb_due: float | None = None
        self._ptr_state = PointerState.OFF if not self._cfg.pointer_enabled else PointerState.IDLE
        self._ptr_detail = ""
        self._ptr_due: float | None = None
        self._ptr_expected: tuple[int, int] | None = None
        self._target_status: TargetStatus | None = None
        self._scroll_down_next = True
        self._shuffler: AppShuffler | None = None
        self._cycles = 0
        self._events: deque[LogEntry] = deque(maxlen=200)
        self._cancel = threading.Event()
        self._cancel.set()
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._last_step: float | None = None
        self._hook_failed = False
        self._test_until: float | None = None
        self._test_running = False
        self._test_cancel = threading.Event()

        self._thread: threading.Thread | None = None
        if start_thread:
            self._thread = threading.Thread(target=self._worker, name="rdpnexus-engine", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ public API
    @property
    def config(self) -> EngineConfig:
        with self._lock:
            return self._cfg

    @property
    def target(self) -> RdpWindow | None:
        with self._lock:
            return self._target

    def set_target(self, target: RdpWindow | None) -> bool:
        with self._lock:
            if self._run == RunState.RUNNING:
                self._log("warning", "Pause or stop automation before changing the RDP window")
                return False
            self._target = target
            self._shuffler = None
            self.pointer.reset_anchor()
            if target is not None:
                self._log("info", f"Target selected: {target.label()} ({target.kind})")
                if self._run == RunState.STOPPED:
                    self._detail = "Ready — press Start, then click into the RDP window"
            self._wake.set()
            return True

    def set_config(self, cfg: EngineConfig) -> None:
        with self._lock:
            old, self._cfg = self._cfg, cfg
            now = self._clock()
            running_ok = self._run == RunState.RUNNING and not self._unavailable and not self._needs_fresh
            if (cfg.app_count, cfg.switch_shortcut) != (old.app_count, old.switch_shortcut):
                self._shuffler = None
            if cfg.keyboard_enabled != old.keyboard_enabled or cfg.interval_s != old.interval_s:
                self._kb_due = now + cfg.interval_s if (running_ok and cfg.keyboard_enabled) else None
                if cfg.keyboard_enabled != old.keyboard_enabled:
                    self._log("info", f"Keyboard navigation {'enabled' if cfg.keyboard_enabled else 'disabled'}")
            if cfg.pointer_enabled != old.pointer_enabled:
                if cfg.pointer_enabled:
                    self._ptr_state = PointerState.ARMING if self._run == RunState.RUNNING else PointerState.IDLE
                    self._hook_failed = False
                else:
                    self._set_pointer(PointerState.OFF, "")
                self._log("info", f"Pointer movement {'enabled' if cfg.pointer_enabled else 'disabled'}")
            elif cfg.pointer_interval_s != old.pointer_interval_s and self._ptr_due is not None:
                self._ptr_due = now + cfg.pointer_interval_s
        self._wake.set()

    def start(self) -> bool:
        with self._lock:
            if self._run == RunState.RUNNING:
                self._log("info", "Already running")
                return True
            if self._target is None:
                self._detail = "Select your RDP window first"
                self._log("warning", self._detail)
                return False
            st = self.inspector.check(self._target, require_foreground=False)
            if not st.ok:
                self._detail = f"Cannot start: {st.message}"
                self._log("warning", self._detail)
                return False
            self._begin_running("Automation started")
            self._shuffler = None
            self._scroll_down_next = True
        self._wake.set()
        return True

    def pause(self) -> None:
        with self._lock:
            self._cancel_test()
            if self._run != RunState.RUNNING:
                return
            self._cancel.set()
            self._run = RunState.PAUSED
            self._kb_due = None
            self._unavailable = False
            if self._ptr_state not in (PointerState.OFF,):
                self._set_pointer(PointerState.IDLE, "")
            self._detail = "Paused — no automated input until you press Resume"
            self._log("info", "Paused")
        self._wake.set()

    def resume(self) -> None:
        with self._lock:
            if self._run != RunState.PAUSED:
                return
            self._begin_running("Resumed")
        self._wake.set()

    def stop(self) -> None:
        with self._lock:
            self._cancel.set()
            self._cancel_test()
            if self._run != RunState.STOPPED:
                self._log("info", "Stopped")
            self._run = RunState.STOPPED
            self._kb_due = None
            self._unavailable = False
            self._needs_fresh = False
            self._set_pointer(PointerState.OFF if not self._cfg.pointer_enabled else PointerState.IDLE, "")
            self._detail = "Stopped"
        self._wake.set()

    def resume_pointer(self) -> None:
        with self._lock:
            if not self._cfg.pointer_enabled:
                return
            if self._ptr_state in (PointerState.MANUAL, PointerState.FAULT, PointerState.IDLE):
                self._hook_failed = False
                if self._run == RunState.RUNNING:
                    self._set_pointer(PointerState.ARMING, "Rest the mouse over the RDP window to arm")
                    self._log("info", "Pointer movement resumed by you; arming")
                else:
                    self._set_pointer(PointerState.IDLE, "")
        self._wake.set()

    TEST_TIMEOUT_S = 15.0
    TEST_REST_S = 1.5

    def test_now(self) -> bool:
        """Run one switch + scroll as soon as the RDP window is active and the user
        keeps still.  Works in any state and does not start automation."""
        with self._lock:
            if self._target is None:
                self._detail = "Select your RDP window first"
                self._log("warning", self._detail)
                return False
            self._test_cancel = threading.Event()
            self._test_until = self._clock() + self.TEST_TIMEOUT_S
            self._detail = "Test: click inside the RDP window, then keep still…"
            self._log("info", "Test requested: click inside the RDP window and keep still")
        self._wake.set()
        return True

    @property
    def testing(self) -> bool:
        """True from Test now until its keys have been sent (or it was cancelled)."""
        return self._test_until is not None or self._test_running

    def snapshot(self) -> Snapshot:
        with self._lock:
            now = self._clock()
            if self._run == RunState.STOPPED:
                state = DisplayState.STOPPED
            elif self._run == RunState.PAUSED:
                state = DisplayState.PAUSED
            elif self._unavailable:
                state = DisplayState.UNAVAILABLE
            else:
                state = DisplayState.RUNNING
            return Snapshot(
                state=state,
                detail=self._detail,
                countdown_s=max(0.0, self._kb_due - now) if self._kb_due is not None else None,
                keyboard_enabled=self._cfg.keyboard_enabled,
                pointer_enabled=self._cfg.pointer_enabled,
                pointer_state=self._ptr_state,
                pointer_detail=self._ptr_detail,
                pointer_countdown_s=(max(0.0, self._ptr_due - now)
                                     if self._ptr_due is not None and self._ptr_state == PointerState.ACTIVE
                                     else None),
                target=self._target,
                target_status=self._target_status,
                cycles=self._cycles,
                events=tuple(list(self._events)[-40:]),
                switch_note=self._switch_note,
            )

    def shutdown(self) -> None:
        self.stop()
        self._shutdown.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(3.0)
        self.monitor.stop()

    # ------------------------------------------------------------------ internals
    def _log(self, level: str, message: str) -> None:
        self._events.append(LogEntry(time.time(), level, message))
        getattr(log, level if level in ("info", "warning", "error") else "info")(message)

    def _cancel_test(self) -> None:
        self._test_cancel.set()
        if self._test_until is not None:
            self._test_until = None
            self._log("info", "Test cancelled")

    def _begin_running(self, message: str) -> None:
        self._cancel = threading.Event()
        self._run = RunState.RUNNING
        self._unavailable = False
        self._needs_fresh = True
        self._kb_due = None
        self._hook_failed = False
        if self._cfg.pointer_enabled:
            self._set_pointer(PointerState.ARMING, "Rest the mouse over the RDP window to arm")
        self._detail = "Waiting for the RDP window to become active…"
        self._log("info", message)

    def _set_pointer(self, state: PointerState, detail: str) -> None:
        if state in (PointerState.ACTIVE, PointerState.SUSPENDED) and self._ptr_state in (
                PointerState.ACTIVE, PointerState.SUSPENDED):
            pass  # keep the hook armed while merely suspended
        elif state != PointerState.ACTIVE:
            self.monitor.disarm()
        if state not in (PointerState.ACTIVE, PointerState.SUSPENDED):
            self._ptr_expected = None
        if state != PointerState.ACTIVE:
            self._ptr_due = None
        self._ptr_state = state
        self._ptr_detail = detail

    def _worker(self) -> None:
        while not self._shutdown.is_set():
            try:
                self.step()
            except Exception:  # fail closed: any unexpected error stops automation
                log.exception("Engine error")
                with self._lock:
                    self._cancel.set()
                    self._run = RunState.STOPPED
                    self._kb_due = None
                    self._set_pointer(PointerState.IDLE if self._cfg.pointer_enabled else PointerState.OFF, "")
                    self._detail = "Stopped after an internal error (details in the log file)"
                    self._log("error", self._detail)
            self._wake.wait(self._tick_s)
            self._wake.clear()

    def step(self) -> None:
        """One scheduler tick.  Called by the worker thread (or directly by tests)."""
        now = self._clock()
        gap = 0.0 if self._last_step is None else now - self._last_step
        self._last_step = now
        self.gate.observe()

        with self._lock:
            run, cfg, target = self._run, self._cfg, self._target
        st = self.inspector.check(target, require_foreground=True) if target is not None else None
        note = ""
        if target is not None and cfg.switch_shortcut in REMOTE_APP_SWITCHES and st is not None \
                and st.code not in _TERMINAL_TARGET_CODES:
            keys, why = self._remote_switch(target, cfg.switch_shortcut)
            note = f"App switching: {chord_text(keys)} ({why})"
        with self._lock:
            self._target_status = st
            self._switch_note = note
        self._manage_hook(run, cfg)
        if self._test_until is not None:
            self._run_test(target, cfg, st)
        if run != RunState.RUNNING:
            return

        if gap > cfg.long_gap_s:
            with self._lock:
                self._needs_fresh = True
                self._log("info", f"Detected a {gap:.0f} s pause in timing (sleep or heavy load); "
                                  "countdowns restarted, no missed actions sent")

        if st is not None and st.code in _TERMINAL_TARGET_CODES:
            with self._lock:
                self._cancel.set()
                self._run = RunState.STOPPED
                self._kb_due = None
                self._set_pointer(PointerState.IDLE if cfg.pointer_enabled else PointerState.OFF, "")
                self._detail = f"{st.message} — automation stopped. Select the RDP window again."
                self._log("warning", self._detail)
            return

        if st is None or not st.ok:
            reason = st.message if st is not None else "No RDP window selected"
        else:
            session = self.gate.session_verdict()
            reason = None if session.ok else session.message
        if reason is not None:
            self._enter_unavailable(reason)
            self._detect_manual()
            return
        if self._unavailable and not self._leave_unavailable(cfg):
            return
        if self._needs_fresh:
            self._fresh_start(cfg)

        self._detect_manual()
        if cfg.keyboard_enabled and self._kb_due is not None and self._clock() >= self._kb_due:
            self._keyboard_cycle(target, cfg)
        self._pointer_tick(target, cfg)

    # -- availability --------------------------------------------------------------
    def _enter_unavailable(self, reason: str) -> None:
        with self._lock:
            if not self._unavailable:
                self._unavailable = True
                self._kb_due = None
                if self._ptr_state == PointerState.ACTIVE:
                    self._set_pointer(PointerState.SUSPENDED, reason)
                elif self._ptr_state == PointerState.ARMING:
                    self._set_pointer(PointerState.SUSPENDED, reason)
                self._log("warning", f"Automation suspended: {reason}")
            self._detail = reason

    def _leave_unavailable(self, cfg: EngineConfig) -> bool:
        with self._lock:
            self._unavailable = False
            if cfg.resume_policy == "manual":
                self._cancel.set()
                self._run = RunState.PAUSED
                if self._ptr_state != PointerState.OFF:
                    self._set_pointer(PointerState.IDLE, "")
                self._detail = "RDP window is available again — press Resume to continue"
                self._log("info", self._detail)
                return False
            self._needs_fresh = True
            self._log("info", "RDP window is active again — countdown restarted (missed actions are not sent)")
            return True

    def _fresh_start(self, cfg: EngineConfig) -> None:
        with self._lock:
            now = self._clock()
            self._needs_fresh = False
            self._kb_due = now + cfg.interval_s if cfg.keyboard_enabled else None
            if cfg.pointer_enabled and self._ptr_state in (PointerState.SUSPENDED, PointerState.IDLE,
                                                           PointerState.ARMING, PointerState.ACTIVE):
                self._set_pointer(PointerState.ARMING, "Rest the mouse over the RDP window to arm")
            self._detail = "Running"

    # -- hook / manual detection ---------------------------------------------------------
    def _manage_hook(self, run: RunState, cfg: EngineConfig) -> None:
        wanted = run == RunState.RUNNING and cfg.pointer_enabled and not self._hook_failed
        if wanted and not self.monitor.running:
            if not self.monitor.start():
                with self._lock:
                    self._hook_failed = True
                    msg = f"Manual-movement detection unavailable ({self.monitor.install_error or 'hook failed'}); " \
                          "pointer movement disabled for safety"
                    self._set_pointer(PointerState.FAULT, msg)
                    self._log("error", msg)
        elif not wanted and self.monitor.running:
            self.monitor.stop()

    def _detect_manual(self) -> None:
        with self._lock:
            if self._ptr_state not in (PointerState.ACTIVE, PointerState.SUSPENDED) or not self.monitor.armed:
                return
            reason = self.monitor.reason if self.monitor.triggered else ""
            if not reason and self._ptr_expected is not None:
                try:
                    pos = self._cursor()
                except OSError:
                    pos = None
                if pos is not None and math.dist(pos, self._ptr_expected) > 2:
                    reason = "pointer position changed"
            if reason:
                self._go_manual(reason)

    def _go_manual(self, reason: str) -> None:
        self.gate.mark_user_activity()
        self._set_pointer(PointerState.MANUAL,
                          f"You took control ({reason}) — pointer movement paused until you resume it")
        self._log("info", f"Manual pointer control detected ({reason}); pointer movement paused")

    # -- keyboard -------------------------------------------------------------------------
    def _shuffler_for(self, cfg: EngineConfig) -> AppShuffler:
        if self._shuffler is None or self._shuffler.count != cfg.app_count:
            self._shuffler = AppShuffler(cfg.app_count)
        return self._shuffler

    def _run_test(self, target: RdpWindow | None, cfg: EngineConfig, st: TargetStatus | None) -> None:
        with self._lock:
            if self._test_until is None:
                return
            if self._clock() > self._test_until:
                self._test_until = None
                self._detail = "Test cancelled: the RDP window was not active within 15 s"
                self._log("warning", self._detail)
                return
        if st is None or not st.ok:
            with self._lock:
                self._detail = f"Test waiting: {st.message if st else 'no RDP window selected'}"
            return
        session = self.gate.session_verdict()
        if not session.ok:
            with self._lock:
                self._detail = f"Test waiting: {session.message}"
            return
        since = self.gate.seconds_since_user_activity()
        if since is not None and since < self.TEST_REST_S:
            with self._lock:
                self._detail = "Test: keep still for a moment…"
            return
        switch = SWITCH_SHORTCUTS.get(cfg.switch_shortcut, SWITCH_SHORTCUTS["off"])
        with self._lock:
            self._test_until = None
            if not switch.keys and cfg.scroll_key == "off":
                self._detail = "Nothing to test: switching and scrolling are both off"
                self._log("warning", self._detail)
                return
            self._test_running = True
            cancel = self._test_cancel
            self._log("info", "Test running")
        try:
            sent = self._keyboard_cycle(target, replace(cfg, idle_guard_s=1.0), cancel)
        finally:
            with self._lock:
                self._test_running = False
        with self._lock:
            self._log("info" if sent else "warning", "Test finished" if sent else "Test sent nothing (see above)")

    def _remote_switch(self, target: RdpWindow, switch_id: str) -> tuple[tuple[str, ...], str]:
        """Pick the app-switch keys the RDP client will actually forward to the remote session."""
        info = self.rdp_keys.lookup(target.pid, target.process_name)
        fullscreen = self.inspector.is_fullscreen(target)
        remote = windows_keys_go_remote(info.mode, fullscreen) and target.hwnd not in self._alt_tab_unsafe
        if remote:
            why = ("full-screen RDP passes Alt+Tab to the remote PC" if info.mode == HOOK_FULLSCREEN
                   else "RDP set to apply Windows key combinations on the remote computer")
        elif target.hwnd in self._alt_tab_unsafe:
            why = "Alt+Tab reached this computer earlier, so Alt+Page Up is used"
        elif info.mode == HOOK_REMOTE or info.mode == HOOK_FULLSCREEN:
            why = "windowed RDP: Alt+Page Up is its remote Alt+Tab"
        else:
            why = "RDP keeps Windows key combinations on this computer: Alt+Page Up is its remote Alt+Tab"
        return remote_app_keys(switch_id, remote), why

    def _keyboard_cycle(self, target: RdpWindow, cfg: EngineConfig, cancel: threading.Event | None = None) -> bool:
        cancel = cancel or self._cancel
        pacing = PACING.get(cfg.key_timing, PACING["normal"])
        switch = SWITCH_SHORTCUTS.get(cfg.switch_shortcut, SWITCH_SHORTCUTS["off"])
        sent_any = False
        scrolling = cfg.scroll_key != "off"
        area = before = None
        try:
            if switch.keys:
                if cancel.is_set():
                    return sent_any
                if scrolling:  # remember the screen so we can tell when the switched-to app has appeared
                    area = self.inspector.input_area(target)
                    before = self.screen.snapshot(area)
                keys, why = switch.keys, ""
                if switch.id in REMOTE_APP_SWITCHES:
                    keys, why = self._remote_switch(target, switch.id)
                steps = 1
                shuffler = self._shuffler_for(cfg) if switch.id == SHUFFLE_APPS else None
                if shuffler is not None:
                    _app, steps = shuffler.peek()
                r = self.keyboard.send_chord(target, keys, cfg.idle_guard_s, repeat=steps, pacing=pacing,
                                             cancel=cancel)
                if r.ok and switch.id in REMOTE_APP_SWITCHES:
                    if shuffler is not None:
                        shuffler.commit()
                    took = r.message.rsplit(" over ", 1)[-1] if " over " in r.message else ""
                    order = (f"; shuffled order of {cfg.app_count}" if shuffler is not None else
                             "; cycling through all open apps" if switch.id in (CYCLE_APPS, "alt_pagedown") else "")
                    r = ActionResult(True, "ok", f"Switched remote app with {chord_text(keys, steps)}"
                                                 f"{' over ' + took if took else ''} ({why}{order})")
                elif r.code == "focus_changed" and "tab" in keys and "alt" in keys:
                    # Alt+Tab may have reached the local computer: never use it for this window again.
                    self._alt_tab_unsafe.add(target.hwnd)
                    r = ActionResult(False, r.code, r.message + " — Alt+Tab may have reached this computer, so "
                                                                "this RDP window will use Alt+Page Up from now on")
                if not self._record(r):
                    return sent_any
                sent_any = True

            down = self._scroll_down_next if cfg.scroll_direction == "alternate" else cfg.scroll_direction == "down"
            wheel = cfg.scroll_key == "wheel"
            key = scroll_key_for(cfg.scroll_key, down) if scrolling and not wheel else None
            if key or wheel:
                if sent_any:
                    # Scroll the app we switched to, not the old one: wait until it has appeared on screen.
                    seen, waited = self.screen.wait_for_switch(area, before, cancel)
                    if cancel.is_set():
                        return sent_any
                    with self._lock:
                        self._log("info", f"New app appeared after {waited:.1f} s; scrolling it now" if seen else
                                          f"No visible change after {waited:.1f} s; scrolling anyway")
                if cancel.is_set():
                    return sent_any
                if wheel:
                    r = self.pointer.wheel(target, down, cfg.idle_guard_s)
                else:
                    r = self.keyboard.send_chord(target, (key,), cfg.idle_guard_s, pacing=pacing, cancel=cancel)
                if not self._record(r):
                    return sent_any
                sent_any = True
                if cfg.scroll_direction == "alternate":
                    self._scroll_down_next = not self._scroll_down_next
        finally:
            with self._lock:
                if self._run == RunState.RUNNING and not self._unavailable and cfg.keyboard_enabled:
                    now = self._clock()
                    self._kb_due = now + cfg.interval_s
                    if self._ptr_due is not None and self._ptr_due < now + 2:
                        self._ptr_due = now + 2  # keep pointer moves clear of key presses
                if sent_any:
                    self._cycles += 1
        return sent_any

    def _record(self, r: ActionResult) -> bool:
        with self._lock:
            if r.ok:
                self._log("info", r.message)
                self._detail = r.message
                return True
            self._log("warning", r.message)
            self._detail = r.message
        if r.code == "focus_changed":
            self._enter_unavailable("Focus changed while a shortcut was being sent")
        return False

    # -- pointer --------------------------------------------------------------------------
    def _pointer_tick(self, target: RdpWindow, cfg: EngineConfig) -> None:
        if not cfg.pointer_enabled:
            return
        with self._lock:
            state = self._ptr_state
        if state == PointerState.ARMING:
            if not self.monitor.running:
                return  # _manage_hook reports failures
            ready, why = self.pointer.cursor_ready(target)
            if not ready:
                with self._lock:
                    self._ptr_detail = why
                return
            since = self.gate.seconds_since_user_activity()
            if since is not None and since < cfg.pointer_rest_s:
                with self._lock:
                    self._ptr_detail = f"Waiting for the mouse to rest ({cfg.pointer_rest_s - since:.0f} s)"
                return
            with self._lock:
                if self._ptr_state != PointerState.ARMING or self._run != RunState.RUNNING:
                    return
                self.monitor.arm()
                self._ptr_expected = self._cursor()
                self._ptr_state = PointerState.ACTIVE
                self._ptr_due = self._clock() + cfg.pointer_interval_s
                self._ptr_detail = "Armed — any manual mouse movement pauses pointer automation"
                self._log("info", "Pointer movement armed")
            return

        if state != PointerState.ACTIVE:
            return
        with self._lock:
            due = self._ptr_due
            cancel = self._cancel
        if due is None or self._clock() < due:
            return
        outcome = self.pointer.glide(target, cfg.pointer_range_px, cfg.idle_guard_s, cancel)
        r = outcome.result
        with self._lock:
            if self._ptr_state != PointerState.ACTIVE or cancel.is_set():
                return
            if outcome.final_pos is not None:
                self._ptr_expected = outcome.final_pos
            if r.code == "manual":
                self._go_manual(r.message.split(": ", 1)[-1])
                return
            if r.code in ("fault", "send_failed"):
                self._set_pointer(PointerState.FAULT, r.message)
                self._log("error", r.message)
                return
            self._ptr_due = self._clock() + cfg.pointer_interval_s
            self._ptr_detail = r.message
            self._log("info" if r.ok else "warning", r.message)
