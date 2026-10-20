"""Entry point: `python -m jarvis.main` (GUI) or `python -m jarvis.main --cli [prompt]`."""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.agent.orchestrator import Orchestrator
from jarvis.agent.planner import Planner
from jarvis.agent.tool_registry import ToolRegistry
from jarvis.config import Settings, load_settings
from jarvis.models.ollama_client import OllamaClient
from jarvis.models.schemas import Task, ToolCallRequest
from jarvis.models.vision import VisionModel
from jarvis.security.audit import AuditLogger
from jarvis.security.permissions import SecurityPolicy
from jarvis.storage.database import Database
from jarvis.storage.repository import AuditRepository, TaskRepository
from jarvis.tools.applications import ApplicationIndex, build_application_tools
from jarvis.tools.base import BaseTool
from jarvis.tools.browser import BrowserSession, build_browser_tools
from jarvis.tools.desktop import build_desktop_tools
from jarvis.tools.files import build_file_tools
from jarvis.tools.input import InputContext, build_input_tools
from jarvis.tools.portal import PortalClient
from jarvis.tools.web import build_web_tools


@dataclass
class Runtime:
    settings: Settings
    database: Database
    ollama: OllamaClient
    orchestrator: Orchestrator
    input_ctx: InputContext
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    async def aclose(self) -> None:
        for close in reversed(self.closers):
            try:
                await close()
            except Exception:  # noqa: BLE001 - best-effort shutdown
                logging.getLogger(__name__).debug("close failed", exc_info=True)


def build_runtime(settings: Settings) -> Runtime:
    perms = settings.permissions
    fs = perms.filesystem
    for raw in fs.allowed_paths:  # the scratch workspace is ours to create
        path = Path(raw).expanduser()
        if path.is_relative_to("/tmp") and not path.exists():
            path.mkdir(parents=True, exist_ok=True)

    policy = SecurityPolicy(
        fs.allowed_paths,
        fs.deny_patterns,
        allowed_url_schemes=perms.browser.allowed_schemes,
        blocked_domains=perms.browser.blocked_domains,
        allow_private_network=perms.browser.allow_private_network,
    )
    database = Database(settings.storage.resolved_database_path)
    ollama = OllamaClient(settings.ollama)
    vision = VisionModel(settings.ollama.host, settings.desktop.vision_model)
    browser = BrowserSession(settings.browser, policy)
    desktop_tools, desktop_ctx = build_desktop_tools(perms.desktop.protected_applications)
    portal = PortalClient(settings.desktop.portal_token_file)
    app_index = ApplicationIndex(blocked=perms.applications.blocked_applications)
    input_ctx = InputContext(portal, vision, desktop_ctx, app_index)

    tools = [
        *build_file_tools(policy, fs.trash_dir),
        *build_application_tools(policy, app_index),
        *build_web_tools(policy, browser),
        *build_browser_tools(browser),
        *desktop_tools,
        *build_input_tools(input_ctx, browser),
    ]
    orchestrator = Orchestrator(
        planner=Planner(ollama, fs.allowed_paths),
        registry=ToolRegistry(tools),
        policy=policy,
        audit=AuditLogger(AuditRepository(database)),
        settings=settings.agent,
        task_repository=TaskRepository(database),
    )
    closers = [ollama.aclose, vision.aclose, browser.close, desktop_ctx.client.close,
               portal.close]
    return Runtime(settings, database, ollama, orchestrator, input_ctx, closers)


# ------------------------------------------------------------------------- CLI mode


async def _cli_approver(task: Task, call: ToolCallRequest, tool: BaseTool) -> bool:
    step = next((s for s in task.steps if s.tool_call and s.tool_call.id == call.id), None)
    summary = step.description if step else tool.describe_call(call.arguments)
    print(f"\n[APPROVAL REQUIRED] {summary}")
    print(json.dumps(call.arguments, indent=2))
    try:
        answer = await asyncio.to_thread(input, "Approve? [y/N] ")
    except EOFError:  # no interactive terminal: deny
        print("(no input: denied)")
        return False
    return answer.strip().lower() in ("y", "yes")


def _step_printer():
    seen: dict[tuple[str, int], str] = {}

    def on_update(task: Task) -> None:
        for step in task.steps:
            key = (task.id, step.step_id)
            if seen.get(key) != step.status.value:
                seen[key] = step.status.value
                print(f"  [{step.status.value:<17}] {step.description}")

    return on_update


async def run_cli(runtime: Runtime, prompt: str | None) -> int:
    orch = runtime.orchestrator
    orch.approver = _cli_approver
    orch.on_task_update = _step_printer()
    try:
        prompts = [prompt] if prompt else None
        while True:
            if prompts is not None:
                if not prompts:
                    break
                text = prompts.pop()
            else:
                try:
                    text = (await asyncio.to_thread(input, "\nyou> ")).strip()
                except EOFError:
                    break
                if text in ("exit", "quit"):
                    break
                if not text:
                    continue
            task = await orch.run(text)
            print(f"jarvis [{task.status.value}]> {task.final_response}")
            if prompt and task.status.value != "COMPLETED":
                return 1
    finally:
        await runtime.aclose()
    return 0


# ------------------------------------------------------------------------- GUI mode


def run_gui(runtime: Runtime, wake_audio: str | None = None) -> int:
    from jarvis.ipc import send_to_gui

    # Already running: bring that window up (and pass on any wake audio) instead.
    if send_to_gui({"action": "wake", "audio": wake_audio}):
        return 0

    from jarvis.ui.window_activation import configure_platform

    configure_platform(runtime.settings.ui.prefer_x11)  # must precede QApplication

    from PySide6.QtWidgets import QApplication

    from jarvis.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Jarvis")
    # Quit cleanly on SIGTERM/SIGINT. Python only runs signal handlers between bytecodes,
    # so a periodic no-op timer gives it the chance while Qt's C++ event loop runs.
    import signal

    from PySide6.QtCore import QTimer

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: app.closeAllWindows())
    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)
    keepalive.start(500)
    window = MainWindow(runtime, wake_audio)
    window.show()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Local desktop AI agent")
    parser.add_argument("--config", help="Path to settings.toml")
    parser.add_argument("--cli", action="store_true", help="Run in the terminal instead of the GUI")
    parser.add_argument("prompt", nargs="?", help="One-shot prompt (CLI mode only)")
    parser.add_argument("--wake-audio", help=argparse.SUPPRESS)  # set by jarvis.daemon
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    runtime = build_runtime(load_settings(args.config))
    try:
        if args.cli or args.prompt:
            return asyncio.run(run_cli(runtime, args.prompt))
        return run_gui(runtime, args.wake_audio)
    finally:
        runtime.database.close()


if __name__ == "__main__":
    sys.exit(main())
