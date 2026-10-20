from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage

from jarvis.models.schemas import RiskLevel
from jarvis.models.vision import parse_bbox, prepare_image
from jarvis.security.risk import destructive_keyword
from jarvis.tools.atspi import AtspiError, Node
from jarvis.tools.desktop import DesktopContext
from jarvis.tools.input import (
    ClickOnScreenTool,
    InputContext,
    PressKeysTool,
    TypeKeyboardTool,
)
from jarvis.tools.portal import KEYSYMS, PortalError, Stream, char_keysym, key_keysym

# --- keysyms ------------------------------------------------------------------------------


def test_keysyms():
    assert char_keysym("a") == ord("a") and char_keysym("A") == ord("A")
    assert char_keysym("\n") == KEYSYMS["enter"]
    assert char_keysym("é") == 0x01000000 + ord("é")
    assert key_keysym("Ctrl") == KEYSYMS["ctrl"] and key_keysym("F5") == 0xFFC2
    assert key_keysym("L") == ord("l")
    with pytest.raises(PortalError):
        key_keysym("hyperdrive")


# --- vision helpers -------------------------------------------------------------------------


@pytest.mark.parametrize("reply,expected", [
    ('```json\n[{"bbox_2d": [10, 20, 30, 40], "label": "OK"}]\n```', (10, 20, 30, 40)),
    ('{"bbox_2d": [1, 2, 3, 4]}', (1, 2, 3, 4)),
    ('[{"point_2d": [5, 6]}]', (5, 6, 5, 6)),
    ("[]", None),
    ("I can't see it", None),
])
def test_parse_bbox(reply, expected):
    assert parse_bbox(reply) == expected


