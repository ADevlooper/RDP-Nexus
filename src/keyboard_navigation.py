"""Keyboard navigation: a curated allow-list of shortcuts, the app shuffler, and
the code that sends one chord through the safety gate.

Only the shortcuts defined here can ever be sent.  None of them is a local
Windows shell shortcut (no Win key, no Alt+Tab, no Ctrl+Alt+Del, no Alt+F4),
so even a mis-routed chord cannot switch, close or lock local applications.

Keys are sent as hardware scan codes (KEYEVENTF_SCANCODE), which is what the
Remote Desktop client forwards to the remote session, one event at a time with
human-like timing (see Pacing).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import winapi as w
from input_safety import INJECTION_SIGNATURE, InjectionLedger, SafetyGate
from window_detection import RdpWindow


@dataclass(frozen=True)
class Key:
    name: str
    scan: int
    extended: bool = False


KEYS = {
    "ctrl": Key("Ctrl", 0x1D),
    "shift": Key("Shift", 0x2A),
    "alt": Key("Alt", 0x38),
    "tab": Key("Tab", 0x0F),
    "pageup": Key("Page Up", 0x49, True),
    "pagedown": Key("Page Down", 0x51, True),
    "up": Key("Up", 0x48, True),
    "down": Key("Down", 0x50, True),
}
MODIFIERS = ("ctrl", "shift", "alt")


@dataclass(frozen=True)
class Shortcut:
    id: str
    label: str
    keys: tuple[str, ...]  # modifiers first, main key last; empty = do nothing


BROWSER = "browser"
REMOTE_APP = "remote_app"
MODES = {BROWSER: "Browser tabs", REMOTE_APP: "Remote apps"}

SHUFFLE_APPS = "shuffle_apps"
CYCLE_APPS = "cycle_apps"

SWITCH_SHORTCUTS: dict[str, Shortcut] = {
    s.id: s
    for s in (
        Shortcut("ctrl_tab", "Ctrl+Tab · next tab", ("ctrl", "tab")),
        Shortcut("ctrl_shift_tab", "Ctrl+Shift+Tab · previous tab", ("ctrl", "shift", "tab")),
        Shortcut("ctrl_pagedown", "Ctrl+Page Down · next tab", ("ctrl", "pagedown")),
        Shortcut("ctrl_pageup", "Ctrl+Page Up · previous tab", ("ctrl", "pageup")),
        # Alt+Page Up / Down are the Remote Desktop client's own equivalents of
        # Alt+Tab / Alt+Shift+Tab *inside* the remote session.
        # Remote-app switching: the keys below are the windowed-mode defaults.  The engine
        # picks Alt+Tab / Alt+Shift+Tab instead when the RDP client forwards Windows key
        # combinations to the remote session (see rdp_settings.py).
        # Cycle: one Alt+Shift+Tab brings forward the app used longest ago, so repeating it visits
        # every open app in turn (verified live: 4 switches showed 4 different apps).
        Shortcut(CYCLE_APPS, "Cycle through all apps · in order", ("alt", "pagedown")),
        Shortcut(SHUFFLE_APPS, "Shuffle apps · random order, no repeats", ("alt", "pageup")),
        Shortcut("alt_pageup", "Back and forth · last two apps", ("alt", "pageup")),
        Shortcut("alt_pagedown", "Cycle through all apps · in order (legacy)", ("alt", "pagedown")),
        Shortcut("off", "Off · no switching", ()),
    )
}

REMOTE_APP_SWITCHES = frozenset({CYCLE_APPS, SHUFFLE_APPS, "alt_pageup", "alt_pagedown"})


def remote_app_keys(switch_id: str, windows_keys_go_remote: bool) -> tuple[str, ...]:
    """Keys that switch apps INSIDE the remote session for the RDP client's current keyboard mode."""
    previous = switch_id in (CYCLE_APPS, "alt_pagedown")
    if windows_keys_go_remote:  # client forwards Alt+Tab itself (e.g. full screen, default settings)
        return ("alt", "shift", "tab") if previous else ("alt", "tab")
    return ("alt", "pagedown") if previous else ("alt", "pageup")  # client's own Alt+Tab stand-in


MODE_SWITCH_OPTIONS = {
    BROWSER: ("ctrl_tab", "ctrl_shift_tab", "ctrl_pagedown", "ctrl_pageup", "off"),
    REMOTE_APP: (CYCLE_APPS, SHUFFLE_APPS, "alt_pageup", "off"),
}
MODE_DEFAULT_SWITCH = {BROWSER: "ctrl_tab", REMOTE_APP: CYCLE_APPS}

SCROLL_KEYS = {  # id -> (label, key for "down", key for "up")
    "page": ("Page Up / Page Down", "pagedown", "pageup"),
    "arrow": ("Arrow Up / Down", "down", "up"),
    # Mouse wheel is sent by the pointer controller (only while the pointer is over the RDP window).
    "wheel": ("Mouse wheel · scrolls what's under the pointer", None, None),
    "off": ("Off · no scrolling", None, None),
}
SCROLL_DIRECTIONS = {"down": "Down", "up": "Up", "alternate": "Alternate"}

