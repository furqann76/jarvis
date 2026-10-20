"""Bringing the Jarvis window to the front when the wake word fires.

GNOME on Wayland won't let a window raise or un-minimize itself without an activation
token, and a background wake-word trigger has none, so the window would open behind
whatever the user is doing. Under XWayland, mutter honours _NET_ACTIVE_WINDOW requests
flagged as coming from a pager, which un-minimizes, raises and focuses the window. So on
Wayland sessions the GUI runs on Qt's xcb platform.

Qt's xcb plugin needs libxcb-cursor0. If the system lacks it, setup_env.sh unpacks the
.deb into ~/.local/share/jarvis/xcb-libs (no root needed) and we preload it here.
"""

import ctypes
import ctypes.util
import logging
import os
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

LOCAL_XCB_CURSOR = Path(
    "~/.local/share/jarvis/xcb-libs/root/usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0"
).expanduser()


def _load_xcb_cursor() -> bool:
    candidates = [ctypes.util.find_library("xcb-cursor"), str(LOCAL_XCB_CURSOR)]
    for lib in filter(None, candidates):
        try:
            ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
            return True
        except OSError:
            continue
    return False


def configure_platform(prefer_x11: bool = True) -> str | None:
    """Call before creating QApplication. Returns the platform chosen, if any."""
    if os.environ.get("QT_QPA_PLATFORM"):
        return os.environ["QT_QPA_PLATFORM"]  # explicit choice (e.g. offscreen) wins
    if not prefer_x11 or os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland":
        return None
    if not os.environ.get("DISPLAY"):
        log.warning("No XWayland DISPLAY; the window may open behind other windows")
        return None
    if not _load_xcb_cursor():
        log.warning("libxcb-cursor0 missing (run scripts/setup_env.sh); using native "
                    "Wayland, so the window may open behind other windows")
        return None
    os.environ["QT_QPA_PLATFORM"] = "xcb"
    return "xcb"


def _x11_activate(window_id: int) -> bool:
    try:
        from Xlib import X, display, protocol
    except ImportError:
        return False
    try:
        d = display.Display()
        try:
            root = d.screen().root
            target = d.create_resource_object("window", window_id)
            event = protocol.event.ClientMessage(
                window=target,
                client_type=d.intern_atom("_NET_ACTIVE_WINDOW"),
                data=(32, [2, X.CurrentTime, 0, 0, 0]),  # 2 = request from a pager
            )
            root.send_event(event,
                            event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
            d.flush()
        finally:
            d.close()
        return True
    except Exception as e:  # noqa: BLE001 - activation is best effort
        log.warning("X11 activation failed: %s", e)
        return False


def bring_to_front(widget: QWidget) -> None:
    """Show, un-minimize, raise and focus `widget`, as far as the platform allows."""
    widget.showNormal()
    widget.raise_()
    widget.activateWindow()
    if QGuiApplication.platformName() == "xcb":
        window_id = int(widget.winId())
        _x11_activate(window_id)
        # A freshly mapped window can miss the first request; ask again shortly after.
        QTimer.singleShot(250, lambda: _x11_activate(window_id))
