"""Application discovery (freedesktop .desktop entries) and launching."""

import asyncio
import configparser
import os
import shlex
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from jarvis.models.schemas import RiskLevel, ToolResult
from jarvis.security.permissions import SecurityPolicy, normalize_path
from jarvis.tools.base import BaseTool

MAX_LIST = 60


@dataclass(frozen=True)
class DesktopEntry:
    id: str  # desktop-file ID, e.g. "org.gnome.TextEditor.desktop"
    name: str
    path: Path
    exec: str
    generic_name: str = ""
    comment: str = ""
    keywords: str = ""
    icon: str = ""  # theme icon name or absolute path (Icon= key)

    @property
    def binary(self) -> str:
        """Basename of the executable in Exec (after `env VAR=x` prefixes)."""
        try:
            parts = shlex.split(self.exec)
        except ValueError:
            parts = self.exec.split()
        if parts and parts[0] == "env":
            parts = [p for p in parts[1:] if "=" not in p]
        return os.path.basename(parts[0]) if parts else ""

    def search_text(self) -> str:
        return " ".join(
            (self.id, self.name, self.generic_name, self.comment, self.keywords)
        ).lower()


def application_dirs() -> list[Path]:
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    dirs = [data_home, *data_dirs.split(":")]
    return [Path(d) / "applications" for d in dirs if d]


def parse_desktop_file(path: Path, desktop_id: str) -> DesktopEntry | None:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # keys are case-sensitive
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError, OSError):
        return None
    if not parser.has_section("Desktop Entry"):
        return None
    sec = parser["Desktop Entry"]
    if sec.get("Type") != "Application" or not sec.get("Exec") or not sec.get("Name"):
        return None
    if any(sec.get(key, "false").lower() == "true" for key in ("NoDisplay", "Hidden")):
        return None
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")
    only = [d for d in sec.get("OnlyShowIn", "").split(";") if d]
    not_in = [d for d in sec.get("NotShowIn", "").split(";") if d]
    if (only and not set(only) & set(desktop)) or set(not_in) & set(desktop):
        return None
    return DesktopEntry(
        id=desktop_id,
        name=sec["Name"],
        path=path,
        exec=sec["Exec"],
        generic_name=sec.get("GenericName", ""),
        comment=sec.get("Comment", ""),
        keywords=sec.get("Keywords", ""),
        icon=sec.get("Icon", ""),
    )


class ApplicationIndex:
    def __init__(self, dirs: list[Path] | None = None, blocked: list[str] | None = None):
        self.dirs = dirs
        self.blocked = {b.lower() for b in blocked or []}
        self._entries: dict[str, DesktopEntry] | None = None

    def entries(self, refresh: bool = False) -> list[DesktopEntry]:
        if self._entries is None or refresh:
            found: dict[str, DesktopEntry] = {}
            for base in self.dirs or application_dirs():
                if not base.is_dir():
                    continue
                for path in sorted(base.rglob("*.desktop")):
                    # Desktop-file ID: path relative to the dir, '/' replaced by '-'.
                    desktop_id = str(path.relative_to(base)).replace("/", "-")
                    if desktop_id in found:  # earlier dirs take precedence
                        continue
                    entry = parse_desktop_file(path, desktop_id)
                    if entry:
                        found[desktop_id] = entry
            self._entries = found
        return sorted(self._entries.values(), key=lambda e: e.name.lower())

    def display_name(self, app: str) -> str | None:
        """Launcher name for an AT-SPI application name ('gnome-calculator' -> 'Calculator')."""
        a = app.lower()
        for e in self.entries():
            stem = e.id.lower().removesuffix(".desktop")
            if a in (e.binary.lower(), stem, e.name.lower()) or stem.endswith("." + a):
                return e.name
        return None

    def is_blocked(self, entry: DesktopEntry) -> bool:
        stem = entry.id.removesuffix(".desktop").lower()
        return bool({entry.id.lower(), stem, entry.name.lower()} & self.blocked)

    def search(self, query: str | None) -> list[DesktopEntry]:
        entries = [e for e in self.entries() if not self.is_blocked(e)]
        if not query:
            return entries
        terms = query.lower().split()
        return [e for e in entries if all(t in e.search_text() for t in terms)]

    def resolve(self, name: str) -> DesktopEntry | list[DesktopEntry] | None:
        """Exact ID/name match, else a unique fuzzy match. Returns candidates if ambiguous."""
        q = name.strip().lower()
        entries = self.entries()
        for e in entries:
            if q in (e.id.lower(), e.id.lower().removesuffix(".desktop"), e.name.lower()):
                return e
        matches = [e for e in entries if q in e.name.lower()] or self.search(name)
        if len(matches) == 1:
            return matches[0]
        return matches or None


# Launcher returns (returncode, stderr). Injected for tests.
Launcher = Callable[[list[str]], Awaitable[tuple[int, str]]]


async def spawn_detached(argv: list[str]) -> tuple[int, str]:
    """Run a launcher command (gio launch/open return once the app is spawned).

    stderr goes to a temp file, not a pipe: the launched app inherits it, and a pipe would
    keep us waiting until the app exits.
    """
    with tempfile.TemporaryFile() as err:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=err,
            start_new_session=True,
        )
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=15)
        except TimeoutError:
            return 0, ""  # launcher still running in the foreground: the app started
        err.seek(0)
        return code or 0, err.read().decode(errors="replace").strip()


