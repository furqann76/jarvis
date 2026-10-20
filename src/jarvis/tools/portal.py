"""xdg-desktop-portal client: RemoteDesktop (keyboard/mouse) + ScreenCast (screen frames).

This is the Wayland-sanctioned way to synthesize input. The first session shows a GNOME
consent dialog; the returned restore token is persisted so later sessions start silently
(until the user revokes it in Settings → Privacy).
"""

import asyncio
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

log = logging.getLogger(__name__)

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REMOTE = "org.freedesktop.portal.RemoteDesktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"

DEVICE_KEYBOARD, DEVICE_POINTER = 1, 2
SOURCE_MONITOR = 1
PERSIST_UNTIL_REVOKED = 2
BTN = {"left": 0x110, "right": 0x111, "middle": 0x112}  # linux evdev codes
USER_RESPONSE_TIMEOUT_S = 120

KEYSYMS = {
    "ctrl": 0xFFE3, "control": 0xFFE3, "shift": 0xFFE1, "alt": 0xFFE9, "super": 0xFFEB,
    "win": 0xFFEB, "meta": 0xFFEB, "enter": 0xFF0D, "return": 0xFF0D, "escape": 0xFF1B,
    "esc": 0xFF1B, "tab": 0xFF09, "backspace": 0xFF08, "delete": 0xFFFF, "del": 0xFFFF,
    "insert": 0xFF63, "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54, "space": 0x20,
    "print": 0xFF61, "menu": 0xFF67, "capslock": 0xFFE5,
    **{f"f{i}": 0xFFBE + i - 1 for i in range(1, 13)},
}


class PortalError(Exception):
    pass


def char_keysym(ch: str) -> int:
    if ch == "\n":
        return KEYSYMS["enter"]
    if ch == "\t":
        return KEYSYMS["tab"]
    code = ord(ch)
    return code if 0x20 <= code <= 0x7E else 0x01000000 + code


def key_keysym(name: str) -> int:
    key = name.strip().lower()
    if key in KEYSYMS:
        return KEYSYMS[key]
    if len(name.strip()) == 1:
        return char_keysym(name.strip().lower())
    raise PortalError(f"Unknown key name: {name!r}")


@dataclass
class Stream:
    node_id: int
    x: int
    y: int
    width: int
    height: int


