"""Keyboard navigation: a curated allow-list of shortcuts, the app shuffler, and
the code that sends one chord through the safety gate.

Only the shortcuts defined here can ever be sent.  None of them is a local
Windows shell shortcut (no Win key, no Alt+Tab, no Ctrl+Alt+Del, no Alt+F4),
so even a mis-routed chord cannot switch, close or lock local applications.

Keys are sent as hardware scan codes (KEYEVENTF_SCANCODE), which is what the
Remote Desktop client forwards to the remote session.
"""

from __future__ import annotations

import random
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

SWITCH_SHORTCUTS: dict[str, Shortcut] = {
    s.id: s
    for s in (
        Shortcut("ctrl_tab", "Ctrl+Tab · next tab", ("ctrl", "tab")),
        Shortcut("ctrl_shift_tab", "Ctrl+Shift+Tab · previous tab", ("ctrl", "shift", "tab")),
        Shortcut("ctrl_pagedown", "Ctrl+Page Down · next tab", ("ctrl", "pagedown")),
        Shortcut("ctrl_pageup", "Ctrl+Page Up · previous tab", ("ctrl", "pageup")),
        # Alt+Page Up / Down are the Remote Desktop client's own equivalents of
        # Alt+Tab / Alt+Shift+Tab *inside* the remote session.
        Shortcut(SHUFFLE_APPS, "Shuffle apps · random order, no repeats", ("alt", "pageup")),
        Shortcut("alt_pageup", "Alt+Page Up · next app", ("alt", "pageup")),
        Shortcut("alt_pagedown", "Alt+Page Down · previous app", ("alt", "pagedown")),
        Shortcut("off", "Off · no switching", ()),
    )
}

MODE_SWITCH_OPTIONS = {
    BROWSER: ("ctrl_tab", "ctrl_shift_tab", "ctrl_pagedown", "ctrl_pageup", "off"),
    REMOTE_APP: (SHUFFLE_APPS, "alt_pageup", "alt_pagedown", "off"),
}
MODE_DEFAULT_SWITCH = {BROWSER: "ctrl_tab", REMOTE_APP: SHUFFLE_APPS}

SCROLL_KEYS = {  # id -> (label, key for "down", key for "up")
    "page": ("Page Up / Page Down", "pagedown", "pageup"),
    "arrow": ("Arrow Up / Down", "down", "up"),
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
class ActionResult:
    ok: bool
    code: str
    message: str


class KeyboardNavigator:
    def __init__(self, gate: SafetyGate, ledger: InjectionLedger, send=w.send_inputs):
        self.gate = gate
        self.ledger = ledger
        self._send = send

    def send_chord(self, target: RdpWindow, keys: tuple[str, ...], idle_guard_s: float,
                   repeat: int = 1) -> ActionResult:
        text = chord_text(keys, repeat)
        verdict = self.gate.check_keyboard(target, idle_guard_s)
        if not verdict.ok:
            return ActionResult(False, verdict.code, f"Skipped {text}: {verdict.message}")
        events = build_chord(keys, repeat)
        inputs = to_inputs(events)
        # Last-moment check, then one atomic SendInput call for the whole chord.
        if not self.gate.still_foreground(target):
            return ActionResult(False, "not_foreground", f"Skipped {text}: RDP window lost focus")
        sent = self._send(inputs)
        self.ledger.record()
        focus_kept = self.gate.still_foreground(target)
        if sent != len(inputs):
            self._release_partial(target, events[:sent])
            return ActionResult(False, "send_failed",
                                f"{text} was blocked by Windows ({sent}/{len(inputs)} events); "
                                "is the RDP client running as administrator?")
        if not focus_kept:
            return ActionResult(False, "focus_changed",
                                f"Focus left the RDP window while {text} was being sent; automation suspended")
        return ActionResult(True, "ok", f"Sent {text}")

    def _release_partial(self, target: RdpWindow, delivered: list[KeyEvent]) -> None:
        """If Windows accepted only part of a chord, release any key left pressed."""
        pressed: list[str] = []
        for ev in delivered:
            if ev.down:
                pressed.append(ev.key)
            elif ev.key in pressed:
                pressed.remove(ev.key)
        if pressed and self.gate.still_foreground(target):
            self._send(to_inputs([KeyEvent(k, False) for k in reversed(pressed)]))
            self.ledger.record()
