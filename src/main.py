"""RDP Nexus entry point."""

from __future__ import annotations

import argparse
import ctypes
import logging
import logging.handlers
import os
import sys

# Must happen before any window or coordinate query so all coordinates are physical pixels.
import winapi as w

w.enable_dpi_awareness()

import app_info  # noqa: E402

log = logging.getLogger("rdpnexus")


def setup_logging() -> str:
    folder = os.path.join(app_info.local_data_dir(), "logs")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "rdp-nexus.log")
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if not app_info.is_frozen() and sys.stderr:
        root.addHandler(logging.StreamHandler())
    return path


def acquire_single_instance():
    handle = w.CreateMutexW(None, False, app_info.SINGLE_INSTANCE_MUTEX)
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    return handle, already


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="RDPNexus", description=app_info.APP_NAME)
    parser.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    args = parser.parse_args(argv)
    if args.version:
        print(f"{app_info.APP_NAME} {app_info.APP_VERSION}")
        return 0

    mutex, already = acquire_single_instance()
    if already:
        w.MessageBoxW(None, f"{app_info.APP_NAME} is already running.\nLook for its icon in the system tray.",
                      app_info.APP_NAME, 0x40)
        return 0

    log_path = setup_logging()
    log.info("%s %s starting (log: %s)", app_info.APP_NAME, app_info.APP_VERSION, log_path)
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_info.APP_USER_MODEL_ID)
    except (OSError, AttributeError):
        pass

    from input_safety import InjectionLedger, SafetyGate
    from keyboard_navigation import KeyboardNavigator
    from pointer_controller import ManualMovementMonitor, PointerController
    from scheduler import AutomationEngine, EngineConfig
    from settings import SettingsStore
    from ui import NexusPanel
    from window_detection import WindowInspector

    store = SettingsStore()
    settings = store.load()
    inspector = WindowInspector()
    ledger = InjectionLedger()
    gate = SafetyGate(inspector, ledger)
    monitor = ManualMovementMonitor()
    keyboard = KeyboardNavigator(gate, ledger)
    pointer = PointerController(inspector, gate, ledger, monitor)
    engine = AutomationEngine(inspector, gate, keyboard, pointer, monitor, EngineConfig.from_settings(settings))

    app = NexusPanel(store, engine, inspector, start_hidden=args.minimized)
    try:
        app.mainloop()
    finally:
        engine.shutdown()
        store.save()
        log.info("Exited")
        del mutex
    return 0


if __name__ == "__main__":
    sys.exit(main())