MIN_APPS, MAX_APPS = 2, 20


# --- pure helpers ---------------------------------------------------------------
@dataclass(frozen=True)
class KeyEvent:
    key: str
    down: bool


def build_chord(keys: tuple[str, ...], repeat: int = 1) -> list[KeyEvent]:
    """Press modifiers, tap the main key `repeat` times, release modifiers in reverse."""
    if not keys:
        return []
    for k in keys:
        if k not in KEYS:
            raise ValueError(f"Key {k!r} is not in the allow-list")
    if repeat < 1:
        raise ValueError("repeat must be >= 1")
    *mods, main = keys
    if main in MODIFIERS or any(m not in MODIFIERS for m in mods):
        raise ValueError("Chord must be modifiers followed by one non-modifier key")
    events = [KeyEvent(m, True) for m in mods]
    for _ in range(repeat):
        events += [KeyEvent(main, True), KeyEvent(main, False)]
    events += [KeyEvent(m, False) for m in reversed(mods)]
    return events


def chord_text(keys: tuple[str, ...], repeat: int = 1) -> str:
    if not keys:
        return "nothing"
    *mods, main = keys
    head = "+".join(KEYS[k].name for k in mods)
    tail = KEYS[main].name if repeat == 1 else f"{KEYS[main].name}×{repeat}"
    return f"{head}+{tail}" if head else tail


def to_inputs(events: list[KeyEvent]) -> list[w.INPUT]:
    inputs = []
    for ev in events:
        key = KEYS[ev.key]
        flags = w.KEYEVENTF_SCANCODE
        if key.extended:
            flags |= w.KEYEVENTF_EXTENDEDKEY
        if not ev.down:
            flags |= w.KEYEVENTF_KEYUP
        inp = w.INPUT(type=w.INPUT_KEYBOARD)
        inp.ki = w.KEYBDINPUT(wVk=0, wScan=key.scan, dwFlags=flags, time=0, dwExtraInfo=INJECTION_SIGNATURE)
        inputs.append(inp)
    return inputs


def scroll_key_for(scroll_key: str, direction_down: bool) -> str | None:
    _label, down, up = SCROLL_KEYS.get(scroll_key, SCROLL_KEYS["off"])
    return down if direction_down else up


class AppShuffler:
    """Plans app switches so every open remote app is visited in a random order.

    Models the remote Alt+Tab list, which Windows keeps in most-recently-used
    order: position 0 is the active app.  Activating the app at position k
    means holding Alt and pressing Page Up k times; k is always >= 1, so the
    active app is never selected again.  Each round visits every app once in a
    fresh shuffled order that never starts with the app that is already active
    (so no app repeats back-to-back across rounds either), and with three or
    more apps a round never reuses the previous round's order.

    The model cannot see the remote screen: it assumes the configured count is
    right and that nobody switched apps manually.  If that drifts, every switch
    still lands on a *different* app as long as the count is not larger than the
    number of apps actually open.
    """

    def __init__(self, count: int, rng: random.Random | None = None):
        if not MIN_APPS <= count <= MAX_APPS:
            raise ValueError(f"App count must be {MIN_APPS}–{MAX_APPS}")
        self.count = count
        self._rng = rng or random.Random()
        self.mru = list(range(count))  # abstract app ids; mru[0] is the active app
        self._plan: list[int] = []
        self._last_round: list[int] | None = None

    def _new_round(self) -> None:
        apps = list(range(self.count))
        for _ in range(200):
            self._rng.shuffle(apps)
            if apps[0] == self.mru[0]:
                continue
            if self.count >= 3 and apps == self._last_round:
                continue
            break
        else:  # pragma: no cover - only reachable with a broken RNG
            apps = self.mru[1:] + self.mru[:1]
        self._plan = list(apps)
        self._last_round = list(apps)

    def peek(self) -> tuple[int, int]:
        """(app id, Page Up presses needed) for the next switch. Does not advance."""
        if not self._plan:
            self._new_round()
        if self._plan[0] == self.mru[0]:
            # Only possible if commit() was skipped mid-round; drop the duplicate.
            self._plan.pop(0)
            return self.peek()
        target = self._plan[0]
        return target, self.mru.index(target)

    def commit(self) -> int:
        """Record that the peeked switch was delivered; returns the app id now active."""
        target, steps = self.peek()
        self._plan.pop(0)
        self.mru.insert(0, self.mru.pop(steps))
        return target


