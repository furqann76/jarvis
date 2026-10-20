"""Raw keyboard/mouse control and screen vision.

Keyboard and pointer events go through the RemoteDesktop portal (works on Wayland).
Where to click comes from the vision model (click_on_screen / look_at_screen), so the
text-only planner never has to guess pixel coordinates.

Risk rules: input into a protected application (terminal, settings, …) is Tier 2, as is a
click whose described target looks high-impact. Jarvis's own window is moved out of the
way before any input so keystrokes land in the user's application.
"""

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from jarvis.models.ollama_client import OllamaError
from jarvis.models.schemas import RiskLevel, ToolResult
from jarvis.models.vision import VisionModel
from jarvis.security.risk import destructive_keyword
from jarvis.tools.atspi import AtspiError, Node
from jarvis.tools.base import BaseTool
from jarvis.tools.desktop import DesktopContext
from jarvis.tools.portal import PortalClient, PortalError

log = logging.getLogger(__name__)

YieldFocus = Callable[[], Awaitable[None]]
MAX_TYPE_CHARS = 2000


class InputContext:
    def __init__(self, portal: PortalClient, vision: VisionModel | None, desktop: DesktopContext,
                 app_index: Any = None):
        self.portal = portal
        self.vision = vision
        self.desktop = desktop
        self.app_index = app_index  # ApplicationIndex: maps window apps to launcher names
        # Set by the GUI: minimizes the Jarvis window so input reaches the user's app.
        self.yield_focus: YieldFocus | None = None
        self.shots_dir = Path(tempfile.gettempdir()) / "jarvis-screens"

    async def prepare_for_input(self) -> str | None:
        """Get Jarvis out of the way; return the name of the app that will receive input."""
        try:
            app, own = await self.desktop.focused_application()
            if own and self.yield_focus:
                await self.yield_focus()
                await asyncio.sleep(0.4)
                app, own = await self.desktop.focused_application()
            return None if own else app
        except AtspiError:
            return None

    async def focus(self, application: str) -> str:
        """Make sure `application`'s window has keyboard focus, or raise FocusError.

        Keystrokes go to whatever is focused, so typing without this check can land in
        the wrong app. Wayland doesn't let us raise windows directly, so we try AT-SPI
        focus and then a mouse click on the window's title bar.
        """
        await self.prepare_for_input()
        matches = self.desktop.match_windows
        try:
            windows = await self.desktop.windows()
        except AtspiError as e:
            raise FocusError(f"Cannot check which window is focused: {e}") from e
        targets = matches(windows, application)
        if not targets:
            raise FocusError(f"No open window matches '{application}'. Launch it first.")
        target = targets[0]
        if target["active"]:
            return target["application"]

        node = Node.from_ref(self.desktop.resolve_ref(target["ref"]))
        try:
            await self.desktop.client.grab_focus(node)
            if await self._wait_active(application, 0.6):
                return target["application"]
        except AtspiError:
            pass
        # GNOME's overview search (Super, type the app's name, Enter) switches to a running
        # app's window. It is real keyboard input, so focus-stealing prevention allows it,
        # and it works for windows hidden behind others (unlike clicking a title bar).
        launcher_name = (
            self.app_index.display_name(target["application"]) if self.app_index else None
        )
        if launcher_name:
            try:
                await self.portal.press_combo(["super"])
                await asyncio.sleep(0.6)
                await self.portal.type_text(launcher_name)
                await asyncio.sleep(0.6)
                await self.portal.press_combo(["enter"])
                # The overview's closing animation takes a moment: poll, don't sleep once.
                if await self._wait_active(application, 3.0):
                    return target["application"]
                if (await self.desktop.focused_application())[0] is None:
                    await self.portal.press_combo(["escape"])  # overview still open
            except (PortalError, AtspiError, OSError) as e:
                log.info("Focus via overview failed: %s", e)
        if self.vision is not None:
            shot = None
            try:
                shot = await self.capture()
                point = await self.vision.locate(
                    shot, f'the title bar of the "{target["title"] or application}" window')
                if point is not None:
                    stream = self.portal.stream
                    await self.portal.click(stream.x + point[0], stream.y + point[1])
                    if await self._wait_active(application, 1.5):
                        return target["application"]
            except (PortalError, OllamaError, OSError) as e:
                log.info("Focus by click failed: %s", e)
            finally:
                if shot:
                    shot.unlink(missing_ok=True)
        focused, _ = await self.desktop.focused_application()
        raise FocusError(f"Could not bring '{application}' to the front (focused: {focused}). "
                         "Ask the user to click on it, or use activate_element/set_element_text "
                         "which work without focus.")

    async def _wait_active(self, application: str, timeout_s: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            if await self._is_active(application):
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def _is_active(self, application: str) -> bool:
        active = await self.desktop.active_window()
        return bool(active and self.desktop.match_windows([active], application))

    async def assess_input(self, verb: str, target: str | None = None) -> tuple[RiskLevel, str]:
        try:
            app, own = await self.desktop.focused_application()
        except AtspiError:
            app, own = None, False
        where = f"in {app}" if app and not own else "in the focused window"
        context = f"{verb} {where}"
        if app and not own and self.desktop.is_protected(app):
            return RiskLevel.HIGH, f"{context}: protected application"
        keyword = destructive_keyword(target)
        if keyword:
            return RiskLevel.HIGH, f"{context}: target looks high-impact ('{keyword}')"
        return RiskLevel.LOW, context

    async def assess_target(self, verb: str, application: str) -> tuple[RiskLevel, str]:
        """Risk of sending input to a named application (which will be focused first)."""
        context = f"{verb} in {application}"
        try:
            hits = self.desktop.match_windows(await self.desktop.windows(), application)
        except AtspiError:
            hits = []
        app_names = {application} | {w["application"] for w in hits}
        if any(self.desktop.is_protected(a) for a in app_names):
            return RiskLevel.HIGH, f"{context}: protected application"
        return RiskLevel.LOW, context

    async def capture(self) -> Path:
        return await self.portal.screenshot(self.shots_dir)


class FocusError(Exception):
    pass


GLOBAL_TARGET = "desktop"  # press_keys target for system-wide shortcuts (super, alt+tab)
_GLOBAL_MODIFIERS = {"super", "win", "meta", "alt"}


def _fail(tool_call_id: str, error: str) -> ToolResult:
    return ToolResult(tool_call_id=tool_call_id, success=False, error=error)


def _input_error(tool_call_id: str, e: Exception) -> ToolResult:
    hint = ""
    if isinstance(e, PortalError) and "denied" in str(e):
        hint = " The user must allow remote control in the GNOME dialog."
    return _fail(tool_call_id, f"{e}{hint}")


class TypeKeyboardTool(BaseTool):
    name = "type_keyboard"
    description = (
        "Type text with the keyboard into an application's window (it is focused first; "
        "fails rather than typing into the wrong app). Use press_keys for Enter/shortcuts."
    )
    risk_level = RiskLevel.LOW
    parameters = {
        "type": "object",
        "properties": {
            "application": {"type": "string",
                            "description": "App/window that must receive the text."},
            "text": {"type": "string", "description": "Text to type.",
                     "maxLength": MAX_TYPE_CHARS},
        },
        "required": ["application", "text"],
    }

    def __init__(self, ctx: InputContext):
        self.ctx = ctx

    async def assess(self, arguments: dict[str, Any]) -> tuple[RiskLevel, str | None]:
        return await self.ctx.assess_target(
            f"type {len(arguments['text'])} characters", arguments["application"])

    async def execute(self, tool_call_id: str, application: str, text: str) -> ToolResult:
        try:
            app = await self.ctx.focus(application)
            await self.ctx.portal.type_text(text)
        except (PortalError, FocusError, OSError) as e:
            return _input_error(tool_call_id, e)
        return ToolResult(tool_call_id=tool_call_id, success=True,
                          data={"typed_chars": len(text), "focused_application": app})

    async def verify(self, execution_result: ToolResult) -> bool:
        # Synthetic key events have no acknowledgement beyond the portal accepting them.
        return execution_result.success


class PressKeysTool(BaseTool):
    name = "press_keys"
    description = (
        "Press a key or shortcut in an application's window (focused first), e.g. "
        "'enter', 'ctrl+s', 'ctrl+shift+t', 'escape', 'down down enter'. For system "
        "shortcuts like 'super' or 'alt+tab' use application='desktop'."
    )
    risk_level = RiskLevel.LOW
    parameters = {
        "type": "object",
        "properties": {
            "application": {"type": "string",
                            "description": "App/window that must receive the keys, or 'desktop'."},
            "keys": {"type": "string", "description": "Key combination(s)."},
        },
        "required": ["application", "keys"],
    }

    def __init__(self, ctx: InputContext):
        self.ctx = ctx

    async def assess(self, arguments: dict[str, Any]) -> tuple[RiskLevel, str | None]:
        if arguments["application"].strip().lower() == GLOBAL_TARGET:
            return await self.ctx.assess_input(f"press {arguments['keys']}")
        return await self.ctx.assess_target(f"press {arguments['keys']}",
                                            arguments["application"])

    async def execute(self, tool_call_id: str, application: str, keys: str) -> ToolResult:
        combos = [[k.strip().lower() for k in c.split("+")] for c in keys.split() if c]
        if not combos:
            return _fail(tool_call_id, "No keys given.")
        is_global = application.strip().lower() == GLOBAL_TARGET
        if is_global and not all(_GLOBAL_MODIFIERS & set(c) for c in combos):
            return _fail(tool_call_id, "application='desktop' is only for system shortcuts "
                                       "with super/alt (e.g. 'super', 'alt+tab'). Name the "
                                       "app that should receive these keys.")
        try:
            app = await self.ctx.prepare_for_input() if is_global \
                else await self.ctx.focus(application)
            for combo in combos:
                await self.ctx.portal.press_combo(combo)
                await asyncio.sleep(0.05)
        except (PortalError, FocusError, OSError) as e:
            return _input_error(tool_call_id, e)
        return ToolResult(tool_call_id=tool_call_id, success=True,
                          data={"pressed": keys, "focused_application": app})

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


class LookAtScreenTool(BaseTool):
    name = "look_at_screen"
    description = (
        "Take a screenshot and have the vision model describe it or answer a question about "
        "what is visible (e.g. 'Is the video playing?', 'What does the error dialog say?')."
    )
    risk_level = RiskLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "Optional question."}},
    }

    def __init__(self, ctx: InputContext):
        self.ctx = ctx

    async def execute(self, tool_call_id: str, question: str | None = None) -> ToolResult:
        if self.ctx.vision is None:
            return _fail(tool_call_id, "No vision model configured.")
        shot = None
        try:
            await self.ctx.prepare_for_input()
            shot = await self.ctx.capture()
            answer = await self.ctx.vision.describe(shot, question)
        except (PortalError, OllamaError, OSError) as e:
            return _input_error(tool_call_id, e)
        finally:
            if shot:
                shot.unlink(missing_ok=True)
        return ToolResult(tool_call_id=tool_call_id, success=True, data={"description": answer})

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


