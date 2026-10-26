"""System-tray icon (pystray).  Menu clicks are queued for the Tk thread to handle,
because Tk must only be touched from the thread that runs its main loop."""

from __future__ import annotations

import logging
import queue

from PIL import Image

log = logging.getLogger(__name__)


class TrayIcon:
    def __init__(self, title: str, image: Image.Image, events: "queue.Queue[str]"):
        self._events = events
        self._icon = None
        self._title = title
        self._image = image
        self._running_label = "Pause"

    def start(self) -> bool:
        try:
            import pystray
        except Exception as exc:  # pragma: no cover - packaging problem
            log.error("System tray unavailable: %s", exc)
            return False

        def emit(name):
            return lambda _icon, _item: self._events.put(name)

        menu = pystray.Menu(
            pystray.MenuItem("Show RDP Nexus", emit("show"), default=True),
            pystray.MenuItem(lambda _item: self._running_label, emit("toggle")),
            pystray.MenuItem("Stop", emit("stop")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", emit("exit")),
        )
        try:
            self._icon = pystray.Icon("RDPNexus", self._image, self._title, menu)
            self._icon.run_detached()
            return True
        except Exception as exc:
            log.error("Could not create tray icon: %s", exc)
            self._icon = None
            return False

    @property
    def available(self) -> bool:
        return self._icon is not None

    def update(self, title: str, toggle_label: str) -> None:
        if self._icon is None:
            return
        changed = toggle_label != self._running_label
        self._running_label = toggle_label
        if title != self._title:
            self._title = title
            try:
                self._icon.title = title[:127]
            except Exception:
                pass
        if changed:
            try:
                self._icon.update_menu()
            except Exception:
                pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