# --- sending ---------------------------------------------------------------------
@dataclass(frozen=True)
class Pacing:
    """Human-like key timing.  The Remote Desktop client only turns Alt+Page Up into
    a remote Alt+Tab when it sees Alt still held while it processes Page Up, and
    the remote task switcher needs time to advance between taps.  A zero-delay
    burst is processed after Alt has already been released, so it is not used."""

    lead_ms: int   # after pressing the modifier(s), before the first key
    hold_ms: int   # how long each key is held down
    gap_ms: int    # pause between repeated taps of the key
    tail_ms: int   # after the last tap, before releasing the modifier(s)


PACING = {
    "normal": Pacing(lead_ms=80, hold_ms=50, gap_ms=150, tail_ms=150),
    "slow": Pacing(lead_ms=150, hold_ms=90, gap_ms=300, tail_ms=300),
}
KEY_TIMINGS = {"normal": "Normal", "slow": "Slow"}
_MODIFIER_STEP_MS = 20
_GATE_NAMES = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt"}


@dataclass(frozen=True)
class Step:
    event: KeyEvent
    delay_ms: int  # wait after sending this event


def paced_steps(keys: tuple[str, ...], repeat: int, pacing: Pacing) -> list[Step]:
    events = build_chord(keys, repeat)
    if not events:
        return []
    main = keys[-1]
    steps = []
    for i, ev in enumerate(events):
        nxt = events[i + 1] if i + 1 < len(events) else None
        if nxt is None:
            delay = 0
        elif ev.key != main:  # a modifier going down or up
            delay = pacing.lead_ms if (ev.down and nxt.key == main) else _MODIFIER_STEP_MS
        elif ev.down:
            delay = pacing.hold_ms
        elif nxt.key == main:
            delay = pacing.gap_ms
        else:
            delay = pacing.tail_ms
        steps.append(Step(ev, delay))
    return steps


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    code: str
    message: str


class KeyboardNavigator:
    """Sends one chord as a paced sequence of single key events.

    Before EVERY event the RDP window must still be the foreground window, no
    new user input may have arrived and no other key/button may be held.  If
    anything changes mid-sequence, every key RDP Nexus is holding is released
    at once (a stuck Alt would be worse than a stray key-up) and the action is
    reported as interrupted.
    """

    def __init__(self, gate: SafetyGate, ledger: InjectionLedger, send=w.send_inputs, sleep=None,
                 clock=time.monotonic):
        self.gate = gate
        self.ledger = ledger
        self._send = send
        self._sleep = sleep
        self._clock = clock

    def _wait(self, seconds: float, cancel) -> None:
        if seconds <= 0:
            return
        if self._sleep is not None:
            self._sleep(seconds)
        elif cancel is not None:
            cancel.wait(seconds)
        else:
            time.sleep(seconds)

    def _interruption(self, target: RdpWindow, held: list[str], started: float, cancel) -> tuple[str, str] | None:
        if cancel is not None and cancel.is_set():
            return "cancelled", "automation was paused or stopped"
        if not self.gate.still_foreground(target):
            return "focus_changed", "the RDP window lost focus"
        self.gate.observe()
        since = self.gate.seconds_since_user_activity()
        if since is not None and since < self._clock() - started:
            return "user_active", "you used the keyboard or mouse"
        ours = {_GATE_NAMES[k] for k in held if k in _GATE_NAMES}
        foreign = [k for k in self.gate.held_keys() if k not in ours] + self.gate.held_buttons()
        if foreign:
            return "held", f"{', '.join(foreign)} was pressed"
        return None

    def _release(self, held: list[str]) -> None:
        if held:
            self._send(to_inputs([KeyEvent(k, False) for k in reversed(held)]))
            self.ledger.record()
            held.clear()

    def send_chord(self, target: RdpWindow, keys: tuple[str, ...], idle_guard_s: float, repeat: int = 1,
                   pacing: Pacing = PACING["normal"], cancel=None) -> ActionResult:
        text = chord_text(keys, repeat)
        verdict = self.gate.check_keyboard(target, idle_guard_s)
        if not verdict.ok:
            return ActionResult(False, verdict.code, f"Skipped {text}: {verdict.message}")
        steps = paced_steps(keys, repeat, pacing)
        held: list[str] = []
        started = self._clock()
        try:
            for step in steps:
                problem = self._interruption(target, held, started, cancel)
                if problem:
                    self._release(held)
                    code, why = problem
                    return ActionResult(False, code, f"{text} interrupted: {why}; keys released")
                if self._send(to_inputs([step.event])) != 1:
                    self._release(held)
                    return ActionResult(False, "send_failed",
                                        f"{text} was blocked by Windows; is the RDP client running as administrator?")
                self.ledger.record()
                if step.event.down:
                    held.append(step.event.key)
                elif step.event.key in held:
                    held.remove(step.event.key)
                self._wait(step.delay_ms / 1000.0, cancel)
        finally:
            self._release(held)  # never leave a key pressed, whatever happened
        if not self.gate.still_foreground(target):
            return ActionResult(False, "focus_changed",
                                f"Focus left the RDP window right after {text}; automation suspended")
        return ActionResult(True, "ok", f"Sent {text} over {self._clock() - started:.2f} s")
