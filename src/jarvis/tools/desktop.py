"""Desktop window introspection and semantic control via AT-SPI.

AT-SPI actions are preferred; when an element has no usable action, raw input (mouse /
keyboard through the RemoteDesktop portal, see tools/input.py) is the fallback the
model can use together with look_at_screen.
"""

import asyncio
import os
from typing import Any

from dbus_fast import Message, MessageType

from jarvis.models.schemas import RiskLevel, ToolResult, max_risk
from jarvis.security.risk import destructive_keyword
from jarvis.security.validation import ValidationError
from jarvis.tools.atspi import (
    ACTION,
    EDITABLE_TEXT,
    AtspiClient,
    AtspiError,
    Node,
    NodeInfo,
)
from jarvis.tools.base import BaseTool

MAX_DEPTH = 25
DEFAULT_MAX_NODES = 150
IGNORED_APPS = {"gnome-shell"}  # the compositor's own "Main stage" is not a window
INTERACTIVE_ROLES = {
    "push button", "toggle button", "check box", "radio button", "menu item",
    "check menu item", "radio menu item", "page tab", "link", "combo box", "entry",
    "text", "password text", "spin button", "slider", "list item", "table cell",
    "tree item", "menu", "toggle", "switch",
}


class DesktopContext:
    """Shared AT-SPI client plus the protections common to all desktop tools."""

    def __init__(self, client: AtspiClient, protected_apps: list[str], own_pid: int | None = None):
        self.client = client
        self.protected = {a.lower() for a in protected_apps}
        self.own_pid = own_pid if own_pid is not None else os.getpid()
        self._pid_cache: dict[str, int | None] = {}
        # Short refs ("d12") for the model; full AT-SPI refs are ~70 characters of D-Bus
        # path that small models copy wrongly.
        self._short_to_full: dict[str, str] = {}
        self._full_to_short: dict[str, str] = {}

    def short_ref(self, full: str) -> str:
        if full not in self._full_to_short:
            short = f"d{len(self._short_to_full) + 1}"
            self._short_to_full[short] = full
            self._full_to_short[full] = short
        return self._full_to_short[full]

    def resolve_ref(self, ref: str) -> str:
        ref = ref.strip()
        if ref in self._short_to_full:
            return self._short_to_full[ref]
        if ref.startswith(":"):
            return ref
        raise AtspiError(f"Unknown element ref '{ref}'. Call inspect_window to get refs.")

    async def pid_of(self, bus_name: str) -> int | None:
        if bus_name not in self._pid_cache:
            bus = await self.client.connect()
            reply = await bus.call(Message(
                destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus", member="GetConnectionUnixProcessID",
                signature="s", body=[bus_name],
            ))
            self._pid_cache[bus_name] = (
                reply.body[0] if reply.message_type != MessageType.ERROR else None
            )
        return self._pid_cache[bus_name]

    async def is_own(self, node: Node) -> bool:
        return await self.pid_of(node.bus) == self.own_pid

    async def app_name(self, node: Node) -> str:
        return await self.client.name(await self.client.application_of(node))

    def is_protected(self, app_name: str) -> bool:
        return app_name.lower() in self.protected

    async def check_target(self, ref: str) -> Node:
        """Parse a ref and refuse Jarvis's own UI (it could otherwise approve itself)."""
        node = Node.from_ref(self.resolve_ref(ref))
        if await self.is_own(node):
            raise AtspiError("Jarvis cannot interact with its own windows.")
        return node

    async def assess_element(self, ref: str, verb: str) -> tuple[RiskLevel, str | None]:
        node = await self.check_target(ref)
        app = await self.app_name(node)
        info = await self.client.info(node, with_text=False)
        label = f"{info.role} '{info.name}' in {app}"
        if self.is_protected(app):
            return RiskLevel.HIGH, f"{verb} {label}: protected application"
        keyword = destructive_keyword(info.name)
        if keyword:
            return RiskLevel.HIGH, f"{verb} {label}: looks high-impact ('{keyword}')"
        return RiskLevel.LOW, f"{verb} {label}"

    async def windows(self) -> list[dict[str, Any]]:
        out = []
        for app, win in await self.client.windows():
            if app in IGNORED_APPS or await self.is_own(win.node):
                continue
            out.append({
                "ref": self.short_ref(win.node.ref),
                "application": app,
                "title": win.name,
                "active": "active" in win.states,
            })
        return out

    @staticmethod
    def match_windows(windows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
        """Windows whose app name or title contains `name` (active ones first)."""
        q = name.lower().removesuffix(".desktop")
        q_short = q.rsplit(".", 1)[-1]  # org.gnome.Calculator -> calculator
        hits = [w for w in windows
                if any(t in f"{w['application']} {w['title']}".lower() for t in (q, q_short))]
        return sorted(hits, key=lambda w: not w["active"])

    async def window_for(self, application: str, wait_s: float = 5.0) -> Node:
        """Window of `application`, waiting briefly for a just-launched app to appear."""
        deadline = asyncio.get_running_loop().time() + wait_s
        while True:
            windows = await self.windows()
            matches = self.match_windows(windows, application)
            if matches or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.5)
        if not matches:
            titles = [f"{w['application']}: {w['title']}" for w in windows]
            raise AtspiError(f"No open window matches '{application}'. Open windows: {titles}. "
                             "Launch it first?")
        return Node.from_ref(self.resolve_ref(matches[0]["ref"]))

    async def find_element(self, application: str, name: str | None,
                           editable: bool = False) -> str:
        """Ref of the best element named `name` in the app's window (exact > prefix > contains).

        With editable=True and no name, the first editable text field is returned.
        """
        root = await self.window_for(application)
        nodes, _ = await self.client.walk(root, MAX_DEPTH, 1500)
        candidates = [n for n in nodes[1:] if _is_meaningful(n)
                      and (EDITABLE_TEXT in n.interfaces if editable
                           else bool(useful_actions(n.actions)))]
        if not name:
            if candidates:
                return candidates[0].node.ref
            raise AtspiError(f"No {'editable field' if editable else 'element'} in {application}.")
        q = _normalize_label(name)
        for test in (lambda n: n == q, lambda n: n.startswith(q), lambda n: q in n):
            hits = [c for c in candidates if test(_normalize_label(c.name))]
            if hits:
                return hits[0].node.ref
        names = sorted({c.name for c in candidates if c.name})[:40]
        raise AtspiError(f"No element named '{name}' in {application}. Available: {names}")

    async def target_from(self, arguments: dict[str, Any], editable: bool = False) -> str:
        """Element ref from either element_ref or application (+ name)."""
        if arguments.get("element_ref"):
            return arguments["element_ref"]
        if arguments.get("application"):
            return await self.find_element(arguments["application"], arguments.get("name"),
                                           editable)
        raise AtspiError("Give element_ref, or application and name.")

    async def active_window(self) -> dict[str, Any] | None:
        return next((w for w in await self.windows() if w["active"]), None)

    async def visible_text(self, application: str, limit: int = 8) -> list[str]:
        """Text currently shown in fields/displays of the app's window (for read-back)."""
        try:
            root = await self.window_for(application, wait_s=0)
            nodes, _ = await self.client.walk(root, MAX_DEPTH, 1500)
        except AtspiError:
            return []
        return [n.text for n in nodes if n.text][:limit]

    async def focused_application(self) -> tuple[str | None, bool]:
        """(app name of the active window or None, whether that window is Jarvis's own)."""
        for app, win in await self.client.windows():
            if "active" in win.states and app not in IGNORED_APPS:
                return app, await self.is_own(win.node)
        return None, False


