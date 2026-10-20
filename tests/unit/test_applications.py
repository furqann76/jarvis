from pathlib import Path

import pytest

from jarvis.security.permissions import SecurityPolicy
from jarvis.tools.applications import (
    ApplicationIndex,
    LaunchApplicationTool,
    ListApplicationsTool,
    OpenFileTool,
    parse_desktop_file,
)


def write_entry(directory: Path, filename: str, body: str) -> Path:
    path = directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Desktop Entry]\nType=Application\n" + body)
    return path


@pytest.fixture
def apps_dir(tmp_path: Path) -> Path:
    d = tmp_path / "applications"
    write_entry(d, "org.gnome.Calculator.desktop",
                "Name=Calculator\nExec=gnome-calculator\nKeywords=math;arithmetic;\n")
    write_entry(d, "firefox.desktop",
                "Name=Firefox Web Browser\nGenericName=Web Browser\nExec=env MOZ=1 firefox %u\n")
    write_entry(d, "chromium.desktop", "Name=Chromium Web Browser\nExec=/usr/bin/chromium %U\n")
    write_entry(d, "hidden.desktop", "Name=Hidden\nExec=hidden\nNoDisplay=true\n")
    write_entry(d, "kde/konsole.desktop", "Name=Konsole\nExec=konsole\n")
    write_entry(d, "notapp.desktop", "Name=Link\nExec=x\n".replace("", "", 0))
    (d / "notapp.desktop").write_text("[Desktop Entry]\nType=Link\nName=Link\nURL=x\n")
    return d


def test_parse_and_binary(apps_dir: Path):
    entry = parse_desktop_file(apps_dir / "firefox.desktop", "firefox.desktop")
    assert entry.name == "Firefox Web Browser" and entry.binary == "firefox"
    assert parse_desktop_file(apps_dir / "hidden.desktop", "hidden.desktop") is None
    assert parse_desktop_file(apps_dir / "notapp.desktop", "notapp.desktop") is None


def test_index_ids_search_and_resolve(apps_dir: Path):
    index = ApplicationIndex([apps_dir])
    ids = {e.id for e in index.entries()}
    assert ids == {"org.gnome.Calculator.desktop", "firefox.desktop", "chromium.desktop",
                   "kde-konsole.desktop"}
    assert [e.name for e in index.search("math")] == ["Calculator"]
    assert index.resolve("calculator").id == "org.gnome.Calculator.desktop"
    assert index.resolve("org.gnome.Calculator").name == "Calculator"
    assert index.resolve("firefox").binary == "firefox"
    ambiguous = index.resolve("web browser")
    assert isinstance(ambiguous, list) and len(ambiguous) == 2
    assert index.resolve("photoshop") is None


def test_blocked_applications(apps_dir: Path):
    index = ApplicationIndex([apps_dir], blocked=["konsole"])
    assert "Konsole" not in [e.name for e in index.search(None)]


async def test_list_applications(apps_dir: Path):
    result = await ListApplicationsTool(ApplicationIndex([apps_dir])).execute("c", query="browser")
    assert result.success and result.data["total"] == 2


async def test_launch_and_verify(apps_dir: Path):
    launched = []

    async def launcher(argv):
        launched.append(argv)
        return 0, ""

    tool = LaunchApplicationTool(ApplicationIndex([apps_dir]), launcher=launcher,
                                 process_names=lambda: {"bash", "gnome-calculator"},
                                 verify_timeout_s=0)
    result = await tool.execute("c", application="Calculator")
    assert result.success
    assert launched == [["gio", "launch", str(apps_dir / "org.gnome.Calculator.desktop")]]
    assert await tool.verify(result)

    tool.process_names = lambda: {"bash"}
    assert not await tool.verify(result)


async def test_launch_errors(apps_dir: Path):
    async def failing(argv):
        return 1, "boom"

    index = ApplicationIndex([apps_dir], blocked=["chromium"])
    tool = LaunchApplicationTool(index, launcher=failing)
    assert "ambiguous" in (await tool.execute("c", application="web browser")).error
    assert "No installed" in (await tool.execute("c", application="photoshop")).error
    assert "blocked" in (await tool.execute("c", application="chromium")).error
    assert "boom" in (await tool.execute("c", application="calculator")).error


async def test_open_file_respects_sandbox(sandbox: Path, policy: SecurityPolicy, tmp_path: Path):
    opened = []

    async def launcher(argv):
        opened.append(argv)
        return 0, ""

    (sandbox / "doc.txt").write_text("x")
    tool = OpenFileTool(policy, launcher)
    assert (await tool.execute("c", path=str(sandbox / "doc.txt"))).success
    denied = await tool.execute("c", path=str(tmp_path / "outside"))
    assert not denied.success and len(opened) == 1