class ClickOnScreenTool(BaseTool):
    name = "click_on_screen"
    description = (
        "Find a visible UI element by description using the screen and click it with the "
        "mouse, e.g. 'the blue Sign in button', 'the search box at the top', 'the first "
        "video thumbnail'. Use when no accessibility element ref is available."
    )
    risk_level = RiskLevel.LOW
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "What to click, described visually."},
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "double_click": {"type": "boolean"},
        },
        "required": ["target"],
    }

    def __init__(self, ctx: InputContext):
        self.ctx = ctx

    async def assess(self, arguments: dict[str, Any]) -> tuple[RiskLevel, str | None]:
        return await self.ctx.assess_input(f"click '{arguments['target']}'", arguments["target"])

    async def execute(
        self, tool_call_id: str, target: str, button: str = "left", double_click: bool = False
    ) -> ToolResult:
        if self.ctx.vision is None:
            return _fail(tool_call_id, "No vision model configured.")
        shot = None
        try:
            app = await self.ctx.prepare_for_input()
            shot = await self.ctx.capture()
            point = await self.ctx.vision.locate(shot, target)
            if point is None:
                return _fail(tool_call_id, f"Could not find '{target}' on screen. Try "
                                           "look_at_screen to see what is visible.")
            stream = self.ctx.portal.stream
            x, y = stream.x + point[0], stream.y + point[1]
            await self.ctx.portal.click(x, y, button, 2 if double_click else 1)
        except (PortalError, OllamaError, OSError) as e:
            return _input_error(tool_call_id, e)
        finally:
            if shot:
                shot.unlink(missing_ok=True)
        return ToolResult(tool_call_id=tool_call_id, success=True,
                          data={"target": target, "x": round(x), "y": round(y),
                                "focused_application": app})

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