def _fail(tool_call_id: str, error: str) -> ToolResult:
    return ToolResult(tool_call_id=tool_call_id, success=False, error=error)


class ListWindowsTool(BaseTool):
    name = "list_windows"
    description = "List open application windows with their titles and window refs."
    risk_level = RiskLevel.SAFE
    parameters = {"type": "object", "properties": {}}

    def __init__(self, ctx: DesktopContext):
        self.ctx = ctx

    async def execute(self, tool_call_id: str) -> ToolResult:
        try:
            windows = await self.ctx.windows()
        except (AtspiError, OSError) as e:
            return _fail(tool_call_id, f"Accessibility bus error: {e}")
        return ToolResult(tool_call_id=tool_call_id, success=True, data={"windows": windows})

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


class GetActiveWindowTool(BaseTool):
    name = "get_active_window"
    description = "Get the currently focused application window (excluding Jarvis itself)."
    risk_level = RiskLevel.SAFE
    parameters = {"type": "object", "properties": {}}

    def __init__(self, ctx: DesktopContext):
        self.ctx = ctx

    async def execute(self, tool_call_id: str) -> ToolResult:
        try:
            windows = await self.ctx.windows()
        except (AtspiError, OSError) as e:
            return _fail(tool_call_id, f"Accessibility bus error: {e}")
        active = [w for w in windows if w["active"]]
        if active:
            return ToolResult(tool_call_id=tool_call_id, success=True,
                              data={"active_window": active[0]})
        return ToolResult(
            tool_call_id=tool_call_id, success=True,
            data={"active_window": None,
                  "note": "No other application window is focused (Jarvis may be focused). "
                          "Pick the target from open_windows.",
                  "open_windows": windows},
        )

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


