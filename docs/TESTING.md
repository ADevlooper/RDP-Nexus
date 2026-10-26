# Testing RDP Nexus

## 1. Automated unit tests (any Windows machine, also run in CI)

```bat
.venv\Scripts\python -m pytest
```

They cover settings validation, the shortcut allow-list, the app shuffler (no repeats, every app per
round, the order changes between rounds), chord construction (scan codes, one atomic send, key release
after a partial send), pointer geometry (inside the area, gentle steps, clamping, virtual-desktop
mapping), the safety gate (focus, held keys, recent user activity, tick wrap-around), and the scheduler
state machine (13-minute default, pause/stop cancel, no missed actions after a pause or sleep, resume
policy, no duplicate loops, manual pointer control). No real input is sent.

## 2. Interactive integration tests (real windows, real input; opt-in)

```bat
set RDPNEXUS_INTERACTIVE_TESTS=1
.venv\Scripts\python -m pytest tests\integration -v
```

`tests/integration/fake_rdp_app.py` opens a stand-in RDP window, with mstsc's real classes
`TscShellContainerClass` and `IHWindowClass`, plus a stand-in local app. Both log every key, mouse move
and click they receive. The real engine then sends real `SendInput` events. Checked:

- exact key sequences reach only the RDP window, and none reach the local window;
- switching to the local app suspends automation, and returning restarts the countdown from the full interval;
- nothing is sent when a local app is active from the start;
- minimize, an owned dialog and a hidden input window (disconnect) each suspend automation; closing the window stops it;
- pointer moves stay inside the permitted area, and an outside mouse movement switches to manual control with no further moves;
- focus loss stops pointer moves, and Stop cancels a glide in progress.

Keep your hands off the mouse and keyboard while they run (about 30 seconds). Moving the mouse
counts as manual control and makes the pointer tests fail, which is by design.

## 3. Manual acceptance checklist against a real Remote Desktop session

Use a test VM or test server you're allowed to use. Set **Every (min)** to 1 and **Pointer → Every (s)** to 10
to speed things up. In the remote session open Notepad, Paint and a browser, and set **Open apps** to match.

| # | Check | Expected |
| --- | --- | --- |
| 1 | Launch from the Start Menu | Panel shows the RDP Nexus icon in the taskbar and the tray |
| 2 | Drag the panel, expand/collapse it, toggle Always on top | Responsive, position remembered after restart |
| 3 | Start → click into RDP → Pause → Resume → Stop | States change instantly; countdown restarts on Resume |
| 4 | Default interval | 13:00 with fresh settings |
| 5 | Shuffle apps over 3+ cycles | A different remote app each cycle, never the same twice in a row |
| 6 | Scroll alternate | Page Down, then Page Up, alternating |
| 7 | Pointer glides | Small moves, only over the remote desktop, no clicks |
| 8 | Move the mouse | Panel shows *Manual control* at once; no further pointer moves until *Resume pointer* |
| 9 | Click a local app (e.g. local Notepad) while running | *RDP Unavailable*; type in local Notepad past the interval: no characters, tab switches or scrolls arrive |
| 10 | Minimize the RDP window / disconnect the session | *RDP Unavailable* (minimized / dialog / disconnected) |
| 11 | Keep a local app active, press Start | Nothing is sent locally; status explains why |
| 12 | Switch from RDP to a local app mid-countdown | Suspends immediately; countdown cleared |
| 13 | Watch for a few cycles | Exactly one switch and one scroll per cycle; no clicks or typing |
| 14 | Stop, and Exit from the tray while a glide is running | Movement stops immediately; process exits |
| 15 | Install with "Desktop icon" and "Launch at sign-in", sign out/in, then uninstall from Settings → Apps | Shortcuts and startup entry created, app starts hidden in the tray; uninstall removes them |
| 16 | Push a tag `vX.Y.Z` | The workflow publishes the installer, portable exe and SHA256SUMS.txt |
