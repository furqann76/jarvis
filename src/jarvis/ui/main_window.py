import asyncio
import concurrent.futures
import json
import logging
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QScrollArea,
    QSizeGrip,
    QStackedWidget,
    QWidget,
)

from jarvis.config import find_config_file
from jarvis.ipc import gui_socket_path, speaking_flag
from jarvis.models.ollama_client import OllamaError
from jarvis.models.schemas import RiskLevel, Task, TaskStatus, ToolCallRequest
from jarvis.tools.base import BaseTool
from jarvis.ui.components.async_runner import AsyncRunner
from jarvis.ui.dashboard import theme
from jarvis.ui.dashboard.chrome import FooterStatus, Header, Sidebar
from jarvis.ui.dashboard.home import TOOL_LABELS, HomePage
from jarvis.ui.dashboard.pages import (
    ApplicationsPage,
    BrowserPage,
    FileManagerPage,
    HistoryPage,
    SettingsPage,
)
from jarvis.ui.dashboard.right_panel import RightPanel, detect_system_info
from jarvis.ui.dashboard.widgets import IDLE, LISTENING, SPEAKING, THINKING
from jarvis.ui.window_activation import bring_to_front
from jarvis.voice.listener import HandsFreeListener
from jarvis.voice.microphone import Microphone, MicrophoneError, load_wav
from jarvis.voice.stt import SpeechToText
from jarvis.voice.tts import TextToSpeech
from jarvis.voice.wake import is_stop_command, parse_wake

if TYPE_CHECKING:
    from jarvis.main import Runtime

log = logging.getLogger(__name__)
FOLLOW_UP_WINDOW_S = 8.0  # after a bare "Jarvis", the next utterance is the command


@dataclass
class ApprovalRequest:
    task: Task
    call: ToolCallRequest
    tool: BaseTool
    future: concurrent.futures.Future


class AgentBridge(QObject):
    """Lives on the Qt thread; its signals are emitted from worker threads."""

    task_updated = Signal(object)  # Task snapshot
    task_finished = Signal(object)  # Task, or None when the run is over
    approval_requested = Signal(object)  # ApprovalRequest
    status_message = Signal(str, str)  # text, colour
    voice_command = Signal(str)  # transcribed command to run
    voice_heard = Signal(str)  # transcript shown as a notice (not a command)
    minimize_requested = Signal(object)  # concurrent.futures.Future
    cancel_requested = Signal()
    follow_up_requested = Signal()  # listen briefly for a command after a bare "Jarvis"
    llm_status = Signal(bool, str)
    system_info = Signal(object)  # list of (icon, label, value)
    speaking = Signal(bool)