def test_prepare_image_maps_back_to_source(tmp_path: Path):
    img = QImage(1920, 1080, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    path = tmp_path / "s.png"
    img.save(str(path))
    prepared = prepare_image(path)
    assert prepared.width % 28 == 0 and prepared.height % 28 == 0
    assert prepared.width <= 1288
    x, y = prepared.to_source(prepared.width / 2, prepared.height / 2)
    assert abs(x - 960) < 1 and abs(y - 540) < 1


def test_destructive_keyword():
    assert destructive_keyword("Place order") in ("place order", "order")
    assert destructive_keyword("Delete forever") == "delete"
    assert destructive_keyword("Send") == "send"
    assert destructive_keyword("Search") is None and destructive_keyword(None) is None


# --- input tools with fakes ---------------------------------------------------------------------


class FakeAtspiFocus:
    def __init__(self, desktop):
        self.desktop = desktop

    async def grab_focus(self, node):
        if self.desktop.grab_works:
            self.desktop.focus_app(self.desktop.window_app(node.path))
        return True

    async def walk(self, root, max_depth, max_nodes):
        """Window tree: a Close button only for apps listed in desktop.close_buttons."""
        from jarvis.tools.atspi import Node, NodeInfo

        app = self.desktop.window_app(root.path)
        if app not in self.desktop.close_buttons:
            return [], False
        return [NodeInfo(Node(":1.1", f"/close/{app}"), "push button", "Close",
                         actions=["Click"])], False

    async def do_action(self, node, index):
        del self.desktop.apps[node.path.rsplit("/", 1)[-1]]
        return True


class FakeDesktop(DesktopContext):
    """Windows: {app_name: active}. Jarvis's own window is 'python' when own=True."""

    def __init__(self, app: str | None, own: bool = False, others: tuple[str, ...] = (),
                 grab_works: bool = False):
        super().__init__(client=None, protected_apps=["org.gnome.Terminal"])
        self.client = FakeAtspiFocus(self)
        self.grab_works = grab_works
        self.close_buttons: set[str] = set()
        self.apps = {name: False for name in others}
        if app:
            self.apps[app] = not own
        self.own = own

    def window_app(self, path):
        return path.rsplit("/", 1)[-1]

    def focus_app(self, app):
        for name in self.apps:
            self.apps[name] = name == app
        self.own = False

    async def windows(self):
        return [{"ref": f":1.1/w/{name}", "application": name, "title": name.title(),
                 "active": active} for name, active in self.apps.items()]

    async def focused_application(self):
        if self.own:
            return "python", True
        active = [n for n, a in self.apps.items() if a]
        return (active[0] if active else None), False


class FakePortal:
    def __init__(self):
        self.events: list = []
        self.stream = Stream(1, 0, 0, 1920, 1080)

    async def type_text(self, text):
        self.events.append(("type", text))

    async def press_combo(self, keys):
        self.events.append(("combo", keys))

    async def click(self, x, y, button="left", count=1):
        self.events.append(("click", round(x), round(y), button, count))

    async def screenshot(self, out_dir):
        path = Path(out_dir) / "shot.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        return path


class FakeVision:
    def __init__(self, point):
        self.point = point
        self.targets: list[str] = []

    async def locate(self, screenshot, target):
        assert screenshot.exists()
        self.targets.append(target)
        return self.point


def make_ctx(app="gedit", own=False, point=(100.0, 200.0), tmp_path: Path | None = None,
             **desktop_kwargs):
    ctx = InputContext(FakePortal(), FakeVision(point), FakeDesktop(app, own, **desktop_kwargs))
    if tmp_path:
        ctx.shots_dir = tmp_path
    return ctx


async def test_typing_risk_depends_on_target_app():
    tool = TypeKeyboardTool(make_ctx("gedit"))
    risk, context = await tool.assess({"application": "gedit", "text": "hello"})
    assert risk == RiskLevel.LOW and "gedit" in context

    tool = TypeKeyboardTool(make_ctx("org.gnome.Terminal"))
    risk, context = await tool.assess({"application": "terminal", "text": "rm -rf ~"})
    assert risk == RiskLevel.HIGH and "protected" in context


async def test_type_and_press_send_portal_events():
    ctx = make_ctx()
    assert (await TypeKeyboardTool(ctx).execute("c", application="gedit", text="hi")).success
    assert (await PressKeysTool(ctx).execute("c", application="gedit",
                                             keys="ctrl+s down enter")).success
    assert ctx.portal.events == [("type", "hi"), ("combo", ["ctrl", "s"]),
                                 ("combo", ["down"]), ("combo", ["enter"])]


async def test_refuses_to_type_into_wrong_app(tmp_path: Path):
    # Chrome is focused; the target calculator can't be focused (vision finds nothing).
    ctx = make_ctx("chrome", others=("calculator",), point=None, tmp_path=tmp_path)
    result = await TypeKeyboardTool(ctx).execute("c", application="calculator", text="12*7=")
    assert not result.success and "Could not bring" in result.error
    assert ctx.portal.events == []


async def test_focuses_target_via_atspi_then_types():
    ctx = make_ctx("chrome", others=("calculator",), grab_works=True)
    result = await TypeKeyboardTool(ctx).execute("c", application="calculator", text="1")
    assert result.success and result.data["focused_application"] == "calculator"
    assert ctx.portal.events == [("type", "1")]


async def test_focuses_target_by_clicking_title_bar(tmp_path: Path):
    ctx = make_ctx("chrome", others=("calculator",), tmp_path=tmp_path)
    original_click = ctx.portal.click

    async def click_focuses(x, y, button="left", count=1):
        await original_click(x, y, button, count)
        ctx.desktop.focus_app("calculator")

    ctx.portal.click = click_focuses
    result = await PressKeysTool(ctx).execute("c", application="calculator", keys="enter")
    assert result.success
    assert ctx.portal.events == [("click", 100, 200, "left", 1), ("combo", ["enter"])]
    assert "title bar" in ctx.vision.targets[0]


async def test_desktop_target_only_for_system_shortcuts():
    ctx = make_ctx()
    assert (await PressKeysTool(ctx).execute("c", application="desktop", keys="super")).success
    bad = await PressKeysTool(ctx).execute("c", application="desktop", keys="1 2")
    assert not bad.success and ctx.portal.events == [("combo", ["super"])]


async def test_input_yields_focus_when_jarvis_is_focused():
    ctx = make_ctx(own=True)
    yielded = []

    async def yield_focus():
        yielded.append(True)
        ctx.desktop.focus_app("gedit")

    ctx.yield_focus = yield_focus
    result = await TypeKeyboardTool(ctx).execute("c", application="gedit", text="x")
    assert yielded and result.data["focused_application"] == "gedit"


async def test_click_on_screen(tmp_path: Path):
    ctx = make_ctx(point=(100.4, 200.6), tmp_path=tmp_path)
    tool = ClickOnScreenTool(ctx)
    result = await tool.execute("c", target="the OK button")
    assert result.success and ctx.portal.events == [("click", 100, 201, "left", 1)]
    assert list(tmp_path.iterdir()) == []  # screenshot cleaned up

    risk, _ = await tool.assess({"target": "the Delete account button"})
    assert risk == RiskLevel.HIGH


async def test_click_on_screen_not_found(tmp_path: Path):
    ctx = make_ctx(point=None, tmp_path=tmp_path)
    result = await ClickOnScreenTool(ctx).execute("c", target="unicorn")
    assert not result.success and "Could not find" in result.error
    assert ctx.portal.events == []


# --- desktop context ----------------------------------------------------------------------------


class FakeAtspi:
    def __init__(self, app_name: str, element_name: str):
        self.app_name, self.element_name = app_name, element_name

    async def application_of(self, node):
        return Node(node.bus, "/app")

    async def name(self, node):
        return self.app_name

    async def info(self, node, with_text=True):
        from jarvis.tools.atspi import NodeInfo
        return NodeInfo(node, "push button", self.element_name)


class PidDesktop(DesktopContext):
    def __init__(self, client, pid):
        super().__init__(client, ["org.gnome.Terminal"], own_pid=1000)
        self.pid = pid

    async def pid_of(self, bus_name):
        return self.pid


async def test_desktop_assessment():
    ref = ":1.5/org/a11y/atspi/accessible/7"
    ctx = PidDesktop(FakeAtspi("gedit", "Save"), pid=2000)
    assert (await ctx.assess_element(ref, "activate"))[0] == RiskLevel.LOW

    ctx = PidDesktop(FakeAtspi("gedit", "Delete file"), pid=2000)
    assert (await ctx.assess_element(ref, "activate"))[0] == RiskLevel.HIGH

    ctx = PidDesktop(FakeAtspi("org.gnome.Terminal", "Copy"), pid=2000)
    assert (await ctx.assess_element(ref, "activate"))[0] == RiskLevel.HIGH


async def test_desktop_refuses_jarvis_own_windows():
    ctx = PidDesktop(FakeAtspi("python", "Approve"), pid=1000)
    with pytest.raises(AtspiError, match="own windows"):
        await ctx.check_target(":1.9/org/a11y/atspi/accessible/3")


def test_node_ref_roundtrip():
    node = Node.from_ref(":1.42/org/a11y/atspi/accessible/17")
    assert node == Node(":1.42", "/org/a11y/atspi/accessible/17")
    with pytest.raises(AtspiError):
        Node.from_ref("garbage")


# --- close_window -----------------------------------------------------------------------


def closing_ctx(**kwargs):
    from jarvis.tools.input import CloseWindowTool

    ctx = make_ctx(grab_works=True, **kwargs)
    original = ctx.portal.press_combo

    async def press_and_close(keys):
        await original(keys)
        focused = [n for n, a in ctx.desktop.apps.items() if a]
        for name in focused:
            del ctx.desktop.apps[name]

    ctx.portal.press_combo = press_and_close
    return ctx, CloseWindowTool


async def test_close_window_uses_alt_f4_for_apps_and_ctrl_w_for_browsers():
    ctx, tool_cls = closing_ctx(app="gedit", others=("calculator", "google chrome"))
    tool = tool_cls(ctx)
    result = await tool.execute("c", target="calculator")
    assert result.success and await tool.verify(result)
    assert ctx.portal.events == [("combo", ["alt", "f4"])]

    result = await tool.execute("c", target="chrome")
    assert result.success and ctx.portal.events[-1] == ("combo", ["ctrl", "w"])
    assert "gedit" in ctx.desktop.apps  # untouched


async def test_close_window_closes_jarvis_browser_tabs_directly():
    class FakeBrowser:
        async def close_matching(self, target):
            return ["YouTube - tesla"] if target.lower() == "youtube" else []

    ctx, tool_cls = closing_ctx(app="gedit")
    result = await tool_cls(ctx, FakeBrowser()).execute("c", target="YouTube")
    assert result.success and result.data["closed"] == ["Jarvis browser tab: YouTube - tesla"]
    assert ctx.portal.events == []  # no keystrokes needed


async def test_close_window_nothing_matches():
    ctx, tool_cls = closing_ctx(app="gedit")
    result = await tool_cls(ctx).execute("c", target="YouTube")
    assert not result.success and "No open window" in result.error and ctx.portal.events == []



async def test_close_window_presses_the_windows_own_close_button_without_keys():
    ctx, tool_cls = closing_ctx(app="chrome", others=("calculator",))
    ctx.desktop.close_buttons.add("calculator")
    tool = tool_cls(ctx)
    result = await tool.execute("c", target="calculator")
    assert result.success and await tool.verify(result)
    assert ctx.portal.events == []  # no focus change, no keystrokes
    assert ctx.desktop.apps == {"chrome": True}


async def test_focus_via_gnome_overview_for_hidden_windows():
    """Super, type the launcher name, Enter: works for windows behind others."""

    class Index:
        def display_name(self, app):
            return {"calculator": "Calculator"}.get(app)

    ctx = make_ctx("chrome", others=("calculator",), point=None)
    ctx.app_index = Index()
    original = ctx.portal.press_combo

    async def press(keys):
        await original(keys)
        if keys == ["enter"]:
            ctx.desktop.focus_app("calculator")

    ctx.portal.press_combo = press
    result = await TypeKeyboardTool(ctx).execute("c", application="calculator", text="7")
    assert result.success and result.data["focused_application"] == "calculator"
    assert ctx.portal.events == [("combo", ["super"]), ("type", "Calculator"),
                                 ("combo", ["enter"]), ("type", "7")]