class InspectWindowTool(BaseTool):
    name = "inspect_window"
    description = (
        "List the interactive and text elements of a window (buttons, fields, menus, links) "
        "with element refs for activate_element / set_element_text. Pick the window by "
        "application name or title (it does NOT need to be focused); defaults to the active "
        "window."
    )
    risk_level = RiskLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {
            "application": {
                "type": "string",
                "description": "App name or window title, e.g. 'Calculator', 'Text Editor'.",
            },
            "window_ref": {"type": "string", "description": "Window ref from list_windows."},
            "query": {
                "type": "string",
                "description": "Only return elements whose name/text contains this text.",
            },
            "max_elements": {"type": "integer", "minimum": 10, "maximum": 400},
        },
    }

    def __init__(self, ctx: DesktopContext):
        self.ctx = ctx

    async def execute(
        self,
        tool_call_id: str,
        window_ref: str | None = None,
        application: str | None = None,
        query: str | None = None,
        max_elements: int = DEFAULT_MAX_NODES,
    ) -> ToolResult:
        try:
            if window_ref:
                root = await self.ctx.check_target(window_ref)
            elif application:
                root = await self.ctx.window_for(application)
            else:
                active = [w for w in await self.ctx.windows() if w["active"]]
                if not active:
                    return _fail(tool_call_id, "No active window; pass application "
                                               "(see list_windows).")
                root = Node.from_ref(self.ctx.resolve_ref(active[0]["ref"]))
            # Walk generously, then keep only meaningful nodes.
            nodes, truncated = await self.ctx.client.walk(root, MAX_DEPTH, max_elements * 6)
        except (AtspiError, OSError) as e:
            return _fail(tool_call_id, str(e))

        elements = [n for n in nodes[1:] if _is_meaningful(n)]
        if query:
            q = query.lower()
            elements = [n for n in elements if q in n.name.lower() or q in (n.text or "").lower()]
        truncated = truncated or len(elements) > max_elements
        root_info = nodes[0] if nodes else None
        return ToolResult(
            tool_call_id=tool_call_id,
            success=True,
            data={
                "window": {"ref": self.ctx.short_ref(root.ref),
                           "title": root_info.name if root_info else ""},
                "elements": [_compact(n, self.ctx.short_ref(n.node.ref))
                             for n in elements[:max_elements]],
                "truncated": truncated,
            },
        )

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


# GTK4 exposes text-selection/clipboard helpers as "actions" on every label; they are not
# things a user would click, and listing them doubles the element count.
_NOISE_ACTION_PREFIXES = ("clipboard.", "selection.", "link.", "menu.", "text.")