class MouseClickTool(BaseTool):
    name = "mouse_click"
    description = (
        "Click at exact screen coordinates (pixels, origin top-left). Prefer "
        "click_on_screen unless coordinates are already known from a previous result."
    )
    risk_level = RiskLevel.LOW
    parameters = {
        "type": "object",
        "properties": {
            "x": {"type": "number"},
            "y": {"type": "number"},
            "target_description": {"type": "string",
                                   "description": "What is at this position (for safety)."},
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "double_click": {"type": "boolean"},
        },
        "required": ["x", "y", "target_description"],
    }

    def __init__(self, ctx: InputContext):
        self.ctx = ctx

    async def assess(self, arguments: dict[str, Any]) -> tuple[RiskLevel, str | None]:
        desc = arguments.get("target_description", "")
        return await self.ctx.assess_input(
            f"click ({arguments['x']}, {arguments['y']}) '{desc}'", desc
        )

    async def execute(
        self, tool_call_id: str, x: float, y: float, target_description: str,
        button: str = "left", double_click: bool = False,
    ) -> ToolResult:
        try:
            app = await self.ctx.prepare_for_input()
            await self.ctx.portal.click(x, y, button, 2 if double_click else 1)
        except (PortalError, OSError) as e:
            return _input_error(tool_call_id, e)
        return ToolResult(tool_call_id=tool_call_id, success=True,
                          data={"x": x, "y": y, "focused_application": app})

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


