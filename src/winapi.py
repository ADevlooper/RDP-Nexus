"""Minimal ctypes bindings for the documented Win32 APIs RDP Nexus relies on.

Only user32 / kernel32 / dwmapi functions are used, every one of them with
explicit argtypes/restype so 64-bit handles are never truncated.  Input is
injected exclusively through SendInput (the documented, foreground-routed
input path).  Nothing here posts or sends window messages to other processes.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
HHOOK = wintypes.HANDLE
HMONITOR = wintypes.HANDLE
HDESK = wintypes.HANDLE

# --- constants ---------------------------------------------------------------
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_MOVE_NOCOALESCE = 0x2000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

WH_MOUSE_LL = 14
HC_ACTION = 0
LLMHF_INJECTED = 0x00000001

WM_QUIT = 0x0012
WM_MOUSEMOVE = 0x0200
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
PM_NOREMOVE = 0x0000

GA_ROOT = 2
GW_OWNER = 4

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

MONITOR_DEFAULTTONULL = 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_CLOAKED = 14
SPI_GETSCREENSAVERRUNNING = 0x0072
UOI_NAME = 2
DESKTOP_READOBJECTS = 0x0001

GWL_STYLE = -16
GWL_EXSTYLE = -20

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04
VK_XBUTTON1 = 0x05
VK_XBUTTON2 = 0x06
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C


# --- structures --------------------------------------------------------------
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


def _bind(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


# --- user32 --------------------------------------------------------------------
GetForegroundWindow = _bind(user32, "GetForegroundWindow", wintypes.HWND)
GetAncestor = _bind(user32, "GetAncestor", wintypes.HWND, wintypes.HWND, wintypes.UINT)
GetWindow = _bind(user32, "GetWindow", wintypes.HWND, wintypes.HWND, wintypes.UINT)
IsWindow = _bind(user32, "IsWindow", wintypes.BOOL, wintypes.HWND)
IsWindowVisible = _bind(user32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
IsWindowEnabled = _bind(user32, "IsWindowEnabled", wintypes.BOOL, wintypes.HWND)
IsIconic = _bind(user32, "IsIconic", wintypes.BOOL, wintypes.HWND)
IsHungAppWindow = _bind(user32, "IsHungAppWindow", wintypes.BOOL, wintypes.HWND)
GetWindowThreadProcessId = _bind(
    user32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
)
GetClassNameW = _bind(user32, "GetClassNameW", ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
GetWindowTextW = _bind(user32, "GetWindowTextW", ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
GetWindowTextLengthW = _bind(user32, "GetWindowTextLengthW", ctypes.c_int, wintypes.HWND)
EnumWindows = _bind(user32, "EnumWindows", wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
EnumChildWindows = _bind(user32, "EnumChildWindows", wintypes.BOOL, wintypes.HWND, WNDENUMPROC, wintypes.LPARAM)
GetClientRect = _bind(user32, "GetClientRect", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
GetWindowRect = _bind(user32, "GetWindowRect", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
ClientToScreen = _bind(user32, "ClientToScreen", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.POINT))
WindowFromPoint = _bind(user32, "WindowFromPoint", wintypes.HWND, wintypes.POINT)
MonitorFromPoint = _bind(user32, "MonitorFromPoint", HMONITOR, wintypes.POINT, wintypes.DWORD)
GetCursorPos = _bind(user32, "GetCursorPos", wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
GetSystemMetrics = _bind(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
GetAsyncKeyState = _bind(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
GetLastInputInfo = _bind(user32, "GetLastInputInfo", wintypes.BOOL, ctypes.POINTER(LASTINPUTINFO))
SendInput = _bind(user32, "SendInput", wintypes.UINT, wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
SetWindowsHookExW = _bind(
    user32, "SetWindowsHookExW", HHOOK, ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD
)
CallNextHookEx = _bind(user32, "CallNextHookEx", LRESULT, HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
UnhookWindowsHookEx = _bind(user32, "UnhookWindowsHookEx", wintypes.BOOL, HHOOK)
GetMessageW = _bind(user32, "GetMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                    wintypes.UINT, wintypes.UINT)
PeekMessageW = _bind(user32, "PeekMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                     wintypes.UINT, wintypes.UINT, wintypes.UINT)
PostThreadMessageW = _bind(user32, "PostThreadMessageW", wintypes.BOOL, wintypes.DWORD, wintypes.UINT,
                           wintypes.WPARAM, wintypes.LPARAM)
OpenInputDesktop = _bind(user32, "OpenInputDesktop", HDESK, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
CloseDesktop = _bind(user32, "CloseDesktop", wintypes.BOOL, HDESK)
GetUserObjectInformationW = _bind(user32, "GetUserObjectInformationW", wintypes.BOOL, wintypes.HANDLE,
                                  ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
SystemParametersInfoW = _bind(user32, "SystemParametersInfoW", wintypes.BOOL, wintypes.UINT, wintypes.UINT,
                              wintypes.LPVOID, wintypes.UINT)
GetParent = _bind(user32, "GetParent", wintypes.HWND, wintypes.HWND)
MessageBoxW = _bind(user32, "MessageBoxW", ctypes.c_int, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                    wintypes.UINT)
if hasattr(user32, "GetWindowLongPtrW"):
    GetWindowLongPtrW = _bind(user32, "GetWindowLongPtrW", ctypes.c_ssize_t, wintypes.HWND, ctypes.c_int)
    SetWindowLongPtrW = _bind(user32, "SetWindowLongPtrW", ctypes.c_ssize_t, wintypes.HWND, ctypes.c_int,
                              ctypes.c_ssize_t)
else:  # 32-bit Python
    GetWindowLongPtrW = _bind(user32, "GetWindowLongW", ctypes.c_long, wintypes.HWND, ctypes.c_int)
    SetWindowLongPtrW = _bind(user32, "SetWindowLongW", ctypes.c_long, wintypes.HWND, ctypes.c_int, ctypes.c_long)

# --- kernel32 ------------------------------------------------------------------
GetTickCount = _bind(kernel32, "GetTickCount", wintypes.DWORD)
GetCurrentThreadId = _bind(kernel32, "GetCurrentThreadId", wintypes.DWORD)
GetModuleHandleW = _bind(kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
OpenProcess = _bind(kernel32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
CloseHandle = _bind(kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
QueryFullProcessImageNameW = _bind(kernel32, "QueryFullProcessImageNameW", wintypes.BOOL, wintypes.HANDLE,
                                   wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
CreateMutexW = _bind(kernel32, "CreateMutexW", wintypes.HANDLE, wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)

# --- dwmapi --------------------------------------------------------------------
DwmGetWindowAttribute = _bind(dwmapi, "DwmGetWindowAttribute", ctypes.c_long, wintypes.HWND, wintypes.DWORD,
                              wintypes.LPVOID, wintypes.DWORD)
DwmSetWindowAttribute = _bind(dwmapi, "DwmSetWindowAttribute", ctypes.c_long, wintypes.HWND, wintypes.DWORD,
                              wintypes.LPCVOID, wintypes.DWORD)


# --- small helpers -------------------------------------------------------------
def hwnd_int(value) -> int:
    """Normalise a ctypes HWND (None for NULL) to a plain int."""
    return int(value or 0)


def tick_count() -> int:
    return int(GetTickCount())


def tick_delta(later: int, earlier: int) -> int:
    """Signed difference of two 32-bit GetTickCount values (handles wrap-around)."""
    delta = (later - earlier) & 0xFFFFFFFF
    return delta - 0x100000000 if delta >= 0x80000000 else delta


def last_input_tick() -> int:
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    if not GetLastInputInfo(ctypes.byref(info)):
        return tick_count()
    return int(info.dwTime)


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    if not GetCursorPos(ctypes.byref(pt)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pt.x, pt.y


def key_is_down(vk: int) -> bool:
    return bool(GetAsyncKeyState(vk) & 0x8000)


def virtual_screen() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the whole virtual desktop in physical pixels."""
    return (
        GetSystemMetrics(SM_XVIRTUALSCREEN),
        GetSystemMetrics(SM_YVIRTUALSCREEN),
        GetSystemMetrics(SM_CXVIRTUALSCREEN),
        GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def send_inputs(inputs: list[INPUT]) -> int:
    """Insert all events with ONE SendInput call so they cannot be interleaved."""
    if not inputs:
        return 0
    array = (INPUT * len(inputs))(*inputs)
    return int(SendInput(len(inputs), array, ctypes.sizeof(INPUT)))


def enable_dpi_awareness() -> None:
    """Per-monitor DPI awareness so every coordinate we read or send is a physical pixel.

    CustomTkinter requests the same mode (PROCESS_PER_MONITOR_DPI_AWARE); doing it
    first guarantees the worker threads see physical coordinates from the start.
    """
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        try:
            user32.SetProcessDPIAware()
        except (OSError, AttributeError):
            pass
