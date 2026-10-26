"""Find, identify and continuously validate the user-selected RDP client window.

A target is accepted only when it is a top-level window that belongs to a known
Remote Desktop client process AND has a known RDP session window class.  The
connection dialog, error dialogs and every other local window are rejected.

Window handles can be reused by Windows after a window is destroyed, so every
check re-confirms the handle still belongs to the same process and class that
the user selected.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from ctypes import wintypes

import winapi as w

# mstsc.exe = classic Remote Desktop Connection; msrdc.exe = session process of the
# Microsoft Remote Desktop / Windows App clients (it shares the mstsc core).
RDP_PROCESS_NAMES = frozenset({"mstsc.exe", "msrdc.exe"})
# TscShellContainerClass = full remote-desktop session window.
# RAIL_WINDOW = a RemoteApp (published application) window.
RDP_SESSION_CLASSES = frozenset({"TscShellContainerClass", "RAIL_WINDOW"})
# mstsc's "Input Capture Window": receives the input that is forwarded to the remote side.
INPUT_CHILD_CLASS = "IHWindowClass"
DIALOG_CLASS = "#32770"


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def inset(self, margin: int) -> "Rect | None":
        r = Rect(self.left + margin, self.top + margin, self.right - margin, self.bottom - margin)
        return r if r.width > 0 and r.height > 0 else None

    def intersect(self, other: "Rect") -> "Rect | None":
        r = Rect(max(self.left, other.left), max(self.top, other.top),
                 min(self.right, other.right), min(self.bottom, other.bottom))
        return r if r.width > 0 and r.height > 0 else None


@dataclass(frozen=True)
class RdpWindow:
    """Identity of the selected RDP window, captured when the user selected it."""

    hwnd: int
    pid: int
    process_name: str
    class_name: str
    title: str
    has_input_child: bool

    @property
    def kind(self) -> str:
        return "RemoteApp" if self.class_name == "RAIL_WINDOW" else "Desktop session"

    def label(self) -> str:
        title = self.title.strip() or "(untitled)"
        if len(title) > 48:
            title = title[:45] + "…"
        return f"{title}  ·  {self.process_name}"


@dataclass(frozen=True)
class TargetStatus:
    ok: bool
    code: str
    message: str


# Status codes (stable strings, also used by tests and the UI).
OK = "ok"
NO_TARGET = "no_target"
CLOSED = "closed"
REPLACED = "replaced"
HUNG = "hung"
MINIMIZED = "minimized"
HIDDEN = "hidden"
DIALOG = "dialog"
DISCONNECTED = "disconnected"
NOT_FOREGROUND = "not_foreground"

_STATUS_TEXT = {
    OK: "Connected and in the foreground",
    NO_TARGET: "No RDP window selected",
    CLOSED: "RDP window was closed",
    REPLACED: "RDP window handle now belongs to a different window",
    HUNG: "RDP client is not responding",
    MINIMIZED: "RDP window is minimized",
    HIDDEN: "RDP window is hidden or on another virtual desktop",
    DIALOG: "RDP client is showing a dialog (session may be disconnected)",
    DISCONNECTED: "RDP session appears disconnected",
    NOT_FOREGROUND: "RDP window is not the active window",
}


def status(code: str, detail: str | None = None) -> TargetStatus:
    return TargetStatus(code == OK, code, detail or _STATUS_TEXT[code])


class WindowInspector:
    """Read-only queries about windows.  Never changes focus or window state."""

    def __init__(self, process_names=RDP_PROCESS_NAMES, session_classes=RDP_SESSION_CLASSES):
        self.process_names = frozenset(n.lower() for n in process_names)
        self.session_classes = frozenset(session_classes)

    # -- raw window facts -------------------------------------------------------
    @staticmethod
    def class_name(hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        n = w.GetClassNameW(hwnd, buf, 256)
        return buf.value[:n] if n else ""

    @staticmethod
    def title(hwnd: int) -> str:
        length = w.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        w.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    @staticmethod
    def pid(hwnd: int) -> int:
        out = wintypes.DWORD(0)
        w.GetWindowThreadProcessId(hwnd, ctypes.byref(out))
        return int(out.value)

    @staticmethod
    def process_name(pid: int) -> str:
        if pid <= 0:
            return ""
        handle = w.OpenProcess(w.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if not w.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value).lower()
        finally:
            w.CloseHandle(handle)

    @staticmethod
    def is_cloaked(hwnd: int) -> bool:
        value = wintypes.DWORD(0)
        hr = w.DwmGetWindowAttribute(hwnd, w.DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
        return hr == 0 and value.value != 0

    @staticmethod
    def foreground_root() -> int:
        fg = w.hwnd_int(w.GetForegroundWindow())
        if not fg:
            return 0
        return w.hwnd_int(w.GetAncestor(fg, w.GA_ROOT)) or fg

    @staticmethod
    def root_at(x: int, y: int) -> int:
        hwnd = w.hwnd_int(w.WindowFromPoint(wintypes.POINT(x, y)))
        if not hwnd:
            return 0
        return w.hwnd_int(w.GetAncestor(hwnd, w.GA_ROOT)) or hwnd

    @staticmethod
    def point_on_monitor(x: int, y: int) -> bool:
        return bool(w.MonitorFromPoint(wintypes.POINT(x, y), w.MONITOR_DEFAULTTONULL))

    @staticmethod
    def window_rect(hwnd: int) -> Rect | None:
        r = wintypes.RECT()
        if not w.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return Rect(r.left, r.top, r.right, r.bottom)

    @staticmethod
    def client_rect_on_screen(hwnd: int) -> Rect | None:
        r = wintypes.RECT()
        if not w.GetClientRect(hwnd, ctypes.byref(r)):
            return None
        origin = wintypes.POINT(0, 0)
        if not w.ClientToScreen(hwnd, ctypes.byref(origin)):
            return None
        rect = Rect(origin.x, origin.y, origin.x + r.right, origin.y + r.bottom)
        return rect if rect.width > 0 and rect.height > 0 else None

    @staticmethod
    def find_child(hwnd: int, class_name: str) -> int:
        found: list[int] = []

        @w.WNDENUMPROC
        def _cb(child, _lparam):
            if WindowInspector.class_name(child) == class_name:
                found.append(w.hwnd_int(child))
                return False
            return True

        w.EnumChildWindows(hwnd, _cb, 0)
        return found[0] if found else 0

    def owned_dialog_visible(self, hwnd: int, pid: int) -> bool:
        """True when a visible dialog (#32770) owned by the target window exists."""
        hit: list[int] = []

        @w.WNDENUMPROC
        def _cb(top, _lparam):
            top = w.hwnd_int(top)
            if (w.IsWindowVisible(top)
                    and w.hwnd_int(w.GetWindow(top, w.GW_OWNER)) == hwnd
                    and self.pid(top) == pid
                    and self.class_name(top) == DIALOG_CLASS):
                hit.append(top)
                return False
            return True

        w.EnumWindows(_cb, 0)
        return bool(hit)

    # -- RDP identification -------------------------------------------------------
    def describe(self, hwnd: int) -> RdpWindow | None:
        """Return an RdpWindow if `hwnd` is a supported RDP session window, else None."""
        return self._describe(hwnd)[0]

    def explain_rejection(self, hwnd: int) -> str:
        return self._describe(hwnd)[1]

    def _describe(self, hwnd: int) -> tuple[RdpWindow | None, str]:
        if not hwnd or not w.IsWindow(hwnd):
            return None, "No window"
        root = w.hwnd_int(w.GetAncestor(hwnd, w.GA_ROOT)) or hwnd
        if root != hwnd:
            return None, "Not a top-level window"
        pid = self.pid(hwnd)
        proc = self.process_name(pid)
        cls = self.class_name(hwnd)
        if proc not in self.process_names:
            shown = proc or "an unknown process"
            return None, f"Window belongs to {shown}, not a Remote Desktop client"
        if cls not in self.session_classes:
            return None, f"{proc} window '{cls}' is not an RDP session window (e.g. the connection dialog)"
        has_child = bool(self.find_child(hwnd, INPUT_CHILD_CLASS))
        return RdpWindow(hwnd, pid, proc, cls, self.title(hwnd), has_child), ""

    def list_rdp_windows(self) -> list[RdpWindow]:
        handles: list[int] = []

        @w.WNDENUMPROC
        def _cb(hwnd, _lparam):
            handles.append(w.hwnd_int(hwnd))
            return True

        w.EnumWindows(_cb, 0)
        result = []
        for hwnd in handles:
            if not w.IsWindowVisible(hwnd):
                continue
            info = self.describe(hwnd)
            if info is not None:
                result.append(info)
        return result

    # -- validation ----------------------------------------------------------------
    def check(self, target: RdpWindow | None, require_foreground: bool = True) -> TargetStatus:
        """Validate the target.  Order matters: the cheapest decisive checks run first."""
        if target is None:
            return status(NO_TARGET)
        hwnd = target.hwnd
        if not w.IsWindow(hwnd):
            return status(CLOSED)
        if self.pid(hwnd) != target.pid or self.class_name(hwnd) != target.class_name:
            return status(REPLACED)
        if w.IsHungAppWindow(hwnd):
            return status(HUNG)
        if w.IsIconic(hwnd):
            return status(MINIMIZED)
        if not w.IsWindowVisible(hwnd) or self.is_cloaked(hwnd):
            return status(HIDDEN)
        if not w.IsWindowEnabled(hwnd) or self.owned_dialog_visible(hwnd, target.pid):
            return status(DIALOG)
        if target.has_input_child:
            child = self.find_child(hwnd, INPUT_CHILD_CLASS)
            if not child or not w.IsWindowVisible(child):
                return status(DISCONNECTED)
        if require_foreground and self.foreground_root() != hwnd:
            return status(NOT_FOREGROUND)
        return status(OK)

    def is_foreground(self, target: RdpWindow) -> bool:
        """Cheapest possible last-moment check, used right before SendInput."""
        return self.foreground_root() == target.hwnd

    def input_area(self, target: RdpWindow) -> Rect | None:
        """Screen rectangle (physical pixels) that shows the remote session.

        Uses mstsc's input-capture child when present (it excludes scrollbars and
        the client frame); otherwise the top-level client area.  Title bars and
        borders are never included because only client coordinates are used.
        """
        client = self.client_rect_on_screen(target.hwnd)
        if client is None:
            return None
        if target.has_input_child:
            child = self.find_child(target.hwnd, INPUT_CHILD_CLASS)
            if child and w.IsWindowVisible(child):
                child_rect = self.client_rect_on_screen(child)
                if child_rect is not None:
                    return child_rect.intersect(client)
            return None
        return client
