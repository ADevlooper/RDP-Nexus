"""Compact floating control panel (CustomTkinter).

The UI never performs automation itself: it sends commands to the engine and
polls `engine.snapshot()` every 200 ms, so the GUI thread is never blocked.
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser

import customtkinter as ctk
from PIL import Image

import app_info
import autostart
import winapi as w
from keyboard_navigation import (
    MAX_APPS, MIN_APPS, MODE_SWITCH_OPTIONS, MODES, REMOTE_APP, SCROLL_DIRECTIONS, SCROLL_KEYS, SHUFFLE_APPS,
    SWITCH_SHORTCUTS,
)
from scheduler import AutomationEngine, DisplayState, EngineConfig, PointerState, Snapshot
from settings import (
    INTERVAL_MAX, INTERVAL_MIN, POINTER_INTERVAL_MAX, POINTER_INTERVAL_MIN, POINTER_RANGE_MAX, POINTER_RANGE_MIN,
    RESUME_POLICIES, SettingsStore,
)
from system_tray import TrayIcon
from window_detection import RdpWindow, WindowInspector

log = logging.getLogger(__name__)

C = {
    "bg": "#13151A",
    "surface": "#1B1E25",
    "surface2": "#232731",
    "border": "#2D323D",
    "hover": "#2B303B",
    "text": "#E8EAF0",
    "muted": "#959CA9",
    "faint": "#616876",
    "accent": "#4F8CFF",
    "accent_hover": "#6B9EFF",
    "teal": "#2DD4BF",
    "running": "#34D399",
    "paused": "#FBBF24",
    "stopped": "#8B93A1",
    "unavailable": "#F87171",
    "manual": "#C084FC",
}
STATE_COLOR = {
    DisplayState.STOPPED: C["stopped"],
    DisplayState.RUNNING: C["running"],
    DisplayState.PAUSED: C["paused"],
    DisplayState.UNAVAILABLE: C["unavailable"],
}
POINTER_COLOR = {
    PointerState.ACTIVE: C["teal"],
    PointerState.ARMING: C["accent"],
    PointerState.SUSPENDED: C["paused"],
    PointerState.MANUAL: C["manual"],
    PointerState.FAULT: C["unavailable"],
}
GLYPH = {
    "play": "", "pause": "", "stop": "", "keyboard": "", "mouse": "",
    "down": "", "up": "", "minimize": "", "close": "", "refresh": "",
    "grip": "",
}
FALLBACK_GLYPH = {
    "play": "▶", "pause": "❚❚", "stop": "■", "keyboard": "⌨", "mouse": "🖱", "down": "▾", "up": "▴",
    "minimize": "—", "close": "✕", "refresh": "⟳", "grip": "⋮⋮",
}
PLACEHOLDER = "Select RDP window…"
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWCP_ROUND = 2


def fmt_countdown(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    s = int(seconds + 0.999)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Tooltip:
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self._tip: tk.Toplevel | None = None
        self._after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._after = self.widget.after(550, self._show)

    def _cancel(self):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self):
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=self.text, bg=C["surface2"], fg=C["text"], bd=0, padx=8, pady=4,
                 font=("Segoe UI", 9), justify="left", wraplength=320).pack()
        tip.wm_geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide(self, _e=None):
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


class Stepper(ctk.CTkFrame):
    """[−] value [+] numeric input with clamping."""

    def __init__(self, master, value, lo, hi, step, on_change, fmt=str, cast=int, width=52):
        super().__init__(master, fg_color="transparent")
        self.lo, self.hi, self.step, self.cast, self.fmt = lo, hi, step, cast, fmt
        self._on_change = on_change
        self._var = tk.StringVar(value=fmt(value))
        btn = dict(width=28, height=28, corner_radius=8, fg_color=C["surface2"], hover_color=C["hover"],
                   text_color=C["text"], font=("Segoe UI", 14))
        ctk.CTkButton(self, text="−", command=lambda: self._bump(-1), **btn).pack(side="left")
        self.entry = ctk.CTkEntry(self, width=width, height=28, justify="center", textvariable=self._var,
                                  fg_color=C["bg"], border_color=C["border"], text_color=C["text"])
        self.entry.pack(side="left", padx=4)
        ctk.CTkButton(self, text="+", command=lambda: self._bump(1), **btn).pack(side="left")
        self.entry.bind("<Return>", self._commit)
        self.entry.bind("<FocusOut>", self._commit)
        self._value = value

    def _bump(self, direction):
        self.set(self._value + direction * self.step, notify=True)

    def _commit(self, _e=None):
        try:
            v = self.cast(float(self._var.get().strip()))
        except ValueError:
            v = self._value
        self.set(v, notify=True)

    def set(self, value, notify=False):
        v = self.cast(min(max(value, self.lo), self.hi))
        changed = v != self._value
        self._value = v
        self._var.set(self.fmt(v))
        if notify and changed:
            self._on_change(v)

    def configure_state(self, state):
        for child in self.winfo_children():
            child.configure(state=state)


class NexusPanel(ctk.CTk):
    POLL_MS = 200

    def __init__(self, store: SettingsStore, engine: AutomationEngine, inspector: WindowInspector,
                 start_hidden: bool = False):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__(fg_color=C["bg"])
        self.store = store
        self.engine = engine
        self.inspector = inspector
        self.settings = store.current
        self._tray_events: queue.Queue[str] = queue.Queue()
        self._save_after = None
        self._targets: dict[str, RdpWindow] = {}
        self._pick_deadline: float | None = None
        self._last_event_stamp = None
        self._closing = False
        self._drag = (0, 0)
        self._hwnd = 0
        self._applied: dict[int, dict] = {}

        self.title(app_info.APP_NAME)
        try:
            self.iconbitmap(app_info.resource_path("assets", "app.ico"))
        except tk.TclError:
            pass
        self._init_fonts()
        self.overrideredirect(True)
        self.attributes("-topmost", self.settings.always_on_top)
        self.protocol("WM_DELETE_WINDOW", self.quit_app)

        self._build()
        self._apply_settings_to_widgets()
        self._place_window()
        self.after(30, self._apply_native_style)

        self.tray = TrayIcon(app_info.APP_NAME, Image.open(app_info.resource_path("assets", "app.png")),
                             self._tray_events)
        self.tray.start()
        if start_hidden and self.tray.available:
            self.after(60, self.withdraw)
        self.refresh_targets()
        self.after(self.POLL_MS, self._poll)

    # ----------------------------------------------------------------- setup
    def _init_fonts(self):
        families = set(tkfont.families(self))
        self._icon_family = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families), None)
        self._ui_family = next((f for f in ("Segoe UI Variable Text", "Segoe UI") if f in families), "Arial")
        disp = "Segoe UI Variable Display" if "Segoe UI Variable Display" in families else self._ui_family
        self.f_body = ctk.CTkFont(self._ui_family, 12)
        self.f_small = ctk.CTkFont(self._ui_family, 11)
        self.f_bold = ctk.CTkFont(self._ui_family, 12, "bold")
        self.f_title = ctk.CTkFont(self._ui_family, 13, "bold")
        self.f_count = ctk.CTkFont(disp, 20, "bold")
        self.f_icon = ctk.CTkFont(self._icon_family, 14) if self._icon_family else ctk.CTkFont(self._ui_family, 13)

    def glyph(self, name):
        return GLYPH[name] if self._icon_family else FALLBACK_GLYPH[name]

    def _icon_button(self, master, name, command, tip, width=32):
        b = ctk.CTkButton(master, text=self.glyph(name), font=self.f_icon, width=width, height=32, corner_radius=8,
                          fg_color="transparent", hover_color=C["hover"], text_color=C["text"],
                          command=command)
        b.tooltip = Tooltip(b, tip)
        return b

    def _apply_native_style(self):
        """Taskbar presence for the frameless window + Windows 11 rounded corners/border."""
        try:
            hwnd = w.hwnd_int(w.GetParent(self.winfo_id())) or self.winfo_id()
            self._hwnd = hwnd
            ex = w.GetWindowLongPtrW(hwnd, w.GWL_EXSTYLE)
            w.SetWindowLongPtrW(hwnd, w.GWL_EXSTYLE, (ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
            pref = ctypes.c_int(DWMWCP_ROUND)
            w.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), 4)
            border = ctypes.c_uint(0x003D322D)  # COLORREF (0x00BBGGRR) of C["border"]
            w.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(border), 4)
            if self.state() != "withdrawn":
                self.withdraw()
                self.after(10, self.deiconify)
        except Exception:
            log.exception("Could not apply native window style")

    # ----------------------------------------------------------------- layout
    def _build(self):
        outer = ctk.CTkFrame(self, fg_color=C["bg"], corner_radius=0, border_width=0)
        outer.pack(fill="both", expand=True)
        self.outer = outer

        bar = ctk.CTkFrame(outer, fg_color=C["bg"], corner_radius=0)
        bar.pack(fill="x", padx=8, pady=(8, 2))
        self.bar = bar

        grip = ctk.CTkLabel(bar, text=self.glyph("grip"), font=self.f_icon, text_color=C["faint"], width=18,
                            cursor="fleur")
        grip.pack(side="left", padx=(2, 4))

        pill = ctk.CTkFrame(bar, fg_color=C["surface"], corner_radius=14, height=30)
        pill.pack(side="left")
        self.dot = ctk.CTkLabel(pill, text="●", font=ctk.CTkFont(self._ui_family, 13), text_color=C["stopped"],
                                width=14)
        self.dot.pack(side="left", padx=(10, 4))
        self.state_lbl = ctk.CTkLabel(pill, text="Stopped", font=self.f_bold, text_color=C["text"])
        self.state_lbl.pack(side="left", padx=(0, 12))
        self.state_tip = Tooltip(pill, "")

        self.count_lbl = ctk.CTkLabel(bar, text="--:--", font=self.f_count, text_color=C["text"], width=78)
        self.count_lbl.pack(side="left", padx=(10, 6))
        self.count_tip = Tooltip(self.count_lbl, "Time until the next app switch / scroll")

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.pack(side="right")
        self.btn_close = self._icon_button(right, "close", self.quit_app, "Exit RDP Nexus")
        self.btn_tray = self._icon_button(right, "minimize", self.hide_to_tray, "Minimize to system tray")
        self.btn_expand = self._icon_button(right, "down", self.toggle_expanded, "Show settings")
        sep = ctk.CTkFrame(right, fg_color=C["border"], width=1, height=20)
        self.btn_ptr = self._icon_button(right, "mouse", self.toggle_pointer, "Gentle pointer movement on/off")
        self.btn_kb = self._icon_button(right, "keyboard", self.toggle_keyboard, "Keyboard navigation on/off")
        self.btn_stop = self._icon_button(right, "stop", self.engine.stop, "Stop")
        self.btn_play = self._icon_button(right, "play", self.play_pause, "Start")
        for widget in (self.btn_play, self.btn_stop, self.btn_kb, self.btn_ptr):
            widget.pack(side="left", padx=1)
        sep.pack(side="left", padx=5)
        for widget in (self.btn_expand, self.btn_tray, self.btn_close):
            widget.pack(side="left", padx=1)

        line = ctk.CTkFrame(outer, fg_color="transparent")
        line.pack(fill="x", padx=14, pady=(0, 8))
        self.detail_lbl = ctk.CTkLabel(line, text="", font=self.f_small, text_color=C["muted"], anchor="w",
                                       height=18)
        self.detail_lbl.pack(side="left", fill="x", expand=True)
        self.ptr_chip = ctk.CTkButton(line, text="", font=self.f_small, height=20, corner_radius=10, width=10,
                                      fg_color=C["surface"], hover_color=C["hover"], text_color=C["muted"],
                                      command=self.engine.resume_pointer)
        self.ptr_chip.pack(side="right")
        self.ptr_chip_tip = Tooltip(self.ptr_chip, "")

        for widget in (bar, grip, pill, self.dot, self.state_lbl, self.count_lbl, line, self.detail_lbl, outer):
            widget.bind("<ButtonPress-1>", self._drag_start, add="+")
            widget.bind("<B1-Motion>", self._drag_move, add="+")
            widget.bind("<ButtonRelease-1>", self._drag_end, add="+")

        self.details = ctk.CTkFrame(outer, fg_color="transparent")
        self._build_details(self.details)

    def _row(self, master, label, tip=None):
        row = ctk.CTkFrame(master, fg_color="transparent")
        row.pack(fill="x", pady=4)
        lbl = ctk.CTkLabel(row, text=label, font=self.f_body, text_color=C["muted"], width=96, anchor="w")
        lbl.pack(side="left")
        if tip:
            Tooltip(lbl, tip)
        return row

    def _segmented(self, master, values, command, width=None):
        seg = ctk.CTkSegmentedButton(master, values=values, command=command, font=self.f_small, height=28,
                                     fg_color=C["surface2"], selected_color=C["accent"],
                                     selected_hover_color=C["accent_hover"], unselected_color=C["surface2"],
                                     unselected_hover_color=C["hover"], text_color=C["text"],
                                     dynamic_resizing=False, **({"width": width} if width else {}))
        return seg

    def _option(self, master, values, command, width=230):
        return ctk.CTkOptionMenu(master, values=values, command=command, width=width, height=28,
                                 font=self.f_small, dropdown_font=self.f_small, fg_color=C["surface2"],
                                 button_color=C["surface2"], button_hover_color=C["hover"],
                                 dropdown_fg_color=C["surface"], dropdown_hover_color=C["hover"],
                                 text_color=C["text"], dynamic_resizing=False)

    def _switch(self, master, text, command):
        return ctk.CTkSwitch(master, text=text, command=command, font=self.f_body, text_color=C["text"],
                             progress_color=C["accent"], button_color=C["text"], button_hover_color="#FFFFFF",
                             fg_color=C["border"], switch_width=36, switch_height=18)

    def _build_details(self, root):
        tabs = ctk.CTkTabview(root, height=290, fg_color=C["surface"], corner_radius=12,
                              segmented_button_fg_color=C["surface2"],
                              segmented_button_selected_color=C["accent"],
                              segmented_button_selected_hover_color=C["accent_hover"],
                              segmented_button_unselected_color=C["surface2"],
                              segmented_button_unselected_hover_color=C["hover"],
                              text_color=C["text"], border_width=1, border_color=C["border"])
        tabs.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        tabs._segmented_button.configure(font=self.f_small)
        self.tabs = tabs

        # --- Target ---------------------------------------------------------------
        t = tabs.add("Target")
        row = self._row(t, "RDP window", "Choose the Remote Desktop window to automate. Only mstsc/msrdc "
                                          "session windows are accepted.")
        self.target_menu = self._option(row, [PLACEHOLDER], self._on_target_chosen, width=200)
        self.target_menu.pack(side="left")
        self.btn_refresh = self._icon_button(row, "refresh", self.refresh_targets, "Refresh the list", width=30)
        self.btn_refresh.pack(side="left", padx=(4, 0))
        row = self._row(t, "")
        self.btn_pick = ctk.CTkButton(row, text="Pick by clicking it", font=self.f_small, height=28, width=150,
                                      corner_radius=8, fg_color=C["surface2"], hover_color=C["hover"],
                                      text_color=C["text"], command=self.start_pick)
        self.btn_pick.pack(side="left")
        Tooltip(self.btn_pick, "Click this, then click inside your RDP window within 8 seconds.")
        row = self._row(t, "Status")
        self.conn_dot = ctk.CTkLabel(row, text="●", text_color=C["stopped"], font=self.f_body, width=14)
        self.conn_dot.pack(side="left")
        self.conn_lbl = ctk.CTkLabel(row, text="No RDP window selected", font=self.f_small, text_color=C["muted"],
                                     anchor="w", wraplength=260, justify="left")
        self.conn_lbl.pack(side="left", padx=(4, 0), fill="x", expand=True)
        ctk.CTkLabel(t, text="Automation only acts while this window is the active window and the session is "
                             "visible. It never brings the window forward by itself.",
                     font=self.f_small, text_color=C["faint"], wraplength=380, justify="left",
                     anchor="w").pack(fill="x", pady=(8, 0))

        # --- Keys -------------------------------------------------------------------
        k = tabs.add("Keys")
        row = self._row(k, "Keyboard")
        self.sw_keyboard = self._switch(row, "Periodic navigation", self._on_keyboard_switch)
        self.sw_keyboard.pack(side="left")
        row = self._row(k, "Mode", "Remote apps: switch between apps inside the remote Windows session.\n"
                                   "Browser tabs: switch tabs of the browser that is active in the session.")
        self.seg_mode = self._segmented(row, list(MODES.values()), self._on_mode, width=230)
        self.seg_mode.pack(side="left")
        row = self._row(k, "Switch")
        self.switch_menu = self._option(row, ["-"], self._on_switch)
        self.switch_menu.pack(side="left")
        self.apps_row = self._row(k, "Open apps", "How many apps are open in the remote session (shown in its "
                                                  "Alt+Tab list). RDP Nexus cannot see inside the remote screen, "
                                                  "so set this yourself and keep it up to date.")
        self.st_apps = Stepper(self.apps_row, self.settings.app_count, MIN_APPS, MAX_APPS, 1,
                               lambda v: self._apply(app_count=v))
        self.st_apps.pack(side="left")
        row = self._row(k, "Scroll")
        self.scroll_menu = self._option(row, [v[0] for v in SCROLL_KEYS.values()], self._on_scroll_key)
        self.scroll_menu.pack(side="left")
        row = self._row(k, "Direction")
        self.seg_dir = self._segmented(row, list(SCROLL_DIRECTIONS.values()), self._on_scroll_dir, width=230)
        self.seg_dir.pack(side="left")
        row = self._row(k, "Every (min)", "Interval between navigation cycles. Default 13 minutes.")
        self.st_interval = Stepper(row, self.settings.interval_minutes, INTERVAL_MIN, INTERVAL_MAX, 1,
                                   lambda v: self._apply(interval_minutes=v), fmt=lambda v: f"{v:g}",
                                   cast=lambda v: round(float(v), 1))
        self.st_interval.pack(side="left")

        # --- Pointer ------------------------------------------------------------------
        p = tabs.add("Pointer")
        row = self._row(p, "Pointer")
        self.sw_pointer = self._switch(row, "Gentle movement", self._on_pointer_switch)
        self.sw_pointer.pack(side="left")
        row = self._row(p, "Range", "Maximum distance from where the pointer was resting.")
        self.sl_range = ctk.CTkSlider(row, from_=POINTER_RANGE_MIN, to=POINTER_RANGE_MAX,
                                      number_of_steps=POINTER_RANGE_MAX - POINTER_RANGE_MIN, width=170,
                                      command=self._on_range_slide, progress_color=C["accent"],
                                      button_color=C["text"], button_hover_color="#FFFFFF", fg_color=C["border"])
        self.sl_range.pack(side="left")
        self.sl_range.bind("<ButtonRelease-1>", lambda _e: self._apply(pointer_range_px=int(self.sl_range.get())))
        self.range_lbl = ctk.CTkLabel(row, text="", font=self.f_small, text_color=C["text"], width=48)
        self.range_lbl.pack(side="left", padx=(6, 0))
        row = self._row(p, "Every (s)")
        self.st_ptr_interval = Stepper(row, self.settings.pointer_interval_seconds, POINTER_INTERVAL_MIN,
                                       POINTER_INTERVAL_MAX, 5, lambda v: self._apply(pointer_interval_seconds=v))
        self.st_ptr_interval.pack(side="left")
        row = self._row(p, "State")
        self.ptr_state_lbl = ctk.CTkLabel(row, text="", font=self.f_small, text_color=C["muted"], anchor="w",
                                          wraplength=270, justify="left")
        self.ptr_state_lbl.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(p, text="Moves stay inside the RDP window and never click. Touching the mouse pauses "
                             "pointer movement until you resume it.",
                     font=self.f_small, text_color=C["faint"], wraplength=380, justify="left",
                     anchor="w").pack(fill="x", pady=(8, 0))

        # --- Options -----------------------------------------------------------------
        o = tabs.add("Options")
        row = self._row(o, "Window")
        self.sw_topmost = self._switch(row, "Always on top", self._on_topmost)
        self.sw_topmost.pack(side="left")
        row = self._row(o, "RDP returns", "What happens when the RDP window becomes available again after "
                                          "losing focus, being minimized or disconnecting.")
        self.seg_policy = self._segmented(row, ["Resume", "Wait for me"], self._on_policy, width=230)
        self.seg_policy.pack(side="left")
        row = self._row(o, "Startup")
        self.sw_startup = self._switch(row, "Launch when I sign in", self._on_startup)
        self.sw_startup.pack(side="left")
        if not autostart.supported():
            self.sw_startup.configure(state="disabled", text="Launch when I sign in (installed app only)")
        row = self._row(o, "About")
        ctk.CTkLabel(row, text=f"{app_info.APP_NAME} {app_info.APP_VERSION}", font=self.f_small,
                     text_color=C["text"]).pack(side="left")
        link = ctk.CTkLabel(row, text="GitHub", font=self.f_small, text_color=C["accent"], cursor="hand2")
        link.pack(side="left", padx=(10, 0))
        link.bind("<Button-1>", lambda _e: webbrowser.open(app_info.APP_URL))

        # --- Log ------------------------------------------------------------------------
        g = tabs.add("Log")
        self.log_box = ctk.CTkTextbox(g, height=200, font=self.f_small, fg_color=C["bg"], text_color=C["muted"],
                                      border_width=0, wrap="word")
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")
        link = ctk.CTkLabel(g, text="Open log folder", font=self.f_small, text_color=C["accent"], cursor="hand2")
        link.pack(anchor="w", pady=(6, 0))
        link.bind("<Button-1>", lambda _e: self._open_log_folder())

    # ----------------------------------------------------------------- settings
    def _apply_settings_to_widgets(self):
        s = self.settings
        self.sw_keyboard.select() if s.keyboard_enabled else self.sw_keyboard.deselect()
        self.sw_pointer.select() if s.pointer_enabled else self.sw_pointer.deselect()
        self.sw_topmost.select() if s.always_on_top else self.sw_topmost.deselect()
        (self.sw_startup.select() if autostart.is_enabled() else self.sw_startup.deselect())
        self.seg_mode.set(MODES[s.mode])
        self._fill_switch_menu()
        self.scroll_menu.set(SCROLL_KEYS[s.scroll_key][0])
        self.seg_dir.set(SCROLL_DIRECTIONS[s.scroll_direction])
        self.seg_policy.set("Resume" if s.resume_policy == "auto" else "Wait for me")
        self.sl_range.set(s.pointer_range_px)
        self.range_lbl.configure(text=f"{s.pointer_range_px} px")
        self.st_apps.set(s.app_count)
        self.st_interval.set(s.interval_minutes)
        self.st_ptr_interval.set(s.pointer_interval_seconds)
        if s.expanded:
            self.details.pack(fill="both", expand=True)
            self.btn_expand.configure(text=self.glyph("up"))

    def _fill_switch_menu(self):
        s = self.settings
        options = MODE_SWITCH_OPTIONS[s.mode]
        self.switch_menu.configure(values=[SWITCH_SHORTCUTS[o].label for o in options])
        self.switch_menu.set(SWITCH_SHORTCUTS[s.switch_shortcut].label)
        if s.switch_shortcut == SHUFFLE_APPS:
            self.apps_row.pack(fill="x", pady=4, after=self.switch_menu.master)
        else:
            self.apps_row.pack_forget()

    def _apply(self, **changes):
        self.settings = self.store.update(**changes)
        self.engine.set_config(EngineConfig.from_settings(self.settings))
        if self._save_after:
            self.after_cancel(self._save_after)
        self._save_after = self.after(600, self._save)

    def _save(self):
        self._save_after = None
        self.store.save()

    def _on_keyboard_switch(self):
        self._apply(keyboard_enabled=bool(self.sw_keyboard.get()))

    def _on_pointer_switch(self):
        self._apply(pointer_enabled=bool(self.sw_pointer.get()))

    def _on_mode(self, label):
        mode = next(k for k, v in MODES.items() if v == label)
        if mode != self.settings.mode:
            self._apply(mode=mode)
            self._fill_switch_menu()

    def _on_switch(self, label):
        sid = next(k for k, v in SWITCH_SHORTCUTS.items() if v.label == label)
        self._apply(switch_shortcut=sid)
        self._fill_switch_menu()

    def _on_scroll_key(self, label):
        self._apply(scroll_key=next(k for k, v in SCROLL_KEYS.items() if v[0] == label))

    def _on_scroll_dir(self, label):
        self._apply(scroll_direction=next(k for k, v in SCROLL_DIRECTIONS.items() if v == label))

    def _on_range_slide(self, value):
        self.range_lbl.configure(text=f"{int(value)} px")

    def _on_topmost(self):
        on = bool(self.sw_topmost.get())
        self.attributes("-topmost", on)
        self._apply(always_on_top=on)

    def _on_policy(self, label):
        self._apply(resume_policy="auto" if label == "Resume" else "manual")

    def _on_startup(self):
        want = bool(self.sw_startup.get())
        if not autostart.set_enabled(want):
            self.sw_startup.deselect() if want else self.sw_startup.select()

    def toggle_keyboard(self):
        self._apply(keyboard_enabled=not self.settings.keyboard_enabled)
        self.sw_keyboard.select() if self.settings.keyboard_enabled else self.sw_keyboard.deselect()

    def toggle_pointer(self):
        self._apply(pointer_enabled=not self.settings.pointer_enabled)
        self.sw_pointer.select() if self.settings.pointer_enabled else self.sw_pointer.deselect()

    def toggle_expanded(self):
        expanded = not self.settings.expanded
        if expanded:
            self.details.pack(fill="both", expand=True)
            self.btn_expand.configure(text=self.glyph("up"))
            self.btn_expand.tooltip.text = "Hide settings"
        else:
            self.details.pack_forget()
            self.btn_expand.configure(text=self.glyph("down"))
            self.btn_expand.tooltip.text = "Show settings"
        self._apply(expanded=expanded)
        self.after(20, self._keep_on_screen)

    # ----------------------------------------------------------------- target selection
    def refresh_targets(self):
        windows = self.inspector.list_rdp_windows()
        self._targets = {}
        for win in windows:
            label = win.label()
            while label in self._targets:
                label += " "
            self._targets[label] = win
        values = list(self._targets) or ["No RDP windows found"]
        self.target_menu.configure(values=[PLACEHOLDER] + values if self._targets else values)
        current = self.engine.target
        if current is not None:
            match = next((lbl for lbl, win in self._targets.items() if win.hwnd == current.hwnd), None)
            self.target_menu.set(match or current.label())
        else:
            self.target_menu.set(PLACEHOLDER if self._targets else "No RDP windows found")

    def _on_target_chosen(self, label):
        win = self._targets.get(label)
        if win is None:
            return
        if not self.engine.set_target(win):
            self.refresh_targets()

    def start_pick(self):
        if self.engine.snapshot().state in (DisplayState.RUNNING, DisplayState.UNAVAILABLE):
            self._flash_detail("Pause or stop automation before changing the RDP window")
            return
        self._pick_deadline = time.monotonic() + 8.0
        self._pick_tick()

    def _pick_tick(self):
        if self._pick_deadline is None:
            return
        remaining = self._pick_deadline - time.monotonic()
        fg = self.inspector.foreground_root()
        own = {self._hwnd, self.winfo_id()}
        if fg and fg not in own:
            win = self.inspector.describe(fg)
            if win is not None:
                self._pick_deadline = None
                self.btn_pick.configure(text="Pick by clicking it")
                if self.engine.set_target(win):
                    self.refresh_targets()
                    self._flash_detail(f"Selected: {win.label()}")
                return
            self._set(self.conn_lbl, text=self.inspector.explain_rejection(fg))
        if remaining <= 0:
            self._pick_deadline = None
            self.btn_pick.configure(text="Pick by clicking it")
            self._flash_detail("No RDP window was picked")
            return
        self.btn_pick.configure(text=f"Click the RDP window… {int(remaining) + 1}")
        self.after(150, self._pick_tick)

    # ----------------------------------------------------------------- commands
    def play_pause(self):
        state = self.engine.snapshot().state
        if state in (DisplayState.RUNNING, DisplayState.UNAVAILABLE):
            self.engine.pause()
        elif state == DisplayState.PAUSED:
            self.engine.resume()
        else:
            if self.engine.target is None:
                self.settings.expanded or self.toggle_expanded()
                self.tabs.set("Target")
            self.engine.start()

    def hide_to_tray(self):
        if self.tray.available:
            self.withdraw()
        else:
            self._flash_detail("System tray is unavailable")

    def show_window(self):
        self.deiconify()
        self.lift()
        self.attributes("-topmost", self.settings.always_on_top)

    def quit_app(self):
        if self._closing:
            return
        self._closing = True
        try:
            self._remember_position()
            self.store.save()
            self.engine.shutdown()
        finally:
            self.tray.stop()
            self.destroy()

    def _flash_detail(self, text):
        self._set(self.detail_lbl, text=text, text_color=C["text"])
        self._flash_until = time.monotonic() + 3

    def _open_log_folder(self):
        folder = os.path.join(app_info.local_data_dir(), "logs")
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)  # noqa: S606 - opens Explorer on our own folder

    # ----------------------------------------------------------------- window position
    def _drag_start(self, event):
        self._drag = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_move(self, event):
        self.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def _drag_end(self, _event):
        self._remember_position()

    def _remember_position(self):
        try:
            x, y = self.winfo_x(), self.winfo_y()
        except tk.TclError:
            return
        if (x, y) != (self.settings.window_x, self.settings.window_y):
            self.settings = self.store.update(window_x=x, window_y=y)
            if self._save_after:
                self.after_cancel(self._save_after)
            self._save_after = self.after(600, self._save)

    def _place_window(self):
        self.update_idletasks()
        x, y = self.settings.window_x, self.settings.window_y
        if x is None or y is None or not self.inspector.point_on_monitor(x + 30, y + 15):
            vx, vy, vw, _vh = w.virtual_screen()
            x = max(vx, self.winfo_screenwidth() - self.winfo_reqwidth() - 48)
            y = 48
        self.geometry(f"+{x}+{y}")

    def _keep_on_screen(self):
        x, y = self.winfo_x(), self.winfo_y()
        h = self.winfo_height()
        if not self.inspector.point_on_monitor(x + 30, y + h - 10):
            self.geometry(f"+{x}+{max(0, self.winfo_screenheight() - h - 60)}")

    # ----------------------------------------------------------------- polling
    def _poll(self):
        if self._closing:
            return
        try:
            self._handle_tray_events()
            self._render(self.engine.snapshot())
        except Exception:
            log.exception("UI refresh failed")
        self.after(self.POLL_MS, self._poll)

    def _handle_tray_events(self):
        while True:
            try:
                ev = self._tray_events.get_nowait()
            except queue.Empty:
                return
            if ev == "show":
                self.show_window()
            elif ev == "toggle":
                self.play_pause()
            elif ev == "stop":
                self.engine.stop()
            elif ev == "exit":
                self.quit_app()
                return

    def _render(self, snap: Snapshot):
        color = STATE_COLOR[snap.state]
        self._set(self.dot, text_color=color)
        self._set(self.state_lbl, text=snap.state.value)
        target_text = snap.target.label() if snap.target else "No RDP window selected"
        self.state_tip.text = f"Target: {target_text}"

        if not snap.keyboard_enabled and snap.state != DisplayState.STOPPED:
            self._set(self.count_lbl, text="—", text_color=C["faint"])
        else:
            self._set(self.count_lbl, text=fmt_countdown(snap.countdown_s),
                      text_color=C["text"] if snap.countdown_s is not None else C["faint"])

        running = snap.state in (DisplayState.RUNNING, DisplayState.UNAVAILABLE)
        if running:
            self._set(self.btn_play, text=self.glyph("pause"))
            self.btn_play.tooltip.text = "Pause"
        else:
            self._set(self.btn_play, text=self.glyph("play"))
            self.btn_play.tooltip.text = "Resume" if snap.state == DisplayState.PAUSED else "Start"
        self._set(self.btn_stop, state="disabled" if snap.state == DisplayState.STOPPED else "normal",
                  text_color=C["faint"] if snap.state == DisplayState.STOPPED else C["text"])
        self._set(self.btn_kb, text_color=C["accent"] if snap.keyboard_enabled else C["faint"],
                  fg_color=C["surface2"] if snap.keyboard_enabled else "transparent")
        ptr_color = POINTER_COLOR.get(snap.pointer_state, C["accent"]) if snap.pointer_enabled else C["faint"]
        if snap.pointer_enabled and snap.pointer_state in (PointerState.IDLE, PointerState.OFF):
            ptr_color = C["accent"]
        self._set(self.btn_ptr, text_color=ptr_color, fg_color=C["surface2"] if snap.pointer_enabled else "transparent")

        if getattr(self, "_flash_until", 0) < time.monotonic():
            self._set(self.detail_lbl, text=self._shorten(snap.detail), text_color=C["muted"])

        # Pointer chip
        chip, chip_tip, clickable = "", "", False
        if snap.pointer_enabled and snap.state != DisplayState.STOPPED:
            ps = snap.pointer_state
            if ps == PointerState.ACTIVE and snap.pointer_countdown_s is not None:
                chip = f"Pointer · {int(snap.pointer_countdown_s)} s"
            elif ps in (PointerState.MANUAL, PointerState.FAULT):
                chip, clickable = "Resume pointer", True
            elif ps != PointerState.IDLE:
                chip = f"Pointer · {ps.value.lower()}"
            chip_tip = snap.pointer_detail
        if chip:
            self._set(self.ptr_chip, text=f"  {chip}  ", state="normal" if clickable else "disabled",
                      text_color=POINTER_COLOR.get(snap.pointer_state, C["muted"]),
                      text_color_disabled=POINTER_COLOR.get(snap.pointer_state, C["muted"]))
            if not self.ptr_chip.winfo_ismapped():
                self.ptr_chip.pack(side="right")
        elif self.ptr_chip.winfo_ismapped():
            self.ptr_chip.pack_forget()
        self.ptr_chip_tip.text = chip_tip

        # Target tab
        st = snap.target_status
        if st is None:
            self._set(self.conn_dot, text_color=C["stopped"])
            if self._pick_deadline is None:
                self._set(self.conn_lbl, text="No RDP window selected")
        else:
            self._set(self.conn_dot, text_color=C["running"] if st.ok else
                      (C["paused"] if st.code == "not_foreground" else C["unavailable"]))
            if self._pick_deadline is None:
                kind = f"{snap.target.kind} · " if snap.target else ""
                self._set(self.conn_lbl, text=f"{kind}{st.message}")
        locked = snap.state in (DisplayState.RUNNING, DisplayState.UNAVAILABLE)
        for widget in (self.target_menu, self.btn_refresh, self.btn_pick):
            self._set(widget, state="disabled" if locked else "normal")

        state_text = snap.pointer_state.value if snap.pointer_enabled else "Off"
        detail = f" — {snap.pointer_detail}" if snap.pointer_detail and snap.pointer_enabled else ""
        self._set(self.ptr_state_lbl, text=f"{state_text}{detail}")

        # Log
        stamp = snap.events[-1].when if snap.events else None
        if stamp != self._last_event_stamp:
            self._last_event_stamp = stamp
            lines = [f"{time.strftime('%H:%M:%S', time.localtime(e.when))}  {e.message}"
                     for e in reversed(snap.events)]
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.insert("end", "\n".join(lines))
            self.log_box.configure(state="disabled")

        # Tray
        toggle = "Pause" if running else ("Resume" if snap.state == DisplayState.PAUSED else "Start")
        count = f" · {fmt_countdown(snap.countdown_s)}" if snap.countdown_s is not None else ""
        self.tray.update(f"{app_info.APP_NAME} — {snap.state.value}{count}", toggle)

    def _set(self, widget, **options):
        """configure() only when something changed (CTk redraws on every configure)."""
        key = id(widget)
        if self._applied.get(key) != options:
            self._applied[key] = options
            widget.configure(**options)

    @staticmethod
    def _shorten(text: str, limit: int = 70) -> str:
        return text if len(text) <= limit else text[: limit - 1] + "…"
