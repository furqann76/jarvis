"""Minimal async AT-SPI2 client speaking D-Bus directly (no PyGObject dependency).

Accessible objects are addressed by (bus_name, object_path) and serialized for the LLM
as a single ref string "<bus_name><object_path>", e.g. ":1.42/org/a11y/atspi/accessible/17".
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

ACCESSIBLE = "org.a11y.atspi.Accessible"
ACTION = "org.a11y.atspi.Action"
COMPONENT = "org.a11y.atspi.Component"
EDITABLE_TEXT = "org.a11y.atspi.EditableText"
TEXT = "org.a11y.atspi.Text"
PROPS = "org.freedesktop.DBus.Properties"
REGISTRY = ("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root")
NULL_PATH = "/org/a11y/atspi/null"
CALL_TIMEOUT_S = 3.0

# AtspiStateType bit positions (subset we expose).
STATE_NAMES = {
    1: "active", 4: "checked", 7: "editable", 8: "enabled", 10: "expanded", 11: "focusable",
    12: "focused", 15: "iconified", 16: "modal", 20: "pressed", 23: "selected",
    24: "sensitive", 25: "showing", 30: "visible", 33: "required", 43: "read_only",
}
WINDOW_ROLES = {"frame", "window", "dialog", "alert", "file chooser"}


class AtspiError(Exception):
    pass


@dataclass(frozen=True)
class Node:
    bus: str
    path: str

    @property
    def ref(self) -> str:
        return f"{self.bus}{self.path}"

    @classmethod
    def from_ref(cls, ref: str) -> "Node":
        idx = ref.find("/")
        if idx <= 0 or not ref.startswith(":"):
            raise AtspiError(f"Malformed element ref: {ref!r}")
        return cls(ref[:idx], ref[idx:])


@dataclass
class NodeInfo:
    node: Node
    role: str
    name: str
    states: set[str] = field(default_factory=set)
    interfaces: set[str] = field(default_factory=set)
    actions: list[str] = field(default_factory=list)
    text: str | None = None
    depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ref": self.node.ref, "role": self.role, "name": self.name}
        shown = sorted(self.states & {"focused", "checked", "selected", "expanded", "editable",
                                      "active", "pressed"})
        if shown:
            d["states"] = shown
        if "sensitive" not in self.states and "enabled" not in self.states:
            d["disabled"] = True
        if self.actions:
            d["actions"] = self.actions
        if self.text:
            d["text"] = self.text
        if self.depth:
            d["depth"] = self.depth
        return d


class AtspiClient:
    def __init__(self) -> None:
        self._bus: MessageBus | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> MessageBus:
        async with self._lock:
            if self._bus is not None and self._bus.connected:
                return self._bus
            session = await MessageBus(bus_type=BusType.SESSION).connect()
            try:
                reply = await session.call(Message(
                    destination="org.a11y.Bus", path="/org/a11y/bus",
                    interface="org.a11y.Bus", member="GetAddress",
                ))
            finally:
                session.disconnect()
            if reply.message_type == MessageType.ERROR:
                raise AtspiError(f"Accessibility bus unavailable: {reply.body}")
            self._bus = await MessageBus(bus_address=reply.body[0]).connect()
            return self._bus

    async def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def _call(self, node: Node, interface: str, member: str,
                    signature: str = "", body: list | None = None) -> list:
        bus = await self.connect()
        msg = Message(destination=node.bus, path=node.path, interface=interface,
                      member=member, signature=signature, body=body or [])
        try:
            reply = await asyncio.wait_for(bus.call(msg), CALL_TIMEOUT_S)
        except TimeoutError as e:
            raise AtspiError(
                f"{member} on {node.ref} timed out (application not responding)"
            ) from e
        if reply.message_type == MessageType.ERROR:
            raise AtspiError(f"{member} on {node.ref} failed: {reply.error_name} {reply.body}")
        return reply.body

    async def _prop(self, node: Node, interface: str, name: str) -> Any:
        body = await self._call(node, PROPS, "Get", "ss", [interface, name])
        return body[0].value

    # ---------------------------------------------------------------- Accessible

    async def children(self, node: Node) -> list[Node]:
        (kids,) = await self._call(node, ACCESSIBLE, "GetChildren")
        return [Node(b, p) for b, p in kids if p != NULL_PATH]

    async def applications(self) -> list[Node]:
        return await self.children(Node(*REGISTRY))

    async def name(self, node: Node) -> str:
        return await self._prop(node, ACCESSIBLE, "Name")

    async def role(self, node: Node) -> str:
        (role,) = await self._call(node, ACCESSIBLE, "GetRoleName")
        return role

    async def states(self, node: Node) -> set[str]:
        (words,) = await self._call(node, ACCESSIBLE, "GetState")
        bits = 0
        for i, word in enumerate(words):
            bits |= word << (32 * i)
        return {name for bit, name in STATE_NAMES.items() if bits >> bit & 1}

    async def interfaces(self, node: Node) -> set[str]:
        (ifaces,) = await self._call(node, ACCESSIBLE, "GetInterfaces")
        return set(ifaces)

    async def application_of(self, node: Node) -> Node:
        (bus, path) = (await self._call(node, ACCESSIBLE, "GetApplication"))[0]
        return Node(bus, path)

    # ---------------------------------------------------------------- Action / text

    async def actions(self, node: Node) -> list[str]:
        (acts,) = await self._call(node, ACTION, "GetActions")
        return [a[0] for a in acts]

    async def do_action(self, node: Node, index: int) -> bool:
        (ok,) = await self._call(node, ACTION, "DoAction", "i", [index])
        return bool(ok)

    async def text(self, node: Node) -> str:
        (txt,) = await self._call(node, TEXT, "GetText", "ii", [0, -1])
        return txt

    async def set_text(self, node: Node, value: str) -> bool:
        (ok,) = await self._call(node, EDITABLE_TEXT, "SetTextContents", "s", [value])
        return bool(ok)

    async def grab_focus(self, node: Node) -> bool:
        (ok,) = await self._call(node, COMPONENT, "GrabFocus")
        return bool(ok)

    async def extents(self, node: Node) -> tuple[int, int, int, int]:
        """(x, y, width, height) in screen coordinates (meaningless under Wayland)."""
        ((x, y, w, h),) = await self._call(node, COMPONENT, "GetExtents", "u", [0])
        return x, y, w, h

    # ---------------------------------------------------------------- composite

    async def info(self, node: Node, depth: int = 0, with_text: bool = True) -> NodeInfo:
        role, name, states, ifaces = await asyncio.gather(
            self.role(node), self.name(node), self.states(node), self.interfaces(node)
        )
        info = NodeInfo(node, role, name or "", states, ifaces, depth=depth)
        if ACTION in ifaces:
            try:
                info.actions = await self.actions(node)
            except AtspiError:
                pass
        if with_text and TEXT in ifaces and (EDITABLE_TEXT in ifaces or not name):
            try:
                txt = await self.text(node)
                info.text = txt[:200] + ("…" if len(txt) > 200 else "") if txt else None
            except AtspiError:
                pass
        return info

    async def windows(self) -> list[tuple[str, NodeInfo]]:
        """(application name, window info) for every top-level window."""
        result: list[tuple[str, NodeInfo]] = []
        for app in await self.applications():
            try:
                app_name = await self.name(app)
                for win in await self.children(app):
                    info = await self.info(win, with_text=False)
                    if info.role in WINDOW_ROLES:
                        result.append((app_name, info))
            except AtspiError:
                continue  # app vanished or is unresponsive
        return result

    async def walk(self, root: Node, max_depth: int, max_nodes: int) -> tuple[list[NodeInfo], bool]:
        """Breadth-first walk of visible nodes. Returns (nodes, truncated)."""
        out: list[NodeInfo] = []
        queue: list[tuple[Node, int]] = [(root, 0)]
        while queue:
            node, depth = queue.pop(0)
            try:
                info = await self.info(node, depth)
            except AtspiError:
                continue
            if depth and "showing" not in info.states and "visible" not in info.states:
                continue  # skip hidden subtrees
            out.append(info)
            if len(out) >= max_nodes:
                return out, True
            if depth < max_depth:
                try:
                    queue.extend((c, depth + 1) for c in await self.children(node))
                except AtspiError:
                    pass
        return out, False