def useful_actions(actions: list[str]) -> list[str]:
    return [a for a in actions if not a.startswith(_NOISE_ACTION_PREFIXES)]


# ASCII spellings models use for the typographic symbols apps put on buttons.
_SYMBOL_ALIASES = str.maketrans({"*": "×", "x": "×", "/": "÷", "-": "−", "\u200e": None})


def _normalize_label(label: str) -> str:
    text = label.strip().lower()
    return text.translate(_SYMBOL_ALIASES) if len(text) == 1 else text.replace("\u200e", "")


def _is_meaningful(n: NodeInfo) -> bool:
    if useful_actions(n.actions) or n.role in INTERACTIVE_ROLES or EDITABLE_TEXT in n.interfaces:
        return True
    return bool(n.name or n.text) and n.role in {"label", "heading", "static", "paragraph",
                                                 "status bar", "alert", "notification"}


def _compact(n: NodeInfo, ref: str) -> dict[str, Any]:
    d = n.to_dict()
    d["ref"] = ref
    d.pop("depth", None)
    actions = useful_actions(n.actions)
    if actions:
        d["actions"] = actions
    else:
        d.pop("actions", None)
        d.pop("disabled", None)
    return d


class ActivateElementTool(BaseTool):
    name = "activate_element"
    description = (
        "Press/click/toggle desktop UI elements by label: application + name "
        "(e.g. application='Calculator', name='7'), or application + names to press several "
        "in order (names=['1','2','×','7','=']), or element_ref from inspect_window. "
        "Returns the window's visible text afterwards."
    )
    risk_level = RiskLevel.LOW
    parameters = {
        "type": "object",
        "properties": {
            "application": {"type": "string", "description": "App name or window title."},
            "name": {"type": "string", "description": "Visible label of the element."},
            "names": {"type": "array", "items": {"type": "string"}, "maxItems": 40,
                      "description": "Several labels to press in order."},
            "element_ref": {"type": "string", "description": "Element ref (e.g. d12)."},
            "action": {
                "type": "string",
                "description": "Action name (e.g. 'click', 'toggle'); default: the main one.",
            },
        },
    }

    def __init__(self, ctx: DesktopContext):
        self.ctx = ctx

    async def _targets(self, arguments: dict[str, Any]) -> list[str]:
        names = arguments.get("names")
        if names:
            if not arguments.get("application"):
                raise AtspiError("'names' needs 'application'.")
            return [await self.ctx.find_element(arguments["application"], n) for n in names]
        return [await self.ctx.target_from(arguments)]

    async def assess(self, arguments: dict[str, Any]) -> tuple[RiskLevel, str | None]:
        try:
            refs = await self._targets(arguments)
        except AtspiError as e:
            raise ValidationError(str(e)) from e
        assessed = [await self.ctx.assess_element(ref, "activate") for ref in refs]
        risk = max_risk(*(r for r, _ in assessed))
        if len(assessed) == 1:
            return assessed[0]
        labels = ", ".join(repr(a) for a in arguments["names"])
        reasons = [c for r, c in assessed if r == RiskLevel.HIGH]
        return risk, f"press {labels} in {arguments['application']}" + (
            f": {reasons[0]}" if reasons else "")

    async def _activate(self, ref: str, action: str | None) -> NodeInfo:
        node = await self.ctx.check_target(ref)
        info = await self.ctx.client.info(node, with_text=False)
        if ACTION not in info.interfaces or not info.actions:
            raise AtspiError(f"Element '{info.name}' ({info.role}) has no AT-SPI action. "
                             "Use click_on_screen instead.")
        useful = useful_actions(info.actions) or info.actions
        index = info.actions.index(useful[0])
        if action:
            lowered = [a.lower() for a in info.actions]
            if action.lower() not in lowered:
                raise AtspiError(f"Element has no action '{action}'. Available: {useful}")
            index = lowered.index(action.lower())
        if not await self.ctx.client.do_action(node, index):
            raise AtspiError(f"The application refused '{info.actions[index]}' on "
                             f"'{info.name}'.")
        return info

    async def execute(
        self,
        tool_call_id: str,
        element_ref: str | None = None,
        application: str | None = None,
        name: str | None = None,
        names: list[str] | None = None,
        action: str | None = None,
    ) -> ToolResult:
        arguments = {"element_ref": element_ref, "application": application, "name": name,
                     "names": names}
        pressed: list[str] = []
        try:
            for ref in await self._targets(arguments):
                info = await self._activate(ref, action)
                pressed.append(info.name or info.role)
                await asyncio.sleep(0.05)
        except AtspiError as e:
            done = f" (pressed so far: {pressed})" if pressed else ""
            return _fail(tool_call_id, f"{e}{done}")
        data: dict[str, Any] = {"pressed": pressed}
        if application:
            await asyncio.sleep(0.2)  # let the app update its display
            data["window_text"] = await self.ctx.visible_text(application)
        return ToolResult(tool_call_id=tool_call_id, success=True, data=data)

    async def verify(self, execution_result: ToolResult) -> bool:
        # The toolkit's DoAction reply confirms each action ran; an arbitrary button has
        # no generic observable post-state beyond that.
        return execution_result.success