class MainWindow(QMainWindow):
    def __init__(self, runtime: "Runtime", wake_audio: str | None = None):
        super().__init__()
        self.setWindowTitle("Jarvis")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.resize(1360, 900)
        self.setMinimumSize(1180, 680)

        self.runtime = runtime
        self.orchestrator = runtime.orchestrator
        self.ollama = runtime.ollama
        voice = runtime.settings.voice
        self.voice_settings = voice
        self.runner = AsyncRunner()
        self.bridge = AgentBridge()
        self._current: concurrent.futures.Future | None = None
        self._current_prompt = ""
        self._notified_steps: set[tuple[str, int]] = set()
        self._approvals: list[ApprovalRequest] = []

        self.stt = SpeechToText(voice)
        self.tts = TextToSpeech(voice)
        self.mic = Microphone(voice.max_record_seconds)
        self.listener = HandsFreeListener(self._on_utterance)
        self._awaiting_command_until = 0.0

        # Orchestrator / tool callbacks -> Qt signals (thread-safe, queued delivery).
        self.orchestrator.on_task_update = self.bridge.task_updated.emit
        self.orchestrator.approver = self._request_approval
        runtime.input_ctx.yield_focus = self._yield_focus

        self._build_view(runtime, voice)

        self.command.submitted.connect(self._on_submit)
        self.command.cancel_requested.connect(self._on_cancel)
        self.command.mic_toggled.connect(self._on_mic_toggled)
        self.hands_free.toggled.connect(self._on_hands_free_toggled)
        self.speak.toggled.connect(lambda on: None if on else self.tts.stop())
        self.bridge.task_updated.connect(self._on_task_updated)
        self.bridge.task_finished.connect(self._on_task_finished)
        self.bridge.approval_requested.connect(self._on_approval_requested)
        self.bridge.status_message.connect(self.footer.set)
        self.bridge.voice_command.connect(self._on_voice_command)
        self.bridge.voice_heard.connect(self.activity.add_notice)
        self.bridge.minimize_requested.connect(self._on_minimize_requested)
        self.bridge.cancel_requested.connect(self._on_cancel)
        self.bridge.follow_up_requested.connect(self._listen_for_follow_up)
        self.bridge.llm_status.connect(self.header.set_llm_status)
        self.bridge.system_info.connect(self.right.system.set_rows)
        self.bridge.speaking.connect(self._on_speaking)
        self.right.approvals.decided.connect(self._on_approval_decided)
        QShortcut(QKeySequence("Ctrl+Space"), self, activated=self.command.toggle_mic)

        # Single instance: the wake-word daemon (and a second launch) talk to this window.
        self.ipc = QLocalServer(self)
        socket_path = str(gui_socket_path())
        QLocalServer.removeServer(socket_path)
        if self.ipc.listen(socket_path):
            self.ipc.newConnection.connect(self._on_ipc_connection)
        else:
            log.warning("IPC server failed: %s", self.ipc.errorString())
        # Launched by the wake word (or from the app menu while the user works in another
        # window): come to the front once mapped; GNOME won't focus us on its own.
        QTimer.singleShot(300, lambda: self.handle_wake(wake_audio))

        self.runner.submit(self._check_ollama())
        self.runner.submit(self._load_system_info())
        if voice.enabled:
            self.runner.submit(self._preload_voice())
        self._refresh_recent()
        self._greeting_timer = QTimer(self, interval=10 * 60 * 1000)
        self._greeting_timer.timeout.connect(self.home.hero.refresh_greeting)
        self._greeting_timer.start()

    # ----------------------------------------------------------- view

    def _build_view(self, runtime: "Runtime", voice) -> None:
        self.setStyleSheet(theme.STYLESHEET)
        self.header = Header(self)
        self.sidebar = Sidebar()
        self.home = HomePage()
        self.right = RightPanel()
        self.activity = self.right.activity
        self.command = self.home.hero.command
        self.hands_free = self.header.mic_toggle  # in-app "Jarvis, …" listening
        self.speak = self.header.voice_toggle
        self.speak.setChecked(voice.enabled and voice.speak_responses)
        self.hands_free.setEnabled(voice.enabled)
        self.speak.setEnabled(voice.enabled)

        repo = self.orchestrator.task_repository
        self.pages = QStackedWidget()
        self.page_index: dict[str, int] = {}
        files = FileManagerPage(runtime.settings.permissions.filesystem.allowed_paths)
        apps = ApplicationsPage(runtime.input_ctx.app_index)
        browser = BrowserPage()
        history = HistoryPage(repo)
        settings_page = SettingsPage(runtime.settings, find_config_file(), self.header)
        for key, page in (("home", self.home), ("files", files), ("apps", apps),
                          ("browser", browser), ("history", history),
                          ("settings", settings_page)):
            self.page_index[key] = self.pages.addWidget(page)
        for page in (files, apps, browser):
            page.ask.connect(self._prefill)
        history.rerun.connect(self._prefill)

        center = QScrollArea()
        center.setWidgetResizable(True)
        center.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        center.setWidget(self.pages)
        right = QScrollArea()
        right.setWidgetResizable(True)
        right.setWidget(self.right)
        right.setFixedWidth(360)
        right.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        row = QHBoxLayout(body)
        row.setContentsMargins(14, 14, 14, 10)
        row.setSpacing(14)
        row.addWidget(self.sidebar)
        row.addWidget(center, 1)
        row.addWidget(right)

        root = QWidget()
        root.setObjectName("root")
        from PySide6.QtWidgets import QVBoxLayout

        col = QVBoxLayout(root)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addWidget(self.header)
        col.addWidget(body, 1)
        self.setCentralWidget(root)

        self.footer = FooterStatus()
        self.statusBar().addWidget(self.footer, 1)
        self.statusBar().addPermanentWidget(QSizeGrip(self))

        self.sidebar.page_selected.connect(self.show_page)
        self.header.settings_clicked.connect(lambda: self.show_page("settings"))
        self.home.timeline.view_all.connect(lambda: self.show_page("history"))
        self.right.recent.view_all.connect(lambda: self.show_page("history"))
        self.right.recent.rerun.connect(self._prefill)
        self.home.hero.feature_clicked.connect(self._on_feature)

    def show_page(self, key: str) -> None:
        self.sidebar.select(key)
        self.pages.setCurrentIndex(self.page_index[key])

    def _on_feature(self, key: str) -> None:
        if key == "system":
            self._prefill("Look at my screen and tell me what's open")
        else:
            self.show_page(key)

    def _prefill(self, text: str) -> None:
        self.show_page("home")
        self.command.input.setText(text)
        self.command.input.setFocus()

    def _set_activity(self, state: str, detail: str | None = None) -> None:
        self.activity.set_state(state, detail)
        self.home.hero.set_state(state)

    def _refresh_recent(self) -> None:
        repo = self.orchestrator.task_repository
        if repo is not None:
            self.right.recent.set_tasks(repo.recent(5))

    # ----------------------------------------------------------- worker side

    async def _check_ollama(self) -> None:
        try:
            models = await self.ollama.list_models()
        except OllamaError as e:
            self.bridge.llm_status.emit(False, "Local LLM\nNot running")
            self.bridge.status_message.emit(str(e), theme.RED)
            return
        self.bridge.llm_status.emit(True, "Local LLM Connected\n(Ollama)")
        missing = [m for m in (self.ollama.settings.model,
                               self.runtime.settings.desktop.vision_model)
                   if m not in models and f"{m}:latest" not in models]
        if missing:
            self.bridge.status_message.emit(
                "Missing Ollama model(s): " + ", ".join(missing) + ". Run: ollama pull <model>",
                theme.AMBER,
            )
        else:
            self.bridge.status_message.emit("Jarvis is ready", theme.GREEN)

    async def _load_system_info(self) -> None:
        settings = self.runtime.settings
        llm = f"{settings.ollama.model.replace(':', ' ').title()} (Ollama)"
        rows = await asyncio.to_thread(
            detect_system_info, llm, f"faster-whisper ({settings.voice.stt_model})", "Piper")
        self.bridge.system_info.emit(rows)

    async def _preload_voice(self) -> None:
        try:
            await asyncio.to_thread(self.stt.load)
            await asyncio.to_thread(self.tts.load)
            log.info("Voice ready (speech recognition on %s)", self.stt.device)
        except Exception as e:  # noqa: BLE001 - voice is optional
            log.warning("Voice preload failed: %s", e)
            self.bridge.status_message.emit(f"Voice unavailable: {e}", theme.AMBER)

    async def _run_task(self, prompt: str) -> None:
        try:
            task = await self.orchestrator.run(prompt)
        except asyncio.CancelledError:
            return  # the orchestrator already published the CANCELLED state
        self.bridge.task_finished.emit(task.model_copy(deep=True))

    async def _request_approval(self, task: Task, call: ToolCallRequest, tool: BaseTool) -> bool:
        future: concurrent.futures.Future = concurrent.futures.Future()
        self.bridge.approval_requested.emit(
            ApprovalRequest(task.model_copy(deep=True), call, tool, future)
        )
        if self.speak.isChecked():
            self._speak_async("I need your approval for that.")
        return await asyncio.wrap_future(future)

    async def _yield_focus(self) -> None:
        """Minimize Jarvis so keyboard/mouse input reaches the user's application."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        self.bridge.minimize_requested.emit(future)
        await asyncio.wrap_future(future)

    async def _transcribe_and_run(self, audio: np.ndarray) -> None:
        text = await asyncio.to_thread(self.stt.transcribe, audio)
        if text:
            self.bridge.voice_command.emit(text)
        else:
            self.bridge.status_message.emit("Didn't catch that.", theme.AMBER)

    def _on_utterance(self, audio: np.ndarray) -> None:
        """Hands-free listener thread: transcribe and look for the wake word."""
        self.runner.submit(self._handle_utterance(audio))

    async def _handle_utterance(self, audio: np.ndarray, from_wake_service: bool = False) -> None:
        loop = asyncio.get_running_loop()
        text = await asyncio.to_thread(self.stt.transcribe, audio)
        if not text and not from_wake_service:
            return
        woke, command = parse_wake(text)
        if from_wake_service and not woke:
            # The wake-word model already heard "Jarvis"; Whisper may just spell it oddly.
            woke, command = True, text.strip()
        if not woke and loop.time() < self._awaiting_command_until:
            woke, command = True, text.strip()
        if not woke:
            return
        self._awaiting_command_until = 0.0
        busy = self._current is not None and not self._current.done()
        if is_stop_command(command):
            self.tts.stop()
            if busy:
                self.bridge.cancel_requested.emit()
            return
        if not command:
            self._awaiting_command_until = loop.time() + FOLLOW_UP_WINDOW_S
            self.bridge.follow_up_requested.emit()
            self._speak_async("Yes?")
            return
        if busy:
            self.bridge.voice_heard.emit(f"(busy, ignored: “{command}”)")
            return
        self.bridge.voice_command.emit(command)

    def _speak_async(self, text: str) -> None:
        async def speak() -> None:
            self.listener.pause()  # don't let Jarvis hear itself
            flag = speaking_flag()  # ...nor the background wake-word listener
            flag.touch()
            self.bridge.speaking.emit(True)
            try:
                await asyncio.to_thread(self.tts.speak, text)
            except Exception as e:  # noqa: BLE001
                self.bridge.status_message.emit(f"Speech output failed: {e}", theme.AMBER)
            finally:
                self.bridge.speaking.emit(False)
                await asyncio.sleep(0.3)
                flag.unlink(missing_ok=True)
                self.listener.resume()

        self.runner.submit(speak())

    # ----------------------------------------------------------- Qt thread side

    def _on_submit(self, prompt: str) -> None:
        if self._current is not None and not self._current.done():
            return
        self.tts.stop()
        self._current_prompt = prompt
        self.activity.show_exchange(prompt, None, None)
        self._set_activity(THINKING, "Understanding your command")
        self.command.set_busy(True)
        self._current = self.runner.submit(self._run_task(prompt))
        self._current.add_done_callback(lambda _f: self.bridge.task_finished.emit(None))

    def _on_voice_command(self, text: str) -> None:
        self._on_submit(text)

    # ----------------------------------------------------------- wake word / IPC

    def _on_ipc_connection(self) -> None:
        conn = self.ipc.nextPendingConnection()
        conn.readyRead.connect(lambda: self._read_ipc(conn))
        conn.disconnected.connect(conn.deleteLater)

    def _read_ipc(self, conn) -> None:
        while conn.canReadLine():
            try:
                message = json.loads(bytes(conn.readLine()).decode())
            except ValueError:
                continue
            action = message.get("action")
            if action == "interrupt":
                self.tts.stop()  # the user said "Jarvis" over our speech
            elif action == "wake":
                self.handle_wake(message.get("audio"))

    def handle_wake(self, audio_path: str | None) -> None:
        """Wake word heard: bring the window up and run whatever was said with it."""
        self.tts.stop()
        bring_to_front(self)
        if not audio_path:
            return
        try:
            audio = load_wav(audio_path)
        except (OSError, wave.Error) as e:
            self.footer.set(f"Could not read wake audio: {e}", theme.AMBER)
            return
        finally:
            Path(audio_path).unlink(missing_ok=True)
        self.runner.submit(self._handle_utterance(audio, from_wake_service=True))

    def _listen_for_follow_up(self) -> None:
        """After a bare "Jarvis": listen once for the command even if hands-free is off."""
        if self.listener.running:
            return
        try:
            self.listener.start()
        except MicrophoneError as e:
            self.footer.set(str(e), theme.AMBER)
            return
        self._set_activity(LISTENING)
        QTimer.singleShot(int((FOLLOW_UP_WINDOW_S + 4) * 1000), self._end_follow_up)

    def _end_follow_up(self) -> None:
        if not self.hands_free.isChecked():
            self.listener.stop()
        if not self._busy():
            self._set_activity(IDLE)

    def _busy(self) -> bool:
        return self._current is not None and not self._current.done()

    def _on_speaking(self, speaking: bool) -> None:
        if speaking:
            self._set_activity(SPEAKING)
        elif not self._busy():
            self._set_activity(IDLE)

    def _on_cancel(self) -> None:
        if self._current and not self._current.done():
            self._current.cancel()

    def _on_mic_toggled(self, recording: bool) -> None:
        try:
            if recording:
                self.tts.stop()
                self.mic.start()
                self._set_activity(LISTENING, "Click the mic again (or Ctrl+Space) when done")
            else:
                audio = self.mic.stop()
                self._set_activity(THINKING, "Transcribing…")
                self.runner.submit(self._transcribe_and_run(audio))
        except MicrophoneError as e:
            self.command.set_mic_state(False)
            self.footer.set(str(e), theme.AMBER)

    def _on_hands_free_toggled(self, enabled: bool) -> None:
        try:
            if enabled:
                self.listener.start()
                self.footer.set("Hands-free: say “Jarvis, …”")
            else:
                self.listener.stop()
        except MicrophoneError as e:
            self.hands_free.blockSignals(True)
            self.hands_free.setChecked(False)
            self.hands_free.blockSignals(False)
            self.footer.set(str(e), theme.AMBER)

    def _on_minimize_requested(self, future: concurrent.futures.Future) -> None:
        if self.isActiveWindow() or not self.isMinimized():
            self.showMinimized()
        if not future.done():
            future.set_result(None)

    def _on_task_updated(self, task: Task) -> None:
        self.home.timeline.update_task(task)
        if not task.status.is_terminal:
            running = [s for s in task.steps if not s.status.is_terminal]
            if running:
                name = running[-1].tool_call.name if running[-1].tool_call else ""
                self._set_activity(THINKING, TOOL_LABELS.get(name, (running[-1].description,))[0])
        for step in task.steps:  # Tier 1 actions are surfaced as a notice once done
            key = (task.id, step.step_id)
            if (step.risk_level == RiskLevel.LOW and step.status == TaskStatus.COMPLETED
                    and key not in self._notified_steps):
                self._notified_steps.add(key)
                self.footer.set(f"Done: {step.description}")
        if task.status == TaskStatus.CANCELLED and task.final_response:
            self._show_final(task)

    def _on_task_finished(self, task: Task | None) -> None:
        if task is None:  # done-callback: always re-enable input
            self.command.set_busy(False)
            self._clear_stale_approvals()
            self._refresh_recent()
            if self.activity.title.text() != "Speaking…":
                self._set_activity(IDLE)
            return
        if task.status != TaskStatus.CANCELLED:
            self._show_final(task)

    def _show_final(self, task: Task) -> None:
        key = (task.id, -1)
        if key in self._notified_steps:
            return
        self._notified_steps.add(key)
        self.activity.show_exchange(task.user_prompt, task.final_response or "", task.status)
        color = {TaskStatus.COMPLETED: theme.GREEN, TaskStatus.FAILED: theme.RED}.get(
            task.status, theme.AMBER)
        self.footer.set({TaskStatus.COMPLETED: "Jarvis is ready",
                         TaskStatus.FAILED: "Last task failed",
                         TaskStatus.CANCELLED: "Task cancelled"}.get(task.status, ""), color)
        if self.speak.isChecked() and task.final_response:
            self._speak_async(task.final_response)

    # ----------------------------------------------------------- approvals (inline)

    def _on_approval_requested(self, request: ApprovalRequest) -> None:
        if request.future.done():  # task was cancelled before it was shown
            return
        bring_to_front(self)  # we may have minimized ourselves for keyboard/mouse work
        self._approvals.append(request)
        if len(self._approvals) == 1:
            self._show_approval(request)

    def _show_approval(self, request: ApprovalRequest) -> None:
        step = next((s for s in request.task.steps
                     if s.tool_call and s.tool_call.id == request.call.id), None)
        headline = step.description if step else request.tool.describe_call(
            request.call.arguments)
        icon_name = TOOL_LABELS.get(request.tool.name, ("", "shield"))[1]
        self.right.approvals.show_request(headline, request.task.user_prompt,
                                          request.call.arguments, icon_name)
        self.footer.set("Waiting for your approval", theme.AMBER)

    def _on_approval_decided(self, approved: bool) -> None:
        if not self._approvals:
            return
        request = self._approvals.pop(0)
        if not request.future.done():
            request.future.set_result(approved)
        self._next_approval()

    def _clear_stale_approvals(self) -> None:
        self._approvals = [r for r in self._approvals if not r.future.done()]
        self._next_approval()

    def _next_approval(self) -> None:
        self._approvals = [r for r in self._approvals if not r.future.done()]
        if self._approvals:
            self._show_approval(self._approvals[0])
        else:
            self.right.approvals.clear()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        self._on_cancel()
        self.ipc.close()
        speaking_flag().unlink(missing_ok=True)
        self.listener.stop()
        self.tts.stop()
        if self.mic.is_recording:
            self.mic.stop()
        try:
            self.runner.submit(self.runtime.aclose()).result(timeout=5)
        except Exception:  # noqa: BLE001 - best-effort cleanup on exit
            pass
        self.runner.stop()
        super().closeEvent(event)
