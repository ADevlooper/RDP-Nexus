"""Which app-switching keys does the RDP client forward to the remote session?

Remote Desktop Connection decides where Windows key combinations such as Alt+Tab go
from its "Apply Windows key combinations" option (`keyboardhook` in the .rdp file):

    0  On this computer             -> Alt+Tab switches LOCAL apps
    1  On the remote computer       -> Alt+Tab goes to the remote session
    2  Only when using full screen  -> remote only while the window is full screen (default)

When the client forwards Windows key combinations it does so through a keyboard hook and
passes Alt+Page Up through unchanged, so the remote session ignores it.  When it does not,
Alt+Page Up is its built-in stand-in for a remote Alt+Tab.  Verified against a live
full-screen mstsc session: Alt+Page Up did nothing, Alt+Tab switched the remote app.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass

log = logging.getLogger(__name__)

HOOK_LOCAL, HOOK_REMOTE, HOOK_FULLSCREEN = 0, 1, 2
DEFAULT_HOOK = HOOK_FULLSCREEN
_HOOK_RE = re.compile(r"^\s*keyboardhook\s*:\s*i\s*:\s*(\d+)\s*$", re.I | re.M)
_RDP_ARG_RE = re.compile(r'"([^"]+\.rdp)"|(\S+\.rdp)\b', re.I)


def parse_keyboard_hook(text: str) -> int:
    m = _HOOK_RE.search(text or "")
    if not m:
        return DEFAULT_HOOK
    value = int(m.group(1))
    return value if value in (HOOK_LOCAL, HOOK_REMOTE, HOOK_FULLSCREEN) else DEFAULT_HOOK


def read_rdp_file(path: str) -> str:
    """.rdp files are usually UTF-16 LE with a BOM; tolerate UTF-8/ANSI too."""
    with open(path, "rb") as fh:
        raw = fh.read(256 * 1024)
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff") or b"\x00" in raw[:200]:
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def rdp_file_from_command_line(cmdline: str | None) -> str | None:
    if not cmdline:
        return None
    for quoted, bare in _RDP_ARG_RE.findall(cmdline):
        path = quoted or bare
        if path.lower().endswith("mstsc.exe"):
            continue
        return path
    return None


def default_rdp_path() -> str:
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:  # CSIDL_PERSONAL (Documents)
            return os.path.join(buf.value, "Default.rdp")
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Documents", "Default.rdp")


def process_command_line(pid: int) -> str | None:
    """Command line of another process (via CIM; no extra dependencies)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
            capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.strip() or None
    except Exception as exc:
        log.info("Could not read the RDP client's command line: %s", exc)
        return None


@dataclass(frozen=True)
class KeyboardHookInfo:
    mode: int
    source: str  # where the value came from (for the log/UI)


def windows_keys_go_remote(mode: int, fullscreen: bool) -> bool:
    return mode == HOOK_REMOTE or (mode == HOOK_FULLSCREEN and fullscreen)


class RdpKeyboardSettings:
    """Looks up (and caches per RDP client process) the keyboardhook setting."""

    def __init__(self, cmdline_reader=process_command_line, default_path=default_rdp_path, reader=read_rdp_file):
        self._cmdline = cmdline_reader
        self._default_path = default_path
        self._read = reader
        self._lock = threading.Lock()
        self._cache: dict[int, KeyboardHookInfo] = {}

    def lookup(self, pid: int, process_name: str) -> KeyboardHookInfo:
        with self._lock:
            if pid in self._cache:
                return self._cache[pid]
        info = self._lookup(pid, process_name)
        log.info("RDP keyboard setting for pid %s: keyboardhook=%s (%s)", pid, info.mode, info.source)
        with self._lock:
            self._cache[pid] = info
        return info

    def _lookup(self, pid: int, process_name: str) -> KeyboardHookInfo:
        if process_name != "mstsc.exe":
            return KeyboardHookInfo(DEFAULT_HOOK, f"{process_name}: assumed default (full screen only)")
        path = rdp_file_from_command_line(self._cmdline(pid)) or self._default_path()
        try:
            return KeyboardHookInfo(parse_keyboard_hook(self._read(path)), os.path.basename(path))
        except OSError:
            return KeyboardHookInfo(DEFAULT_HOOK, "no .rdp file found: assumed default (full screen only)")
