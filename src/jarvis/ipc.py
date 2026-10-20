"""Local IPC between the wake-word daemon and the GUI (Unix socket in $XDG_RUNTIME_DIR)."""

import json
import os
import socket
from pathlib import Path
from typing import Any


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/jarvis-{os.getuid()}"
    path = Path(base) / "jarvis"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def gui_socket_path() -> Path:
    return runtime_dir() / "gui.sock"


def speaking_flag() -> Path:
    """Exists while the GUI is speaking, so the daemon doesn't wake on Jarvis's own voice."""
    return runtime_dir() / "speaking"


def send_to_gui(message: dict[str, Any], timeout_s: float = 2.0) -> bool:
    """Deliver a JSON message to a running GUI. False if no GUI is listening."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_s)
            sock.connect(str(gui_socket_path()))
            sock.sendall(json.dumps(message).encode() + b"\n")
            return True
    except OSError:
        return False
