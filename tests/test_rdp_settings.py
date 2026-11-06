import pytest

from rdp_settings import (
    HOOK_FULLSCREEN, HOOK_LOCAL, HOOK_REMOTE, RdpKeyboardSettings, parse_keyboard_hook, rdp_file_from_command_line,
    read_rdp_file, windows_keys_go_remote,
)


@pytest.mark.parametrize("text,expected", [
    ("screen mode id:i:2\r\nkeyboardhook:i:2\r\nfull address:s:host\r\n", HOOK_FULLSCREEN),
    ("keyboardhook:i:0\r\n", HOOK_LOCAL),
    ("KeyboardHook:i:1", HOOK_REMOTE),
    ("full address:s:host\r\n", HOOK_FULLSCREEN),   # missing -> mstsc default
    ("keyboardhook:i:7\r\n", HOOK_FULLSCREEN),      # invalid -> default
    ("", HOOK_FULLSCREEN),
])
def test_parse_keyboard_hook(text, expected):
    assert parse_keyboard_hook(text) == expected


def test_reads_utf16_and_utf8_rdp_files(tmp_path):
    f = tmp_path / "a.rdp"
    f.write_bytes("\ufeffscreen mode id:i:1\r\nkeyboardhook:i:1\r\n".encode("utf-16-le"))
    assert parse_keyboard_hook(read_rdp_file(str(f))) == HOOK_REMOTE
    g = tmp_path / "b.rdp"
    g.write_text("keyboardhook:i:0\n", encoding="utf-8")
    assert parse_keyboard_hook(read_rdp_file(str(g))) == HOOK_LOCAL


@pytest.mark.parametrize("cmdline,expected", [
    ('"C:\\WINDOWS\\system32\\mstsc.exe" ', None),
    ('"C:\\WINDOWS\\system32\\mstsc.exe" "C:\\Users\\me\\Desktop\\Work PC.rdp"', "C:\\Users\\me\\Desktop\\Work PC.rdp"),
    ("mstsc.exe C:\\rdp\\server.rdp /f", "C:\\rdp\\server.rdp"),
    ("mstsc.exe /v:host", None),
    (None, None),
])
def test_rdp_file_from_command_line(cmdline, expected):
    assert rdp_file_from_command_line(cmdline) == expected


def test_decision_matrix():
    assert windows_keys_go_remote(HOOK_FULLSCREEN, True) is True
    assert windows_keys_go_remote(HOOK_FULLSCREEN, False) is False
    assert windows_keys_go_remote(HOOK_REMOTE, False) is True
    assert windows_keys_go_remote(HOOK_LOCAL, True) is False


def test_lookup_prefers_rdp_file_on_command_line(tmp_path):
    work = tmp_path / "work.rdp"
    work.write_text("keyboardhook:i:0\n", encoding="utf-8")
    default = tmp_path / "Default.rdp"
    default.write_text("keyboardhook:i:1\n", encoding="utf-8")
    calls = []

    def cmdline(pid):
        calls.append(pid)
        return f'mstsc.exe "{work}"'

    s = RdpKeyboardSettings(cmdline_reader=cmdline, default_path=lambda: str(default))
    assert s.lookup(10, "mstsc.exe").mode == HOOK_LOCAL
    assert s.lookup(10, "mstsc.exe").mode == HOOK_LOCAL and calls == [10]  # cached per process


def test_lookup_uses_default_rdp_without_file(tmp_path):
    default = tmp_path / "Default.rdp"
    default.write_bytes("keyboardhook:i:1\r\n".encode("utf-16"))
    s = RdpKeyboardSettings(cmdline_reader=lambda pid: '"C:\\WINDOWS\\system32\\mstsc.exe" ',
                            default_path=lambda: str(default))
    info = s.lookup(11, "mstsc.exe")
    assert info.mode == HOOK_REMOTE and info.source == "Default.rdp"


def test_lookup_missing_file_and_other_clients(tmp_path):
    s = RdpKeyboardSettings(cmdline_reader=lambda pid: None, default_path=lambda: str(tmp_path / "nope.rdp"))
    assert s.lookup(12, "mstsc.exe").mode == HOOK_FULLSCREEN
    assert s.lookup(13, "msrdc.exe").mode == HOOK_FULLSCREEN
