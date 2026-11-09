"""Wait until a remote app switch has visibly finished before scrolling.

Measured on a live full-screen session: the remote screen starts showing the new
app about 0.9 s after Alt is released and is fully drawn after about 1.5 s.  A
scroll sent earlier lands in the old app or is lost, so after every switch the
engine watches the RDP area until it has changed and then stayed still.

Frames are tiny grayscale thumbnails kept in memory only; nothing is saved.
RDP Nexus's own windows (the panel, tooltips) are masked out so the panel's
ticking countdown never counts as a change.
"""

from __future__ import annotations

import os
import time

from PIL import ImageChops, ImageDraw, ImageGrab

import winapi as w
from window_detection import Rect, WindowInspector

THUMB = (192, 108)
CHANGED = 3.0      # % of thumbnail pixels that must differ from the pre-switch frame
STILL = 0.5        # % change between consecutive frames that counts as "still"


def changed_percent(a, b) -> float:
    if a is None or b is None or a.size != b.size:
        return 100.0
    mask = ImageChops.difference(a, b).point(lambda v: 255 if v > 24 else 0)
    return mask.histogram()[255] * 100.0 / (a.size[0] * a.size[1])


def own_window_rects(inspector: WindowInspector) -> list[Rect]:
    """Visible top-level windows of this process (the panel, tooltips, menus)."""
    me = os.getpid()
    rects: list[Rect] = []

    @w.WNDENUMPROC
    def _cb(hwnd, _lparam):
        hwnd = w.hwnd_int(hwnd)
        if w.IsWindowVisible(hwnd) and inspector.pid(hwnd) == me:
            r = inspector.window_rect(hwnd)
            if r is not None:
                rects.append(r)
        return True

    w.EnumWindows(_cb, 0)
    return rects


class SwitchWatcher:
    def __init__(self, inspector: WindowInspector, grab=None, clock=time.monotonic, own_rects=None):
        self.inspector = inspector
        self._grab = grab or (lambda box: ImageGrab.grab(bbox=box, all_screens=True))
        self._clock = clock
        self._own_rects = own_rects or (lambda: own_window_rects(inspector))

    def snapshot(self, area: Rect | None):
        if area is None:
            return None
        try:
            img = self._grab((area.left, area.top, area.right, area.bottom)).convert("L")
            draw = ImageDraw.Draw(img)
            for r in self._own_rects():
                cut = r.intersect(area)
                if cut is not None:
                    draw.rectangle((cut.left - area.left, cut.top - area.top,
                                    cut.right - area.left, cut.bottom - area.top), fill=0)
            return img.resize(THUMB)
        except Exception:
            return None  # e.g. secure desktop / capture blocked: fall back to a fixed wait

    def wait_for_switch(self, area: Rect | None, before, cancel, min_s: float = 0.8, timeout_s: float = 4.0,
                        settle_s: float = 0.3, poll_s: float = 0.1) -> tuple[bool, float]:
        """Return (new app seen, seconds waited).  Never returns before `min_s` (unless cancelled)."""
        start = self._clock()
        if before is None:  # cannot watch the screen: fixed, conservative wait
            cancel.wait(1.5)
            return False, self._clock() - start
        last, seen, still_since = before, False, None
        while not cancel.is_set():
            elapsed = self._clock() - start
            if elapsed >= timeout_s:
                return seen, elapsed
            if cancel.wait(poll_s):
                break
            cur = self.snapshot(area)
            if cur is None:
                continue
            if not seen and changed_percent(before, cur) > CHANGED:
                seen = True
            if seen and changed_percent(last, cur) < STILL:
                still_since = still_since if still_since is not None else self._clock()
                if self._clock() - still_since >= settle_s and self._clock() - start >= min_s:
                    return True, self._clock() - start
            else:
                still_since = None
            last = cur
        return seen, self._clock() - start
