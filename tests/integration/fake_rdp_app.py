"""Stand-in windows for integration tests (run as a separate process).

Creates:
* a fake RDP session window: class "TscShellContainerClass" with an
  "IHWindowClass" input child, like mstsc.exe;
* a fake *local* application window.

Every keyboard message, mouse move and mouse click either window receives is
printed as a JSON line on stdout, so tests can prove where input landed.
Commands arrive one per line on stdin (see COMMANDS).
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
from ctypes import wintypes

ctypes.windll.shcore.SetProcessDpiAwareness(2)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, wintypes.LPVOID]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetFocus.argtypes = [wintypes.HWND]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
user32.GetStockObject = ctypes.windll.gdi32.GetStockObject
user32.GetStockObject.restype = wintypes.HBRUSH
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]

WM_DESTROY, WM_SETFOCUS, WM_CLOSE = 0x0002, 0x0007, 0x0010
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
WM_MOUSEMOVE = 0x0200
CLICKS = {0x0201: "left_down", 0x0202: "left_up", 0x0203: "left_dbl", 0x0204: "right_down", 0x0205: "right_up",
          0x0207: "middle_down", 0x0208: "middle_up", 0x020A: "wheel", 0x020B: "x_down"}
WM_APP = 0x8000
WS_OVERLAPPEDWINDOW, WS_VISIBLE, WS_CHILD, WS_POPUP, WS_CAPTION = 0x00CF0000, 0x10000000, 0x40000000, 0x80000000, \
    0x00C00000
SW_MINIMIZE, SW_RESTORE, SW_HIDE, SW_SHOW, SW_SHOWNOACTIVATE = 6, 9, 0, 5, 4

COMMANDS = ["focus rdp", "focus local", "minimize rdp", "restore rdp", "hide input", "show input", "dialog",
            "close dialog", "close rdp", "quit"]

_lock = threading.Lock()


def emit(**data):
    with _lock:
        sys.stdout.write(json.dumps(data) + "\n")
        sys.stdout.flush()


hinst = kernel32.GetModuleHandleW(None)
windows: dict[str, int] = {}
names: dict[int, str] = {}


def make_proc(name):
    def proc(hwnd, msg, wparam, lparam):
        who = names.get(hwnd, name)
        if msg in (WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP):
            emit(event="key", window=who, down=msg in (WM_KEYDOWN, WM_SYSKEYDOWN),
                 scan=(lparam >> 16) & 0xFF, extended=bool((lparam >> 24) & 1), vk=int(wparam))
            return 0  # never let DefWindowProc start menu mode
        if msg == WM_MOUSEMOVE:
            pt = wintypes.POINT(ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value)
            user32.ClientToScreen(hwnd, ctypes.byref(pt))
            emit(event="move", window=who, x=pt.x, y=pt.y)
        elif msg in CLICKS:
            emit(event="click", window=who, kind=CLICKS[msg])
        elif msg == WM_SETFOCUS and hwnd == windows.get("rdp") and windows.get("input"):
            user32.SetFocus(windows["input"])  # like mstsc: focus goes to the input-capture child
            return 0
        elif msg == WM_CLOSE:
            return 0  # tests close windows explicitly
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return WNDPROC(proc)


_procs = {}


def register(class_name, label):
    _procs[class_name] = make_proc(label)
    wc = WNDCLASSW(lpfnWndProc=_procs[class_name], hInstance=hinst, lpszClassName=class_name,
                   hCursor=user32.LoadCursorW(None, ctypes.c_void_p(32512)),
                   hbrBackground=user32.GetStockObject(0))
    if not user32.RegisterClassW(ctypes.byref(wc)):
        raise ctypes.WinError(ctypes.get_last_error())


def create(class_name, title, style, x, y, wdt, hgt, parent=None, ex=0):
    hwnd = user32.CreateWindowExW(ex, class_name, title, style, x, y, wdt, hgt, parent, None, hinst, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(hwnd)


def focus(name):
    hwnd = windows[name]
    user32.ShowWindow(hwnd, SW_RESTORE)
    ok = bool(user32.SetForegroundWindow(hwnd))
    return ok, int(user32.GetForegroundWindow() or 0) == hwnd


def handle(cmd):
    if cmd in ("focus rdp", "focus local"):
        ok, is_fg = focus(cmd.split()[1])
        emit(event="focus", target=cmd.split()[1], ok=ok, foreground=is_fg)
    elif cmd == "minimize rdp":
        user32.ShowWindow(windows["rdp"], SW_MINIMIZE)
    elif cmd == "restore rdp":
        ok, is_fg = focus("rdp")
        emit(event="focus", target="rdp", ok=ok, foreground=is_fg)
    elif cmd == "hide input":
        user32.ShowWindow(windows["input"], SW_HIDE)
    elif cmd == "show input":
        user32.ShowWindow(windows["input"], SW_SHOW)
    elif cmd == "dialog":
        windows["dialog"] = create("#32770", "Remote Desktop Connection", WS_POPUP | WS_CAPTION, 400, 300, 360, 160,
                                   parent=windows["rdp"])
        user32.ShowWindow(windows["dialog"], SW_SHOWNOACTIVATE)
    elif cmd == "close dialog" and windows.get("dialog"):
        user32.DestroyWindow(windows.pop("dialog"))
    elif cmd == "close rdp":
        user32.DestroyWindow(windows["rdp"])
    emit(event="done", command=cmd)


def main():
    register("TscShellContainerClass", "rdp")
    register("IHWindowClass", "rdp")
    register("RDPNexusTestLocalApp", "local")
    windows["rdp"] = create("TscShellContainerClass", "test-host - Remote Desktop Connection",
                            WS_OVERLAPPEDWINDOW | WS_VISIBLE, 160, 140, 900, 600)
    names[windows["rdp"]] = "rdp"
    rect = wintypes.RECT()
    user32.GetClientRect(windows["rdp"], ctypes.byref(rect))
    windows["input"] = create("IHWindowClass", "Input Capture Window", WS_CHILD | WS_VISIBLE, 0, 0, rect.right,
                              rect.bottom, parent=windows["rdp"])
    names[windows["input"]] = "rdp"
    windows["local"] = create("RDPNexusTestLocalApp", "Local App (RDP Nexus test)", WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                              1120, 180, 520, 420)
    names[windows["local"]] = "local"

    buf = ctypes.create_unicode_buffer(1024)
    kernel32.GetModuleFileNameW(None, buf, 1024)
    thread_id = kernel32.GetCurrentThreadId()
    emit(event="ready", rdp=windows["rdp"], input=windows["input"], local=windows["local"], pid=os.getpid(),
         process=os.path.basename(buf.value).lower())

    pending: list[str] = []

    def reader():
        for line in sys.stdin:
            with _lock:
                pending.append(line.strip())
            user32.PostThreadMessageW(thread_id, WM_APP, 0, 0)
        user32.PostThreadMessageW(thread_id, WM_APP, 1, 0)

    threading.Thread(target=reader, daemon=True).start()
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.hWnd is None and msg.message == WM_APP:
            with _lock:
                cmds, pending[:] = list(pending), []
            for cmd in cmds:
                if cmd == "quit":
                    return
                handle(cmd)
            if msg.wParam == 1:
                return
            continue
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