class ScrollTool(BaseTool):
    name = "scroll"
    description = "Scroll the window under the mouse pointer up/down/left/right."
    risk_level = RiskLevel.LOW
    parameters = {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "amount": {"type": "integer", "minimum": 1, "maximum": 30,
                       "description": "Wheel steps (default 5)."},
        },
        "required": ["direction"],
    }

    def __init__(self, ctx: InputContext):
        self.ctx = ctx

    async def execute(self, tool_call_id: str, direction: str, amount: int = 5) -> ToolResult:
        dx = {"left": -amount, "right": amount}.get(direction, 0)
        dy = {"up": -amount, "down": amount}.get(direction, 0)
        try:
            await self.ctx.prepare_for_input()
            await self.ctx.portal.scroll(dx, dy)
        except (PortalError, OSError) as e:
            return _input_error(tool_call_id, e)
        return ToolResult(tool_call_id=tool_call_id, success=True,
                          data={"direction": direction, "amount": amount})

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


BROWSER_APPS = ("chrome", "chromium", "firefox", "brave", "edge", "opera", "vivaldi", "epiphany")


class CloseWindowTool(BaseTool):
    name = "close_window"
    description = (
        "Close a window or browser tab by app name or title, e.g. 'YouTube', 'Calculator', "
        "'Text Editor'. Browser tabs are closed as tabs (Ctrl+W), other apps with Alt+F4."
    )
    risk_level = RiskLevel.LOW
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string",
                       "description": "App name or text in the window/tab title."},
        },
        "required": ["target"],
    }

    def __init__(self, ctx: InputContext, browser: Any = None):
        self.ctx = ctx
        self.browser = browser  # BrowserSession: Jarvis's own tabs are closed directly

    async def assess(self, arguments: dict[str, Any]) -> tuple[RiskLevel, str | None]:
        return await self.ctx.assess_target("close", arguments["target"])

    async def _matching(self, target: str) -> list[dict[str, Any]]:
        return self.ctx.desktop.match_windows(await self.ctx.desktop.windows(), target)

    async def execute(self, tool_call_id: str, target: str) -> ToolResult:
        closed: list[str] = []
        if self.browser is not None:
            try:
                closed += [f"Jarvis browser tab: {t}" for t in
                           await self.browser.close_matching(target)]
            except Exception as e:  # noqa: BLE001 - fall through to desktop windows
                log.info("Closing Jarvis browser tabs failed: %s", e)
        try:
            await asyncio.sleep(0.3 if closed else 0)
            windows = await self._matching(target)
        except AtspiError as e:
            return _fail(tool_call_id, f"Cannot list windows: {e}")
        if not windows and not closed:
            return _fail(tool_call_id, f"No open window or tab matches '{target}'.")

        for window in windows[:3]:
            app = window["application"].lower()
            is_browser = any(b in app for b in BROWSER_APPS)
            # App windows: press the window's own Close button via accessibility. No focus
            # or keystrokes needed, so nothing can land in the wrong window.
            if not is_browser and await self._press_close_button(window):
                closed.append(f"{window['application']}: {window['title']}")
                continue
            keys = ["ctrl", "w"] if is_browser else ["alt", "f4"]
            try:
                await self.ctx.focus(window["title"] or window["application"])
                await self.ctx.portal.press_combo(keys)
                await asyncio.sleep(0.8)
            except (PortalError, FocusError, OSError) as e:
                return _input_error(tool_call_id, e) if not closed else ToolResult(
                    tool_call_id=tool_call_id, success=False,
                    error=f"Closed {closed} but not '{window['title']}': {e}",
                    data={"closed": closed})
            closed.append(f"{window['application']}: {window['title']}")
        return ToolResult(tool_call_id=tool_call_id, success=True,
                          data={"target": target, "closed": closed})

    async def _press_close_button(self, window: dict[str, Any]) -> bool:
        client = self.ctx.desktop.client
        try:
            root = Node.from_ref(self.ctx.desktop.resolve_ref(window["ref"]))
            nodes, _ = await client.walk(root, 12, 600)
            for n in nodes:
                if n.role == "push button" and n.name.strip().lower() == "close" and n.actions:
                    if await client.do_action(n.node, 0):
                        await asyncio.sleep(0.6)
                        return True
        except AtspiError as e:
            log.info("Close button not usable for %s: %s", window["title"], e)
        return False

    async def verify(self, execution_result: ToolResult) -> bool:
        """Closed means no window title still matches (a browser may now show another tab)."""
        if not execution_result.success:
            return False
        try:
            return not await self._matching(execution_result.data["target"])
        except AtspiError:
            return False


def build_input_tools(ctx: InputContext, browser: Any = None) -> list[BaseTool]:
    return [
        CloseWindowTool(ctx, browser),
        TypeKeyboardTool(ctx),
        PressKeysTool(ctx),
        LookAtScreenTool(ctx),
        ClickOnScreenTool(ctx),
        MouseClickTool(ctx),
        ScrollTool(ctx),
    ]