class PortalClient:
    def __init__(self, token_file: str | Path):
        self.token_file = Path(token_file).expanduser()
        self._bus: MessageBus | None = None
        self._session: str | None = None
        self._stream: Stream | None = None
        self._lock = asyncio.Lock()

    # --------------------------------------------------------------- plumbing

    async def _connect(self) -> MessageBus:
        if self._bus is None or not self._bus.connected:
            self._bus = await MessageBus(
                bus_type=BusType.SESSION, negotiate_unix_fd=True
            ).connect()
        return self._bus

    async def _call(self, interface: str, member: str, signature: str, body: list) -> list:
        bus = await self._connect()
        reply = await bus.call(Message(destination=PORTAL, path=PORTAL_PATH,
                                       interface=interface, member=member,
                                       signature=signature, body=body))
        if reply.message_type == MessageType.ERROR:
            raise PortalError(f"{interface}.{member}: {reply.error_name} {reply.body}")
        return reply.body

    async def _request(self, interface: str, member: str, signature: str,
                       args: list, options: dict[str, Variant]) -> dict[str, Any]:
        """Call a portal method that answers through a Request.Response signal."""
        bus = await self._connect()
        token = "jarvis_" + secrets.token_hex(6)
        sender = bus.unique_name[1:].replace(".", "_")
        request_path = f"{PORTAL_PATH}/request/{sender}/{token}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def handler(msg: Message) -> None:
            if (msg.message_type == MessageType.SIGNAL and msg.path == request_path
                    and msg.member == "Response" and not future.done()):
                future.set_result(msg.body)

        match = (f"type='signal',interface='{REQUEST}',member='Response',"
                 f"path='{request_path}'")
        await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                               interface="org.freedesktop.DBus", member="AddMatch",
                               signature="s", body=[match]))
        bus.add_message_handler(handler)
        try:
            options = {**options, "handle_token": Variant("s", token)}
            await self._call(interface, member, signature + "a{sv}", [*args, options])
            code, results = await asyncio.wait_for(future, USER_RESPONSE_TIMEOUT_S)
        except TimeoutError as e:
            raise PortalError(f"No response to the {member} permission dialog") from e
        finally:
            bus.remove_message_handler(handler)
        if code == 1:
            raise PortalError(f"Permission for {member} was denied in the system dialog")
        if code != 0:
            raise PortalError(f"{member} failed (portal response {code})")
        return {k: v.value for k, v in results.items()}

    # --------------------------------------------------------------- remote desktop

    async def ensure_session(self) -> None:
        async with self._lock:
            if self._session is not None:
                return
            res = await self._request(REMOTE, "CreateSession", "", [], {
                "session_handle_token": Variant("s", "jarvis_" + secrets.token_hex(6)),
            })
            session = res["session_handle"]
            device_opts: dict[str, Variant] = {
                "types": Variant("u", DEVICE_KEYBOARD | DEVICE_POINTER),
                "persist_mode": Variant("u", PERSIST_UNTIL_REVOKED),
            }
            token = self._load_token()
            if token:
                device_opts["restore_token"] = Variant("s", token)
            await self._request(REMOTE, "SelectDevices", "o", [session], device_opts)
            # A monitor stream is required for absolute pointer coordinates.
            await self._request(SCREENCAST, "SelectSources", "o", [session], {
                "types": Variant("u", SOURCE_MONITOR),
                "multiple": Variant("b", False),
            })
            started = await self._request(REMOTE, "Start", "os", [session, ""], {})
            if started.get("restore_token"):
                self._save_token(started["restore_token"])
            streams = started.get("streams") or []
            if not streams:
                raise PortalError("No screen was shared; mouse positioning needs one monitor")
            node_id, props = streams[0]
            props = {k: v.value if isinstance(v, Variant) else v for k, v in props.items()}
            pos = props.get("position", (0, 0))
            size = props.get("size", (0, 0))
            self._stream = Stream(node_id, pos[0], pos[1], size[0], size[1])
            self._session = session
            log.info("Remote desktop session started: %s", self._stream)

    @property
    def stream(self) -> Stream | None:
        return self._stream

    def _load_token(self) -> str | None:
        try:
            return self.token_file.read_text().strip() or None
        except OSError:
            return None

    def _save_token(self, token: str) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(token)
        self.token_file.chmod(0o600)

    async def _notify(self, member: str, signature: str, args: list) -> None:
        await self.ensure_session()
        try:
            await self._call(REMOTE, member, "oa{sv}" + signature, [self._session, {}, *args])
        except PortalError:
            self._session = None  # session was closed (e.g. revoked); recreate next time
            raise

    async def key(self, keysym: int, pressed: bool) -> None:
        await self._notify("NotifyKeyboardKeysym", "iu", [keysym, 1 if pressed else 0])

    async def type_text(self, text: str, delay_s: float = 0.008) -> None:
        for ch in text:
            sym = char_keysym(ch)
            await self.key(sym, True)
            await self.key(sym, False)
            await asyncio.sleep(delay_s)

    async def press_combo(self, keys: list[str]) -> None:
        syms = [key_keysym(k) for k in keys]
        for sym in syms:
            await self.key(sym, True)
            await asyncio.sleep(0.02)
        for sym in reversed(syms):
            await self.key(sym, False)
            await asyncio.sleep(0.02)

    async def move(self, x: float, y: float) -> None:
        """Move to (x, y) in screen coordinates of the shared monitor's layout."""
        await self.ensure_session()
        assert self._stream
        sx, sy = x - self._stream.x, y - self._stream.y
        if not (0 <= sx < self._stream.width and 0 <= sy < self._stream.height):
            raise PortalError(f"({x}, {y}) is outside the shared screen")
        await self._notify("NotifyPointerMotionAbsolute", "udd",
                           [self._stream.node_id, float(sx), float(sy)])

    async def click(self, x: float, y: float, button: str = "left", count: int = 1) -> None:
        await self.move(x, y)
        await asyncio.sleep(0.05)
        for _ in range(count):
            await self._notify("NotifyPointerButton", "iu", [BTN[button], 1])
            await asyncio.sleep(0.03)
            await self._notify("NotifyPointerButton", "iu", [BTN[button], 0])
            await asyncio.sleep(0.08)

    async def scroll(self, dx: int, dy: int) -> None:
        if dy:
            await self._notify("NotifyPointerAxisDiscrete", "ui", [0, dy])
        if dx:
            await self._notify("NotifyPointerAxisDiscrete", "ui", [1, dx])

    # --------------------------------------------------------------- screenshot

    async def screenshot(self, out_dir: str | Path) -> Path:
        """Grab one frame of the shared monitor as PNG via the ScreenCast stream.

        (The Screenshot portal refuses non-interactive captures for host apps on GNOME.)
        The image covers exactly the stream, so pixel (x, y) is screen
        (stream.x + x, stream.y + y) when the image size equals the stream size.
        """
        await self.ensure_session()
        assert self._stream and self._session
        bus = await self._connect()
        reply = await bus.call(Message(
            destination=PORTAL, path=PORTAL_PATH, interface=SCREENCAST,
            member="OpenPipeWireRemote", signature="oa{sv}", body=[self._session, {}],
        ))
        if reply.message_type == MessageType.ERROR or not reply.unix_fds:
            raise PortalError(f"OpenPipeWireRemote failed: {reply.body}")
        fd = reply.unix_fds[reply.body[0]]
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        pattern = out / f"frame-{secrets.token_hex(4)}-%d.png"
        try:
            proc = await asyncio.create_subprocess_exec(
                "gst-launch-1.0", "-q", "pipewiresrc", f"fd={fd}",
                f"path={self._stream.node_id}", "num-buffers=2", "always-copy=true",
                "!", "videoconvert", "!", "pngenc", "snapshot=false",
                "!", "multifilesink", f"location={pattern}",
                pass_fds=(fd,), stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), 10)
            except TimeoutError:
                proc.kill()
                err = b"timed out waiting for a frame"
        finally:
            os.close(fd)
        frames = sorted(out.glob(pattern.name.replace("%d", "*")))
        if not frames:
            raise PortalError(f"Screen capture failed: {err.decode(errors='replace').strip()}")
        for extra in frames[:-1]:
            extra.unlink(missing_ok=True)
        return frames[-1]

    async def close(self) -> None:
        if self._session and self._bus and self._bus.connected:
            try:
                await self._bus.call(Message(destination=PORTAL, path=self._session,
                                             interface=SESSION, member="Close"))
            except Exception:  # noqa: BLE001 - best-effort on shutdown
                pass
        self._session = None
        if self._bus:
            self._bus.disconnect()
            self._bus = None