def running_process_names() -> set[str]:
    names: set[str] = set()
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            names.add((pid_dir / "comm").read_text().strip())
            cmdline = (pid_dir / "cmdline").read_bytes().split(b"\0")
            for part in cmdline:  # all args, so `flatpak run <app-id>` matches by app id
                if part and not part.startswith(b"-"):
                    names.add(os.path.basename(part.decode(errors="replace")))
        except OSError:
            continue
    return names


class ListApplicationsTool(BaseTool):
    name = "list_applications"
    description = (
        "List installed desktop applications, optionally filtered by a search query "
        "(matches name, description and keywords, e.g. 'text editor' or 'browser')."
    )
    risk_level = RiskLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Optional search words."}},
    }

    def __init__(self, index: ApplicationIndex):
        self.index = index

    async def execute(self, tool_call_id: str, query: str | None = None) -> ToolResult:
        entries = await asyncio.to_thread(self.index.search, query)
        apps = [{"id": e.id, "name": e.name, "description": e.comment or e.generic_name}
                for e in entries[:MAX_LIST]]
        return ToolResult(
            tool_call_id=tool_call_id,
            success=True,
            data={"applications": apps, "total": len(entries),
                  "truncated": len(entries) > MAX_LIST},
        )

    async def verify(self, execution_result: ToolResult) -> bool:
        return execution_result.success


class LaunchApplicationTool(BaseTool):
    name = "launch_application"
    description = (
        "Launch an installed desktop application by name or desktop-file ID "
        "(e.g. 'Text Editor', 'firefox', 'org.gnome.Calculator')."
    )
    risk_level = RiskLevel.LOW
    parameters = {
        "type": "object",
        "properties": {
            "application": {"type": "string", "description": "Application name or ID."},
        },
        "required": ["application"],
    }

    def __init__(
        self,
        index: ApplicationIndex,
        launcher: Launcher = spawn_detached,
        process_names: Callable[[], set[str]] = running_process_names,
        verify_timeout_s: float = 8.0,
    ):
        self.index = index
        self.launcher = launcher
        self.process_names = process_names
        self.verify_timeout_s = verify_timeout_s

    async def execute(self, tool_call_id: str, application: str) -> ToolResult:
        resolved = await asyncio.to_thread(self.index.resolve, application)
        if resolved is None:
            return ToolResult(tool_call_id=tool_call_id, success=False,
                              error=f"No installed application matches '{application}'. "
                                    "Use list_applications to search.")
        if isinstance(resolved, list):
            names = ", ".join(f"{e.name} ({e.id})" for e in resolved[:10])
            return ToolResult(tool_call_id=tool_call_id, success=False,
                              error=f"'{application}' is ambiguous. Candidates: {names}")
        entry = resolved
        if self.index.is_blocked(entry):
            return ToolResult(tool_call_id=tool_call_id, success=False,
                              error=f"Launching {entry.name} is blocked by policy.")
        code, stderr = await self.launcher(["gio", "launch", str(entry.path)])
        if code != 0:
            return ToolResult(tool_call_id=tool_call_id, success=False,
                              error=f"Failed to launch {entry.name}: {stderr or f'exit {code}'}")
        return ToolResult(
            tool_call_id=tool_call_id,
            success=True,
            data={"id": entry.id, "name": entry.name, "binary": entry.binary,
                  "launched_at": time.time()},
        )

    async def verify(self, execution_result: ToolResult) -> bool:
        """Poll for a running process matching the app's executable or ID."""
        if not execution_result.success or not execution_result.data:
            return False
        data = execution_result.data
        wanted = {data["binary"], data["id"].removesuffix(".desktop")}
        wanted.discard("")
        if "flatpak" in wanted:  # flatpak apps run as bwrap; match on the app id instead
            wanted.discard("flatpak")
        deadline = time.monotonic() + self.verify_timeout_s
        while True:
            names = await asyncio.to_thread(self.process_names)
            if wanted & names:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)


class OpenFileTool(BaseTool):
    name = "open_file"
    description = "Open a file or folder with its default application (like double-clicking it)."
    risk_level = RiskLevel.LOW
    path_arguments = ("path",)
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File or folder to open."}},
        "required": ["path"],
    }

    def __init__(self, policy: SecurityPolicy, launcher: Launcher = spawn_detached):
        self.policy = policy
        self.launcher = launcher

    async def execute(self, tool_call_id: str, path: str) -> ToolResult:
        resolved = normalize_path(path)
        if not self.policy.is_path_allowed(resolved):
            return ToolResult(tool_call_id=tool_call_id, success=False,
                              error=f"Access denied to path: {resolved}")
        if not resolved.exists():
            return ToolResult(tool_call_id=tool_call_id, success=False,
                              error=f"No such file or directory: {resolved}")
        code, stderr = await self.launcher(["gio", "open", str(resolved)])
        if code != 0:
            return ToolResult(tool_call_id=tool_call_id, success=False,
                              error=f"Could not open {resolved}: {stderr or f'exit {code}'}")
        return ToolResult(tool_call_id=tool_call_id, success=True, data={"path": str(resolved)})

    async def verify(self, execution_result: ToolResult) -> bool:
        # gio returns non-zero when no handler exists, so success already means a handler
        # accepted the file; there is no generic way to observe the opened window.
        return execution_result.success


def build_application_tools(
    policy: SecurityPolicy, index: ApplicationIndex
) -> list[BaseTool]:
    return [ListApplicationsTool(index), LaunchApplicationTool(index), OpenFileTool(policy)]