class SetElementTextTool(BaseTool):
    name = "set_element_text"
    description = (
        "Set or append text in an editable field of an app (text editor, form field), by "
        "application (+ optional field name) or element_ref. Does not press Enter."
    )
    risk_level = RiskLevel.LOW
    parameters = {
        "type": "object",
        "properties": {
            "application": {"type": "string", "description": "App name or window title."},
            "name": {"type": "string", "description": "Field label (default: first field)."},
            "element_ref": {"type": "string", "description": "Editable element ref."},
            "text": {"type": "string", "description": "Text to enter."},
            "append": {"type": "boolean", "description": "Append instead of replacing."},
        },
        "required": ["text"],
    }

    def __init__(self, ctx: DesktopContext):
        self.ctx = ctx

    async def assess(self, arguments: dict[str, Any]) -> tuple[RiskLevel, str | None]:
        try:
            ref = await self.ctx.target_from(arguments, editable=True)
        except AtspiError as e:
            raise ValidationError(str(e)) from e
        return await self.ctx.assess_element(ref, "type into")

    async def execute(
        self,
        tool_call_id: str,
        text: str,
        element_ref: str | None = None,
        application: str | None = None,
        name: str | None = None,
        append: bool = False,
    ) -> ToolResult:
        try:
            element_ref = await self.ctx.target_from(
                {"element_ref": element_ref, "application": application, "name": name},
                editable=True)
            node = await self.ctx.check_target(element_ref)
            info = await self.ctx.client.info(node, with_text=False)
            if EDITABLE_TEXT not in info.interfaces:
                return _fail(tool_call_id, f"Element '{info.name}' ({info.role}) is not editable.")
            if info.role == "password text":
                return _fail(tool_call_id, "Jarvis does not type into password fields.")
            current = await self.ctx.client.text(node) if append else ""
            expected = current + text
            if not await self.ctx.client.set_text(node, expected):
                return _fail(tool_call_id, "The application rejected the text change.")
        except AtspiError as e:
            return _fail(tool_call_id, str(e))
        return ToolResult(tool_call_id=tool_call_id, success=True,
                          data={"element_ref": node.ref, "expected_text": expected})

    async def verify(self, execution_result: ToolResult) -> bool:
        if not execution_result.success:
            return False
        data = execution_result.data
        try:
            actual = await self.ctx.client.text(Node.from_ref(data["element_ref"]))
        except AtspiError:
            return False
        return actual == data["expected_text"]


def build_desktop_tools(protected_apps: list[str]) -> tuple[list[BaseTool], DesktopContext]:
    ctx = DesktopContext(AtspiClient(), protected_apps)
    tools: list[BaseTool] = [
        ListWindowsTool(ctx),
        GetActiveWindowTool(ctx),
        InspectWindowTool(ctx),
        ActivateElementTool(ctx),
        SetElementTextTool(ctx),
    ]
    return tools, ctx
